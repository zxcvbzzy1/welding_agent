from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from domain.agent.plan.planAgent import PlanAgent
from domain.agent_base import AgentBase
from domain.context.context import ContextEngine
from domain.event import Event, EventBusPort
from domain.state import Plan, PlanStep, _dict_to_plan



@dataclass
class OrchestratorState:
    prompt: str = ""
    executors: dict[str, dict] = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    current_step: dict | None = None
    final: str = ""
    is_finished: bool = False
    finish_reason: str = ""
    long_term_memory: list[dict] = field(default_factory=list)

    def to_context_dict(self) -> dict:
        state = {
            "prompt": self.prompt,
            "executors": self.executors,
            "plan": self.plan,
            "final": self.final,
            "is_finished": self.is_finished,
            "finish_reason": self.finish_reason,
            "long_term_memory": self.long_term_memory,
        }
        if self.current_step is not None:
            state["current_step"] = self.current_step
        return state

    def get(self, key: str, default=None):
        """兼容 provider / notebook 中少量 dict 风格读取。"""
        return self.to_context_dict().get(key, default)

event_dispatch = Literal[
    "workflow.started",
    "plan.generated",
    "wave.completed",
    "plan.replanned",
    "plan.step.observed",
    "workflow.finished"
]



class PlanOrchestrator:
    """Plan workflow 的依赖调度、执行和 observation 编排器。"""

    def __init__(
        self,
        planner: PlanAgent,
        executors: dict[str, AgentBase],
        step_context_engine: ContextEngine,
        event_bus: EventBusPort | None = None,
        state: OrchestratorState | None = None,
        max_replan_rounds: int = 5,
        on_invalid_replan=None,
    ) -> None:
        self.planner = planner
        self.executors = executors
        self.step_context_engine = step_context_engine
        self.event_bus = event_bus
        self.max_replan_rounds = max_replan_rounds
        self.on_invalid_replan = on_invalid_replan
        self._replan_rounds = 0
        # 避免可变默认参数跨实例共享：未显式传入时每个 orchestrator 独立构造 OrchestratorState。
        self.state = state if state is not None else OrchestratorState()
        self._executor_locks: dict[str, asyncio.Lock] = {
            executor_id: asyncio.Lock()
            for executor_id in executors
        }

    async def start(self, prompt: str) -> None:
        await self._dispatch({
            "event_dispatch": "workflow.started",
            "playload": {"prompt": prompt},
        })

        plan = await self._invoke_agent(
            self.planner, "generate_plan", self.state.to_context_dict(),
            list(self.executors.keys()),
        )
        await self._dispatch({
            "event_dispatch": "plan.generated",
            "playload": {"plan": plan},
        })

        await self.execute(plan)

        final = await self._invoke_agent(self.planner, "summarize_result", self.state.to_context_dict())
        await self._dispatch({
            "event_dispatch": "workflow.finished",
            "playload": {
                "final": final,
                "finish_reason": "计划编排执行完成",
            },
        })

    async def execute(self, plan: Plan) -> None:
        while True:
            self._fail_steps_with_unknown_executor(plan)
            await self._dispatch({
                "event_dispatch": "wave.completed",
                "playload": {"plan": plan},
            })

            pending_steps = [step for step in plan.steps if step.status == "pending"]
            if not pending_steps:
                return

            ready_steps = [
                step for step in pending_steps
                if self._dependencies_done(step, plan)
            ]

            if not ready_steps:
                self._fail_blocked_steps(pending_steps, plan)
                await self._dispatch({
                    "event_dispatch": "wave.completed",
                    "playload": {"plan": plan},
                })
                return

            await asyncio.gather(*[
                self._run_plan_step(step, plan)
                for step in ready_steps
            ])

            await self._dispatch({
                "event_dispatch": "wave.completed",
                "playload": {"plan": plan},
            })
            await self._publish_event(
                "plan.wave.completed",
                {
                    "planner_id": self.planner.id,
                    "completed_step_ids": [step.step_id for step in ready_steps],
                    "plan": plan.to_dict(),
                },
            )

            should_finish = await self._replan_after_observation(plan)
            if should_finish:
                return
            
    # 内部函数
    def _fail_steps_with_unknown_executor(self, plan: Plan) -> None:
        for step in plan.steps:
            if step.status == "pending" and step.executor_id not in self.executors:
                step.status = "failed"
                step.status_reason = f"未知 executor_id: {step.executor_id}"
                step.result_observation = step.status_reason

    async def _dispatch(self, action: dict) -> None:
        action_type = action.get("event_dispatch")
        playload = action.get("playload", {})
        
        if action_type == "workflow.started":
            self.state.prompt = playload.get("prompt", "")
            self.state.executors = self._executor_status()
            self.state.current_step = None
            self.planner.prepare_start(self.state.prompt, keep_history=True)
            return

        if action_type in {"plan.generated", "wave.completed", "plan.replanned"}:
            plan = playload["plan"]
            self.state.plan = plan.to_dict()
            self.state.executors = self._executor_status()
            self.state.current_step = None
            return

        if action_type == "workflow.finished":
            self.state.executors = self._executor_status()
            self.state.final = playload.get("final", "")
            self.state.is_finished = True
            self.state.finish_reason = playload.get("finish_reason", "")
            self.state.current_step = None
            self.planner.store_dialogue_history(
                self.state.prompt,
                self.state.final,
            )

    def _executor_status(self) -> dict[str, dict]:
        status = {}
        for executor_id, executor in self.executors.items():
            state = getattr(executor, "states", {})
            status[executor_id] = {
                "name": getattr(executor, "name", executor_id),
                "description": getattr(executor, "description", ""),
                "is_finished": state.get("is_finished", False),
                "final": state.get("final", ""),
                "finish_reason": state.get("finish_reason", ""),
            }
        return status

    def _dependencies_done(self, step: PlanStep, plan: Plan) -> bool:
        done_step_ids = {
            item.step_id for item in plan.steps
            if item.status == "done"
        }
        return all(dependency in done_step_ids for dependency in step.depends_on)

    def _executor_lock(self, executor_id: str) -> asyncio.Lock:
        if executor_id not in self._executor_locks:
            self._executor_locks[executor_id] = asyncio.Lock()
        return self._executor_locks[executor_id]

    async def _run_plan_step(self, step: PlanStep, plan: Plan) -> None:
        async with self._executor_lock(step.executor_id):
            executor = self.executors[step.executor_id]
            step.status = "in_progress"
            await self._publish_event("plan.step.started", {"step": step.to_dict(), "plan": plan.to_dict()})

            step_prompt = self.step_context_engine.build(
                {
                **self.state.to_context_dict(),
                "plan": plan.to_dict(),
                "executors": self._executor_status(),
                "current_step": step.to_dict(),
            }
            )
            try:
                executor.states["is_finished"] = False
                executor.states["finish_reason"] = ""
                executor.states["final"] = ""
                await self._invoke_agent(executor, "start", step_prompt, step_id=step.step_id)
                if executor.states.get("is_finished", True):
                    step.status = "done"
                    step.status_reason = executor.states.get("finish_reason", "执行完成")
                else:
                    step.status = "failed"
                    step.status_reason = executor.states.get("finish_reason", "执行失败")
            except Exception as exc:
                step.status = "failed"
                step.status_reason = f"执行异常: {exc}"
            finally:
                step.result_observation = self._build_step_observation(step, executor)
                await self._publish_event(
                    "plan.step.observed",
                    {
                        "planner_id": self.planner.id,
                        "step": step.to_dict(),
                        "plan": plan.to_dict(),
                    },
                )

    def _build_step_observation(self, step: PlanStep, executor: AgentBase) -> str:
        state = getattr(executor, "states", {})
        return "\n".join([
            f"executor_id={step.executor_id}",
            f"status={step.status}",
            f"finish_reason={state.get('finish_reason', '')}",
            f"last_tool_ok={state.get('last_tool_ok', True)}",
            f"final={state.get('final', '')}",
        ])


    async def _invoke_agent(self, agent, method: str, *args, step_id: str = ""):
        """Application adapters may observe a call without infrastructure in the domain."""
        return await getattr(agent, method)(*args)

    async def _publish_event(self, name: str, payload: dict) -> None:
        if self.event_bus is None:
            return
        await self.event_bus.publish(Event(name=name, payload=payload))



    async def _replan_after_observation(self, plan: Plan) -> bool:
        if self._replan_rounds >= self.max_replan_rounds:
            return False

        self._replan_rounds += 1
        attempts = 0
        while True:
            decision = await self._invoke_agent(
                self.planner, "replan_after_observation", plan, self.state.to_context_dict(),
            )
            try:
                if not isinstance(decision, dict):
                    raise ValueError('重规划必须返回 JSON 对象')
                action = decision.get("action", "continue")
                if not isinstance(action, str) or action not in {"continue", "finish", "replan"}:
                    raise ValueError(f"未知重规划动作: {action}")
                if action == "replan":
                    self._apply_replan_steps(plan, decision.get("steps", []))
                break
            except ValueError as exc:
                attempts += 1
                error = str(exc)
                self.planner.context_engine.get_memory().store('error', 'replan_validation',
                    f"重规划未生效，原计划保持不变。请修正候选依赖图：{error}")
                await self._publish_event('plan.replan.rejected', {
                    'planner_id': self.planner.id, 'attempt': attempts, 'error': error})
                if attempts >= 3:
                    if self.on_invalid_replan is None:
                        raise ValueError(f"重规划连续三次无效，且没有人工澄清回调：{error}") from exc
                    if await self.on_invalid_replan(self.planner, plan, error) == 'continue':
                        return False
                    attempts = 0

        if action == "finish":
            for step in plan.steps:
                if step.status == "pending":
                    step.status = "skipped"
                    step.status_reason = decision.get("reason", "replan 决定提前结束")
                    step.result_observation = step.status_reason
            await self._dispatch({
                "event_dispatch": "plan.replanned",
                "playload": {"plan": plan},
            })
            return True

        if action == "replan":
            await self._dispatch({
                "event_dispatch": "plan.replanned",
                "playload": {"plan": plan},
            })

        return False



    def _fail_blocked_steps(self, pending_steps: list[PlanStep], plan: Plan) -> None:
        existing_step_ids = {step.step_id for step in plan.steps}
        done_step_ids = {
            step.step_id for step in plan.steps
            if step.status == "done"
        }
        failed_step_ids = {
            step.step_id for step in plan.steps
            if step.status == "failed"
        }

        for step in pending_steps:
            missing = [dep for dep in step.depends_on if dep not in existing_step_ids]
            failed = [dep for dep in step.depends_on if dep in failed_step_ids]
            blocked = [dep for dep in step.depends_on if dep not in done_step_ids]
            step.status = "failed"
            if missing:
                step.status_reason = f"依赖不存在: {missing}"
            elif failed:
                step.status_reason = f"依赖已失败: {failed}"
            else:
                step.status_reason = f"依赖阻塞或循环依赖: {blocked}"
            step.result_observation = step.status_reason


    def _apply_replan_steps(self, plan: Plan, raw_steps: list[dict]) -> None:
        history_steps = [
            step for step in plan.steps
            if step.status != "pending"
        ]
        if not isinstance(raw_steps, list) or any(not isinstance(row, dict) for row in raw_steps):
            raise ValueError("steps 必须是步骤对象数组")
        history = {step.step_id: step for step in history_steps}
        identifiers = []
        for row in raw_steps:
            identifier = row.get('step_id')
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError('每个新步骤必须包含非空字符串 step_id')
            if identifier in history or identifier in identifiers:
                raise ValueError(f'步骤 ID 重复或覆盖历史步骤: {identifier}')
            if not isinstance(row.get('title'), str) or not row['title'].strip():
                raise ValueError(f'步骤 {identifier} 缺少 title')
            if not isinstance(row.get('executor_id'), str) or row['executor_id'] not in self.executors:
                raise ValueError(f"步骤 {identifier} 的执行者不存在: {row.get('executor_id')}")
            if row.get('status', 'pending') != 'pending':
                raise ValueError(f'新步骤 {identifier} 必须为 pending')
            dependencies = row.get('depends_on', [])
            if not isinstance(dependencies, list) or any(not isinstance(dep, str) for dep in dependencies):
                raise ValueError(f'步骤 {identifier} 的 depends_on 必须是字符串数组')
            if len(set(dependencies)) != len(dependencies):
                raise ValueError(f'步骤 {identifier} 包含重复依赖')
            identifiers.append(identifier)
        candidates = set(identifiers)
        pending_dependencies = {}
        for row in raw_steps:
            identifier, dependencies = row['step_id'], row.get('depends_on', [])
            if identifier in dependencies:
                raise ValueError(f'步骤 {identifier} 不能依赖自身')
            missing = [dep for dep in dependencies if dep not in candidates and dep not in history]
            if missing:
                raise ValueError(f'步骤 {identifier} 依赖不存在或已被替换: {missing}；只引用保留历史或本次新步骤')
            blocked = [dep for dep in dependencies if dep in history and history[dep].status in {'failed', 'skipped'}]
            if blocked:
                raise ValueError(f'步骤 {identifier} 依赖不可执行的历史步骤: {blocked}')
            pending_dependencies[identifier] = set(dependencies) & candidates
        remaining = dict(pending_dependencies)
        while remaining:
            ready = {identifier for identifier, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError(f'候选计划包含循环依赖: {sorted(remaining)}')
            remaining = {identifier: deps - ready for identifier, deps in remaining.items() if identifier not in ready}
        replacement = _dict_to_plan({"steps": raw_steps})
        plan.steps = history_steps + replacement.steps
