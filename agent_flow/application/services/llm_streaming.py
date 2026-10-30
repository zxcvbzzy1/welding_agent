from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from domain.runtime_hooks import get_run_context_provider
from domain.run_context import current_execution_id
from infra.runtime.control import ensure_execution_active
from domain.json_parse import robust_json_load
from application.services.events import EventStreamService
from domain.memory.long.chunking import EstimatedTokenCounter


class StreamingObservableLLMClient:
    """Mirrors LLM streaming chunks into the run SSE stream."""

    def __init__(
        self,
        base_llm,
        streams: EventStreamService,
        *,
        agent_id: str,
        agent_name: str,
        agent_type: str,
        context_engine=None,
        context_id: str = '',
    ) -> None:
        self._base = base_llm
        self._streams = streams
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.model = getattr(base_llm, "model", "")
        self.max_tokens = getattr(base_llm, "max_tokens", None)
        self.context_window = getattr(base_llm, 'context_window', None)
        self.context_engine, self.context_id = context_engine, context_id
        self.agent = None
        self.trace_metadata = {}

    async def chat(self, messages: list, *, max_tokens=None, purpose='') -> str:
        await ensure_execution_active()
        call_role = purpose or self._detect_call_role(messages)
        run_id = self._run_id()
        sequence = 0
        full_response = ""

        if self.context_engine is not None and not purpose:
            messages = [dict(message) for message in messages]
            usage = self.context_snapshot(messages, call_role)
            while self.context_window and usage['input_tokens'] > self.context_window:
                if self.agent is None:
                    raise ValueError('输入超过上下文容量')
                from infra.context.runtime import request_agent_question
                result = await request_agent_question(self.agent, [{
                    'question': '当前输入超过模型上下文容量，是否重试压缩？压缩失败的原始反馈仍然保留。',
                    'type': 'single', 'options': ['重试压缩', '取消本次任务'],
                }])
                if result['answers'][0].get('selected_option_ids') != ['o1']:
                    raise asyncio.CancelledError('context_capacity_cancelled')
                previous = self.context_engine.last_text
                await self.context_engine.prepare(self.context_engine.last_state)
                refreshed = self.context_engine.refresh_tool_sections()
                for message in messages:
                    if message.get('role') == 'user':
                        if message.get('content') == previous:
                            message['content'] = refreshed
                        elif isinstance(message.get('content'), list):
                            message['content'] = [
                                {**block, 'text': refreshed}
                                if block.get('type') == 'text' and block.get('text') == previous else block
                                for block in message['content']
                            ]
                usage = self.context_snapshot(messages, call_role)
            if run_id:
                await self._streams.publish_context_event(run_id, 'context.usage.updated', usage)
            logging.getLogger(__name__).info('Model context agent=%s context=%s phase=%s tools=%s input_tokens=%s',
                self.agent_id, self.context_id, call_role, usage['available_tool_names'], usage['input_tokens'])

        if run_id:
            await self._publish(
                run_id,
                "llm.started",
                {
                    "call_role": call_role,
                    "model": self.model,
                },
            )

        options = {'model': self.model}
        if max_tokens is not None:
            options['max_tokens'] = max_tokens
        async for delta in self._base.stream_chat(messages, **options):
            print(delta, end="", flush=True)
            full_response += delta
            sequence += 1
            if run_id:
                await self._sse_publish(
                    run_id,
                    "llm.delta",
                    {
                        "call_role": call_role,
                        "delta": delta,
                        "sequence": sequence,
                    },
                )

        if run_id:
            await self._publish(
                run_id,
                "llm.completed",
                {
                    "call_role": call_role,
                    "content": full_response,
                    "token_chunks": sequence,
                },
            )
            if not purpose:
                await self._publish_structured(run_id, call_role, full_response)

        return full_response

    def context_snapshot(self, messages, phase):
        counter = EstimatedTokenCounter()
        components, tools = [], []
        image_count = 0
        for message_index, message in enumerate(messages):
            raw = message.get('content', '')
            content = ('\n\n'.join(block.get('text', '') for block in raw if block.get('type') == 'text')
                       if isinstance(raw, list) else str(raw))
            if isinstance(raw, list):
                image_count += sum(block.get('type') in {'file', 'image_url'} for block in raw)
            if (self.context_engine is not None and message.get('role') == 'user' and
                    content == self.context_engine.last_text):
                prefix = ''
                for index, section in enumerate(self.context_engine.last_sections):
                    updated = '\n\n'.join(filter(None, [prefix, section['text']]))
                    components.append({'id': f'{message_index}:{index}', 'name': section['name'],
                                       'tokens': counter.count(updated) - counter.count(prefix)})
                    prefix = updated
                    if section['name'] == 'available_tools':
                        tools.extend(section['tool_names'])
            else:
                components.append({'id': str(message_index), 'name': 'system_prompt' if message.get('role') == 'system'
                    else 'other_input', 'tokens': counter.count(content)})
        # Count one canonical representation of the actual messages, including
        # role/content keys and escaping; allocate remaining cost to formatting.
        # Provider-specific chat templates remain an estimate, not billing usage.
        serialized = json.dumps(messages, ensure_ascii=False, separators=(',', ':'))
        total = counter.count(serialized)
        components.append({'id': 'message_format', 'name': 'message_format',
                           'tokens': total - sum(c['tokens'] for c in components)})
        # Files IDs are not the visual token cost. Use the documented conservative
        # per-image bound separately from the existing text/format estimator.
        if image_count:
            components.append({'id': 'images', 'name': 'images', 'tokens': image_count * 1024})
        return {'call_id': str(uuid.uuid4()), 'execution_id': current_execution_id.get(),
                'agent_id': self.agent_id, 'agent_name': self.agent_name, 'agent_type': self.agent_type,
                'context_id': self.context_id, 'model': self.model, 'phase': phase, 'created_at': time.time(),
                'context_window': self.context_window, 'input_tokens': sum(c['tokens'] for c in components),
                'components': components, 'count_method': counter.name, 'available_tool_names': sorted(set(tools)),
                'estimated': True, 'image_count': image_count, 'image_tokens': image_count * 1024,
                'image_tokens_estimated': bool(image_count), **self.trace_metadata}

    def _run_id(self) -> str:
        if self._streams is None:
            return ''
        provider = get_run_context_provider()
        if provider is None:
            return ""
        return provider.run_id_for_agent(self.agent_id)

    async def _publish(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        await self._streams.publish(
            run_id,
            name,
            {
                "run_id": run_id,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "agent_type": self.agent_type,
                **self.trace_metadata,
                **payload,
            },
        )

    async def _sse_publish(self, run_id: str, name: str, payload: dict[str, Any]) -> None:
        await self._streams.no_store_publish(
            run_id,
            name,
            {
                "run_id": run_id,
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "agent_type": self.agent_type,
                **self.trace_metadata,
                **payload,
            },
        )

    async def _publish_structured(self, run_id: str, call_role: str, raw: str) -> None:
        if self.agent_type == "planner":
            await self._publish_planner_structured(run_id, call_role, raw)
            return
        await self._publish_executor_structured(run_id, raw)

    async def _publish_executor_structured(self, run_id: str, raw: str) -> None:
        data = self._parse_json(raw)
        if data is None:
            await self._publish(run_id, "agent.think", {"think": raw})
            return

        think = data.get("think", "")
        if think:
            await self._publish(run_id, "agent.think", {"think": think})

        for tool_call in data.get("tool_calls", []) or []:
            reasoning = tool_call.get("reasoning", "")
            if not reasoning:
                continue
            await self._publish(
                run_id,
                "agent.tool.reasoning",
                {
                    "tool_name": tool_call.get("tool_name", ""),
                    "reasoning": reasoning,
                    "arguments": tool_call.get("arguments", {}),
                },
            )

        if data.get("is_finished"):
            await self._publish(
                run_id,
                "agent.final",
                {
                    "finish_reason": data.get("finish_reason", ""),
                    "final": data.get("final", "") or data.get("finish_reason", ""),
                },
            )

    async def _publish_planner_structured(self, run_id: str, call_role: str, raw: str) -> None:
        data = self._parse_json(raw)
        if call_role == "plan_summary":
            await self._publish(run_id, "planner.final", {"planner_id": self.agent_id, "final": raw})
            return
        if data is None:
            return

        if data.get("tool_calls"):
            # Exploration is not a completed plan or a replan decision.
            return

        if call_role == "plan_replan":
            await self._publish(
                run_id,
                "planner.replan.reasoning",
                {
                    "planner_id": self.agent_id,
                    "action": data.get("action", ""),
                    "reason": data.get("reason", ""),
                    "steps": data.get("steps", []),
                    "raw": data,
                },
            )
            return

        if "steps" in data:
            await self._publish(
                run_id,
                "planner.plan.generated",
                {
                    "planner_id": self.agent_id,
                    "steps": data.get("steps", []),
                    "raw": data,
                },
            )

    def _detect_call_role(self, messages: list) -> str:
        if self.agent_type == "executor":
            return "executor_think"

        system = ""
        if messages:
            system = str(messages[0].get("content", ""))
        lowered = system.lower()

        if "replan" in lowered or "重新规划" in system or "判断下一步" in system:
            return "plan_replan"
        if "总结本次任务" in system or "最终结果" in system:
            return "plan_summary"
        if "生成结构化计划" in system or '"steps"' in system or "steps" in lowered:
            return "plan_generate"
        return "unknown"

    def _parse_json(self, raw: str) -> dict[str, Any] | None:
        # 鲁棒解析：先直接解析原文，避免把字符串内嵌的 ```json 代码块误当成外层围栏。
        return robust_json_load(raw)
