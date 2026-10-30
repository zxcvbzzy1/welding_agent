"""Host policy adapters; agent_flow remains unaware of IM conversation settings."""
from __future__ import annotations

import copy

from application.services.agents import AgentFactoryService
from application.services.memory_runtime import RunMemoryCoordinator
from domain.run_context import current_run_id
from domain.context.providers import ContextProvider
from domain.agent.vlm import Vlm


class RunWorkspaceProvider(ContextProvider):
    name = "run_workspace"

    def __init__(self, workdir):
        self.content = f"## 本次任务工作目录\n{workdir}\n本次任务涉及的文件和命令使用此目录。"

    def get(self, state):
        return [self.content]


class IMAgentFactoryService(AgentFactoryService):
    def build_run_agent(self, agent_id):
        record = self.get_agent_record(agent_id)
        if record is None:
            raise KeyError(f"Agent 不存在: {agent_id}")
        record = copy.deepcopy(record)
        run = self._store.find_one("runs", {"run_id": current_run_id.get()}) or {}
        workdir = (run.get("run_options") or {}).get("workdir")
        if workdir:
            record.setdefault("metadata", {})["workdir"] = workdir
        engine = self._contexts.build_engine(record["context_id"])
        agent = self._construct_agent(record, engine)
        if workdir:
            agent.inject_attribute(work_path=workdir)
            agent.context_engine._providers.insert(0, RunWorkspaceProvider(workdir))
        if isinstance(agent, Vlm) and getattr(self, 'files', None) is not None:
            source = self._image_source_message()
            if source:
                agent.initial_images = self.files.image_references(source)
        return agent

    def _image_source_message(self):
        run = self._store.find_one('runs', {'run_id': current_run_id.get()}) or {}
        source_id = run.get('source_message_id') or (run.get('message_id') if run.get('kind') == 'dm_reply' else None)
        return self._store.find_one('im_messages', {'message_id': source_id}) if source_id else None

    def resolve_image_upload(self, file_id):
        source = self._image_source_message()
        if source is None or getattr(self, 'files', None) is None:
            raise ValueError('当前任务没有可访问的聊天图片来源')
        return self.files.resolve_image(file_id, source)


class IMRunMemoryCoordinator(RunMemoryCoordinator):
    async def archive(self, record):
        if not (record.get("run_options") or {}).get("memory_write_enabled", False):
            self._status(record["run_id"], memory_status="skipped", memory_reason="automatic_archiving_disabled")
            return
        await super().archive(record)
