
import asyncio
import codecs
import errno
import json
from enum import Enum
import os
from pathlib import Path
import shlex
import tempfile
import logging
import math
import time
import uuid
from dataclasses import dataclass
from collections.abc import Callable
import httpx
from dotenv import load_dotenv
from openai import AsyncOpenAI, APIConnectionError, APIStatusError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMCallPolicy:
    connect_timeout: float = 10
    first_output_timeout: float = 180
    idle_timeout: float = 90
    total_timeout: float = 900
    max_retries: int = 3
    retry_delay: float = 10
    backoff: float = 1.5

    def __post_init__(self):
        for name in ('connect_timeout', 'first_output_timeout', 'idle_timeout',
                     'total_timeout', 'retry_delay', 'backoff'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} 必须为正数')
        if not isinstance(self.max_retries, int) or self.max_retries < 0:
            raise ValueError('max_retries 必须为非负整数')


class LLMGatewayTimeout(TimeoutError):
    def __init__(self, stage: str):
        self.stage = stage
        super().__init__({'first_output': '等待模型有效输出超时', 'idle': '模型流连续无有效片段',
                          'total': '模型调用总预算耗尽'}[stage])

for env_path in (
    Path.cwd() / ".env",
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
):
    load_dotenv(env_path, override=False)

class LLM_Model_Provider(Enum):
    """LLM模型提供者"""
    GLM = "GLM_API"
    DEEPSEEK = "DEEPSEEK_API"
    MINMAX = "MINIMAX_API"

    @property
    def context_window(self):
        return PROVIDER_CONTEXT_WINDOWS[self]


# Reference-model defaults, checked 2026-10-29; not universal provider limits.
# https://huggingface.co/zai-org/GLM-5/raw/main/config.json
# https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/raw/main/config.json
# https://huggingface.co/MiniMaxAI/MiniMax-M2.7/raw/main/config.json
PROVIDER_CONTEXT_WINDOWS = {
    LLM_Model_Provider.GLM: 202752,
    LLM_Model_Provider.DEEPSEEK: 1048576,
    LLM_Model_Provider.MINMAX: 204800,
}



