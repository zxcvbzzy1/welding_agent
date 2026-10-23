"""Generic TypeSafe JEV client (official SDK); callers supply prompts, criteria and question keys.

Reusable for any judgement over a structured state: memory gating/classification,
"should we recall?" decisions, etc. No generative calls and no application dependencies.
"""
from __future__ import annotations

import os
import importlib
from importlib.metadata import version, PackageNotFoundError


class JevConfigurationError(ValueError):
    retryable = False


class JevRequestError(RuntimeError):
    def __init__(self, message, *, retryable=True):
        super().__init__(message)
        self.safe_message = message
        self.retryable = retryable


class JevClient:
    def __init__(self, model: str | None = None, *, timeout: float = 90, max_retries: int = 0):
        self.model = model or os.getenv("JEV_MODEL", "jev-latest")
        self.timeout, self.max_retries = timeout, max_retries

    def ready(self) -> bool:
        return self.diagnostics()["status"] == "configured"

    def diagnostics(self) -> dict:
        try:
            sdk = importlib.import_module("typesafe_sdk")
            for name in ("AsyncTypeSafeClient", "RetryPolicy", "Noul", "Choice"):
                getattr(sdk, name)
            installed = version("pydantic")
            if tuple(map(int, installed.split('.')[:2])) < (2, 12) or not installed.startswith('2.'):
                raise ImportError("incompatible pydantic")
        except (ImportError, AttributeError, PackageNotFoundError):
            return {"status": "missing_dependency", "reason": "JEV SDK 或依赖不可用；请在后端 Python 环境安装 typesafe-sdk==0.7.1、pydantic>=2.12,<3"}
        if not os.getenv("TYPESAFE_API_KEY", "").strip():
            return {"status": "missing_credentials", "reason": "未配置 TYPESAFE_API_KEY"}
        return {"status": "configured", "reason": "SDK 与凭据已配置，尚未自动检测远端连接"}

    def _sdk(self):
        diagnostic = self.diagnostics()
        if diagnostic["status"] != "configured":
            raise JevConfigurationError(diagnostic["reason"])
        return importlib.import_module("typesafe_sdk")

    async def ask(self, state, questions: dict):
        """One system_one call; several questions share the same state and request."""
        sdk = self._sdk()
        try:
            async with sdk.AsyncTypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"].strip(), timeout=self.timeout,
                                               retry=sdk.RetryPolicy(max_retries=self.max_retries)) as client:
                return await client.system_one(state=state, questions=questions, model=self.model)
        except sdk.TypeSafeError as exc:
            status = getattr(exc, "status", None)
            retryable = status in (408, 429) or (isinstance(status, int) and status >= 500)
            retryable = retryable or isinstance(exc, (sdk.TypeSafeAPIConnectionError, sdk.TypeSafeAPITimeoutError))
            reason = {401: "JEV 鉴权失败，请检查服务端凭据", 403: "JEV 访问被拒绝，请检查账号权限",
                      429: "JEV 请求限流，请稍后重试"}.get(status, "JEV 请求失败，请检查模型名称、网络和服务端配置")
            raise JevRequestError(reason, retryable=retryable) from None

    async def noul(self, state, instructions: str, *, key: str = "default") -> dict:
        sdk = self._sdk()
        response = await self.ask(state, {key: sdk.Noul(instructions=instructions)})
        return {"noul": response.nouls[key].noul, "model": response.model}

    async def choice(self, state, instructions: str, criteria: dict, *, key: str = "default") -> dict:
        sdk = self._sdk()
        response = await self.ask(state, {key: sdk.Choice(instructions=instructions, criteria=criteria)})
        answer = response.choices[key]
        return {"probabilities": answer.probabilities, "confidence": answer.confidence, "model": response.model}