class LLM_Client:
    supports_gateway_progress = True

    def __init__(self,url:str,model_class:str,model_provider:LLM_Model_Provider,max_tokens:int, *,
                 context_window: int | None = None, call_policy: LLMCallPolicy | None = None):
        self.url = url
        self.model_provider = model_provider
        self._client: AsyncOpenAI | None = None
        self.model = model_class
        self.max_tokens =max_tokens
        self.call_policy = call_policy if call_policy is not None else LLMCallPolicy()
        self._clock = time.monotonic
        self._sleep = asyncio.sleep
        self.context_window = model_provider.context_window if context_window is None else context_window
        if self.context_window <= 0:
            raise ValueError('context_window 必须为正数')

    def _api_key(self) -> str:
        key = os.getenv(self.model_provider.value, "").strip()
        if not key or key == "im-backend-placeholder":
            raise RuntimeError(
                f"LLM API key 未配置：请在环境变量或 .env 中设置 {self.model_provider.value}。"
                f"当前 base_url={self.url}, model={self.model}"
            )
        return key

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key(),
                base_url=self.url,
            )
        return self._client

    async def default_call(self, messages: list, model: str):
        async for delta in self.stream_chat(messages, model=model):
            yield delta

    @staticmethod
    def _retryable(error):
        if isinstance(error, APIStatusError):
            override = error.response.headers.get('x-should-retry')
            if override in {'true', 'false'}:
                return override == 'true'
            return error.status_code in {408, 409, 429} or error.status_code >= 500
        return isinstance(error, (APIConnectionError, httpx.TransportError, LLMGatewayTimeout))

    @staticmethod
    def _failure(error):
        if isinstance(error, LLMGatewayTimeout):
            return f'{error.stage}_timeout', str(error)
        if isinstance(error, APIStatusError):
            return f'http_{error.status_code}', f'网关返回 HTTP {error.status_code}'
        if isinstance(error, (APIConnectionError, httpx.TransportError)):
            return 'connection_error', '模型网关连接异常'
        return 'gateway_error', f'模型调用异常（{type(error).__name__}）'

    async def _before(self, operation, *, deadline, total_deadline, stage):
        remaining = min(deadline, total_deadline) - self._clock()
        if remaining <= 0:
            raise LLMGatewayTimeout('total' if self._clock() >= total_deadline else stage)
        try:
            return await asyncio.wait_for(operation(), remaining)
        except TimeoutError as error:
            raise LLMGatewayTimeout('total' if self._clock() >= total_deadline else stage) from error

    async def _close_stream(self, stream):
        if stream is not None:
            try:
                await asyncio.wait_for(stream.close(), 5)
            except Exception:
                log.warning('Could not close model response stream', exc_info=True)

    async def stream_chat(self, messages: list, model: str | None = None, *, max_tokens: int | None = None,
                          progress: Callable[[dict], None] | None = None, call_id: str | None = None):
        """Retry gateway I/O only, before any body text; never replay agent tools.

        The optional synchronous callback only reports metadata. Callers must not
        perform network I/O in it; callback errors cannot restart a model request.
        """
        policy = self.call_policy
        call_id = call_id or str(uuid.uuid4())
        started = self._clock()
        total_deadline = started + policy.total_timeout
        body_started = False

        def report(status, attempt, error=None, delay=0):
            code, reason = self._failure(error) if error is not None else ('', '')
            item = {'call_id': call_id, 'status': status, 'attempt': attempt,
                    'max_attempts': policy.max_retries + 1, 'retry_delay': delay,
                    'elapsed_seconds': max(0, self._clock() - started), 'error_code': code, 'reason': reason}
            emit_log = log.warning if status in {'retrying', 'failed'} else log.info
            emit_log('Model gateway call=%s model=%s status=%s attempt=%s/%s elapsed=%.3f error=%s',
                     call_id, model or self.model, status, attempt, policy.max_retries + 1,
                     item['elapsed_seconds'], code)
            if progress is not None:
                try:
                    progress(item)
                except Exception:
                    log.warning('Model progress callback failed call=%s', call_id, exc_info=True)

        for attempt in range(1, policy.max_retries + 2):
            stream = None
            error = None
            report('waiting', attempt)
            progress_deadline = self._clock() + policy.first_output_timeout
            stage = 'first_output'
            try:
                # Per-request options leave Files API retries/timeouts unchanged.
                client = self.client.with_options(max_retries=0, timeout=httpx.Timeout(
                    connect=policy.connect_timeout, pool=policy.connect_timeout, read=None, write=None))
                stream = await self._before(lambda: client.chat.completions.create(
                    model=model or self.model, messages=messages,
                    max_tokens=self.max_tokens if max_tokens is None else max_tokens,
                    stream=True, extra_body={"reasoning_split": True}),
                    deadline=progress_deadline, total_deadline=total_deadline, stage=stage)
                log.info('Model gateway response headers call=%s attempt=%s', call_id, attempt)
                iterator = stream.__aiter__()
                while True:
                    try:
                        chunk = await self._before(lambda: anext(iterator), deadline=progress_deadline,
                                                   total_deadline=total_deadline, stage=stage)
                    except StopAsyncIteration:
                        return
                    if not chunk.choices:
                        continue  # e.g. usage-only packets, not inference progress.
                    delta = chunk.choices[0].delta
                    content = delta.content
                    reasoning = getattr(delta, 'reasoning_content', None)
                    if content or reasoning:
                        if stage == 'first_output':
                            log.info('Model gateway first effective fragment call=%s attempt=%s', call_id, attempt)
                        stage = 'idle'
                        progress_deadline = self._clock() + policy.idle_timeout
                    if content:
                        body_started = True
                        yield content
            except Exception as exc:
                error = exc
            finally:
                await self._close_stream(stream)
            if (body_started or not self._retryable(error) or attempt > policy.max_retries
                    or self._clock() >= total_deadline):
                report('failed', attempt, error)
                raise error
            delay = policy.retry_delay * policy.backoff ** (attempt - 1)
            report('retrying', attempt, error, delay)
            try:
                await self._before(lambda: self._sleep(delay), deadline=total_deadline,
                                   total_deadline=total_deadline, stage='total')
                if self._clock() >= total_deadline:
                    raise LLMGatewayTimeout('total')
            except LLMGatewayTimeout as exc:
                report('failed', attempt, exc)
                raise
    
    async def chat(self, messages: list, *, max_tokens: int | None = None, purpose: str = ""):
        full_response = ""
        async for delta in self.stream_chat(messages, model=self.model, max_tokens=max_tokens):
            print(delta, end="", flush=True)
            full_response += delta
        return full_response
