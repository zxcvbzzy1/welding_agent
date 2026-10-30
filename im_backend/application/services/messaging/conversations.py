from __future__ import annotations

import asyncio
import uuid
import time
from pathlib import Path
from typing import Any

from im_backend.application.services._shared.run_options import capture_run_options
from im_backend.application.services.messaging.agents import IMAgentService
from im_backend.application.services.platform.cleanup import IMCleanupService
from im_backend.application.services.platform.events import RoomEventStreamService
from im_backend.application.services.messaging.favorites import FavoriteService
from im_backend.application.services._shared.history import history_before
from im_backend.application.services._shared.lookup import find_im_message, require_im_message
from im_backend.application.services._shared.message_text import message_text
from im_backend.application.services._shared.pagination import fetch_message_window
from im_backend.application.services._shared.runtime_profile import build_runtime_profile
from im_backend.application.services._shared.inline_artifacts import collect_inline_artifact_parts
from im_backend.application.services._shared.prompting import compose_prompt_with_references
from im_backend.domain.models import AgentRuntimeProfile, ContentPart, Conversation, Message, now_ts
from im_backend.infra.agent_flow_bridge.bridge import AgentFlowBridge
from domain.memory.long.models import MemoryScope
from infra.runtime.control import ensure_execution_active, ExecutionRevoked


class ConversationService:
    """Single-agent conversation use cases."""

    def __init__(
        self,
        *,
        store,
        bridge: AgentFlowBridge,
        events: RoomEventStreamService,
        default_workdir: str | Path,
        agents: IMAgentService,
        favorites: FavoriteService,
        cleanup: IMCleanupService | None = None,
        files=None,
    ) -> None:
        self.files = files
        self._store = store
        self._bridge = bridge
        self._events = events
        self._agents = agents
        self._favorites = favorites
        self._default_workdir = str(Path(default_workdir).expanduser().resolve())
        self._reply_tasks: dict[str, asyncio.Task] = {}
        self.runtime = bridge.runtime
        self._cleanup = cleanup or IMCleanupService(store, bridge.runtime, long_memory=getattr(bridge, "long_memory", None))

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        conversation = self._store.find_one("im_conversations", {"conversation_id": conversation_id})
        if conversation is None:
            raise KeyError(f"对话不存在: {conversation_id}")
        return conversation

    def list_conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        self.get_conversation(conversation_id)
        return self._store.find_many(
            "im_messages",
            {"conversation_id": conversation_id},
            sort=[("created_at", 1)],
        )

    def list_conversation_messages_window(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        before_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """懒加载窗口：返回 (items, has_more)，只从数据库取窗口数据（不全量加载）。"""
        self.get_conversation(conversation_id)
        return fetch_message_window(
            self._store,
            {"conversation_id": conversation_id},
            limit=limit,
            before_id=before_id,
        )

    def list_agent_conversations(self, agent_id: str, user_id: str = "") -> list[dict[str, Any]]:
        if user_id:
            self._agents.ensure_agent_access(agent_id, user_id)
        else:
            self._bridge.ensure_agent_exists(agent_id)
        conversations: list[dict[str, Any]] = []
        records = self._store.find_many(
            "im_conversations",
            {"agent_id": agent_id},
            sort=[("updated_at", -1)],
        )
        for conversation in records:
            messages = self._store.find_many(
                "im_messages",
                {"conversation_id": conversation["conversation_id"]},
                sort=[("created_at", -1)],
                limit=1,
            )
            last_message = messages[0] if messages else None
            conversations.append(
                {
                    **conversation,
                    "agent_id": agent_id,
                    "pinned": bool(conversation.get("pinned", False)),
                    "archived": bool(conversation.get("archived", False)),
                    "last_message": last_message,
                    "message_count": len(self.list_conversation_messages(conversation["conversation_id"])),
                }
            )
        # store 已按 updated_at 倒序；这里稳定排序把置顶提到最前，组内顺序不变。
        conversations.sort(key=lambda item: not item.get("pinned", False))
        return conversations

    async def update_conversation(
        self,
        conversation_id: str,
        *,
        pinned: bool | None = None,
        archived: bool | None = None,
        title: str | None = None,
        memory_write_enabled: bool | None = None,
        user_id: str = "",
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if memory_write_enabled is not None and user_id and conversation.get("created_by") != user_id:
            raise KeyError("会话不存在")
        updates: dict[str, Any] = {}
        timestamp = now_ts()
        if memory_write_enabled is not None:
            updates["memory_write_enabled"] = memory_write_enabled
        if title is not None:
            cleaned = title.strip()
            if cleaned:
                updates["title"] = cleaned
        if pinned is not None:
            updates["pinned"] = bool(pinned)
            updates["pinned_at"] = timestamp if pinned else 0.0
        if archived is not None:
            updates["archived"] = bool(archived)
            updates["archived_at"] = timestamp if archived else 0.0
            # 归档时自动取消置顶。
            if archived and pinned is None:
                updates["pinned"] = False
                updates["pinned_at"] = 0.0
        if not updates:
            return self.get_conversation(conversation_id)
        record = self._store.update_one("im_conversations", {"conversation_id": conversation_id}, updates)
        record = record or self.get_conversation(conversation_id)
        await self._events.publish(conversation_id, "conversation.updated", {"conversation": record})
        return record

    async def regenerate_reply(
        self,
        *,
        conversation_id: str,
        message_id: str,
        auto_start: bool = True,
        user_id: str = "",
    ) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        message = self._get_message(message_id)
        if message.get("conversation_id") != conversation_id:
            raise ValueError("message 不属于该 conversation")
        if message.get("sender_type") != "user":
            raise ValueError("只能对用户消息重新生成回复")
        if message.get('metadata', {}).get('source') == 'human_answer':
            raise ValueError('问答回答不能作为新任务重新生成')

        # 取消可能仍在运行的旧回复任务，并等它完全结算（状态写回 cancelled、回调 pop）后再继续，
        # 否则被取消的旧任务会在新任务注册后才把用户消息改回 cancelled，并把新任务从 _reply_tasks 中 pop 掉。
        old_run_id = message.get("run_id") or message_id
        current = await self.runtime.get_state("dm_reply", old_run_id)
        if current and current["status"] in {"pending", "running"}:
            await self.runtime.request_cancel("dm_reply", old_run_id)
            for _ in range(20):
                await asyncio.sleep(0.1)
                current = await self.runtime.get_state("dm_reply", old_run_id)
                if not current or current["status"] not in {"pending", "running"}:
                    break
            else:
                raise ValueError("上一轮仍在取消中，请稍后重试")

        # 删除上一轮该用户消息触发的 agent 回复（含同条消息内的内联产物）。
        removed = 0
        for reply in self.list_conversation_messages(conversation_id):
            if reply.get("sender_type") != "agent":
                continue
            if ((reply.get("metadata") or {}).get("reply_to") == message_id
                    and (not reply.get("run_id") or reply["run_id"] == message.get("run_id"))):
                await self._cleanup.delete_message(reply)
                removed += 1

        # 重置用户消息状态并重新触发回复。
        reset = self._store.update_one("im_messages", {"message_id": message_id, "run_id": message.get("run_id")},
                                       {"status": "sent", "run_id": ""})
        if reset is None:
            raise ValueError("该消息已开始新的回复，请刷新后重试")
        await self._events.publish(
            conversation_id,
            "message.regenerated",
            {"message_id": message_id, "removed": removed},
        )
        result = await self.reply_to_conversation_message(
            conversation_id=conversation_id,
            message_id=message_id,
            auto_start=auto_start,
            user_id=user_id,
        )
        return {"type": "dm_regenerated", "message_id": message_id, "removed": removed, "reply": result}

    async def create_agent_conversation(
        self,
        *,
        agent_id: str,
        created_by: str,
        title: str = "",
        avatar_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        agent = self._agents.ensure_agent_access(agent_id, created_by)
        conversation = Conversation.create(
            agent_id=agent_id,
            title=title or f"{agent.get('name', agent_id)} 对话",
            created_by=created_by,
            avatar_url=avatar_url,
            metadata={"conversation_kind": "agent_dm", **(metadata or {})},
        )
        record = self._store.insert_one("im_conversations", conversation.to_dict())
        await self._events.publish(record["conversation_id"], "conversation.created", {"conversation": record})
        return record

    def list_activity(self, user_id: str = "") -> list[dict[str, Any]]:
        """Return a lightweight activity summary for every conversation (for unread badges)."""
        all_conversations = self._store.find_many("im_conversations", {})
        result: list[dict[str, Any]] = []
        for conversation in all_conversations:
            try:
                cid = conversation["conversation_id"]
                messages = self._store.find_many(
                    "im_messages",
                    {"conversation_id": cid},
                    sort=[("created_at", -1)],
                    limit=1,
                )
                last_message_at = messages[0]["created_at"] if messages else 0
                message_count = len(self.list_conversation_messages(cid))
                result.append({
                    "conversation_id": cid,
                    "room_id": conversation.get("room_id", "") or "",
                    "agent_id": conversation.get("agent_id", "") or "",
                    "message_count": message_count,
                    "last_message_at": last_message_at,
                    "updated_at": conversation.get("updated_at", 0),
                })
            except Exception:
                continue
        return result

    def _attach_last_message(self, conversation: dict) -> dict:
        """Return a new dict with last_message and message_count attached."""
        messages = self._store.find_many(
            "im_messages",
            {"conversation_id": conversation["conversation_id"]},
            sort=[("created_at", -1)],
            limit=1,
        )
        last_message = messages[0] if messages else None
        message_count = len(self.list_conversation_messages(conversation["conversation_id"]))
        return {**conversation, "last_message": last_message, "message_count": message_count}

    async def list_room_conversations(self, room_id: str, user_id: str = "") -> list[dict[str, Any]]:
        """列出某个群聊 room 下的所有会话（按 created_at 升序）。

        若该 room 还没有任何会话，则创建一条默认会话「默认对话」，并把所有 room_id 命中
        但 conversation_id 为空/缺失的历史消息回填到这条默认会话上，再连同它一起返回。
        """
        conversations = self._store.find_many(
            "im_conversations",
            {"room_id": room_id},
            sort=[("created_at", 1)],
        )
        if conversations:
            return [self._attach_last_message(c) for c in conversations]

        conversation = Conversation.create(
            room_id=room_id,
            title="默认对话",
            created_by=user_id or "user",
            metadata={"conversation_kind": "group", "default": True},
        )
        record = self._store.insert_one("im_conversations", conversation.to_dict())
        self._backfill_room_messages(room_id, record["conversation_id"])
        await self._events.publish(record["conversation_id"], "conversation.created", {"conversation": record})
        return [self._attach_last_message(record)]

    def _backfill_room_messages(self, room_id: str, conversation_id: str) -> None:
        """把 room 下尚未归属会话的历史消息挂到默认会话上。"""
        update_many = getattr(self._store, "update_many", None)
        if callable(update_many):
            for missing in ({"conversation_id": ""}, {"conversation_id": {"$exists": False}}):
                try:
                    update_many("im_messages", {"room_id": room_id, **missing}, {"conversation_id": conversation_id})
                except Exception:
                    pass
            return
        for message in self._store.find_many("im_messages", {"room_id": room_id}):
            if not message.get("conversation_id"):
                self._store.update_one(
                    "im_messages",
                    {"message_id": message["message_id"]},
                    {"conversation_id": conversation_id},
                )

    async def create_room_conversation(
        self,
        *,
        room_id: str,
        created_by: str = "user",
        title: str = "",
    ) -> dict[str, Any]:
        if self._store.find_one("im_rooms", {"room_id": room_id}) is None:
            raise KeyError(f"房间不存在: {room_id}")
        conversation = Conversation.create(
            room_id=room_id,
            title=title,
            created_by=created_by,
            metadata={"conversation_kind": "group"},
        )
        record = self._store.insert_one("im_conversations", conversation.to_dict())
        await self._events.publish(record["conversation_id"], "conversation.created", {"conversation": record})
        return record

    async def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        self.get_conversation(conversation_id)
        stats = await self._cleanup.delete_conversation(conversation_id)
        return {"deleted": True, "conversation_id": conversation_id, "stats": stats}

    async def add_conversation_message(
        self,
        *,
        conversation_id: str,
        sender_type: str,
        sender_id: str,
        content_parts: list[dict[str, Any]],
        user_id: str = "",
        reply_to: str = "",
        quote_of: str = "",
        run_id: str = "",
        status: str = "sent",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        if sender_type == "agent" and sender_id != conversation.get("agent_id"):
            raise ValueError("该 conversation 只能由绑定 agent 回复")
        parts = [ContentPart.from_dict(part) for part in content_parts]
        if not parts:
            raise ValueError("content_parts 不能为空")
        if sender_type == "user":
            metadata = capture_run_options(self._store, metadata, conversation_id=conversation_id,
                default_workdir=self._default_workdir,
                agent=self._bridge.ensure_agent_exists(conversation.get("agent_id", "")))
        message = Message.create(
            conversation_id=conversation_id,
            sender_type=sender_type,
            sender_id=sender_id,
            content_parts=parts,
            reply_to=reply_to,
            quote_of=quote_of,
            run_id=run_id,
            status=status,
            metadata=metadata or {},
        )
        record = (self.files.save_message(message.to_dict(), user_id or sender_id)
                  if self.files else self._store.insert_one("im_messages", message.to_dict()))
        self._store.update_one(
            "im_conversations",
            {"conversation_id": conversation_id},
            {"updated_at": record["created_at"]},
        )
        await self._events.publish(conversation_id, "message.created", {"message": record})
        return record

    async def reply_to_conversation_message(
        self,
        *,
        conversation_id: str,
        message_id: str,
        auto_start: bool = True,
        user_id: str = "",
    ) -> dict[str, Any]:
        conversation = self.get_conversation(conversation_id)
        message = self._get_message(message_id)
        if message.get("conversation_id") != conversation_id:
            raise ValueError("message 不属于该 conversation")
        if message.get("sender_type") != "user":
            raise ValueError("只能回复 user 消息")
        if message.get('metadata', {}).get('source') == 'human_answer':
            raise ValueError('问答回答不能作为新任务执行')

        agent_id = conversation.get("agent_id", "")
        # 触发前先校验 agent 存在（缺失会抛 KeyError），再决定是否启动回复任务。
        self._runtime_profile(agent_id)
        if not auto_start:
            await self._events.publish(conversation_id, "agent.reply.pending", {"message_id": message_id, "agent_id": agent_id})
            return {"type": "dm_reply_pending", "message_id": message_id, "agent_id": agent_id}

        previous_run_id = message.get("run_id")
        if previous_run_id:
            previous = self._store.find_one("runs", {"run_id": previous_run_id})
            if previous and previous.get("status") in {"pending", "running"}:
                return {"type": "dm_reply_started", "message_id": message_id,
                        "run_id": previous_run_id, "agent_id": agent_id}
        run_id = str(uuid.uuid4())
        fields = {
            "run_id": run_id, "kind": "dm_reply", "version": 2, "status": "pending",
            "memory_scope": MemoryScope(user_id, "direct", conversation_id).to_dict() if user_id else None,
            "user_question": message_text(message),
            "message_id": message_id, "source_message_id": message_id, "agent_id": agent_id,
            "agent_ids": [agent_id], "conversation_id": conversation_id,
            "conversation_title": conversation.get("title", ""), "room_id": "",
            "scope_id": conversation_id, "mode": "reply", "prompt": message_text(message)[:200],
        }
        options = (message.get("metadata") or {}).get("run_options")
        if options is None:
            options = capture_run_options(self._store, {}, conversation_id=conversation_id,
                default_workdir=self._default_workdir, agent=self._bridge.ensure_agent_exists(agent_id))["run_options"]
            self._store.update_one("im_messages", {"message_id": message_id},
                                   {"metadata": {**message.get("metadata", {}), "run_options": options}})
        fields.update(run_options=options, project_path=options["workdir"])
        await self._bridge.runs.states.create(fields)
        attached = self._store.update_one("im_messages", {
            "message_id": message_id, "run_id": previous_run_id,
        }, {"run_id": run_id, "status": "pending"})
        if attached is None:
            self._store.delete_one("runs", {"run_id": run_id})
            await self.runtime.cache.invalidate(run_id)
            winner = self._get_message(message_id)
            return {"type": "dm_reply_started", "message_id": message_id,
                    "run_id": winner.get("run_id"), "agent_id": agent_id}
        try:
            if not await self.runtime.claim("dm_reply", run_id, fields):
                raise ValueError("回复已启动")
            await self._update_reply(message_id, {"status": "running", "started_at": time.time()}, run_id=run_id)
            await self._events.publish(conversation_id, "agent.reply.started", fields)
        except Exception:
            await self._bridge.runs.states.update(run_id, {"status": "failed", "finished_at": time.time()})
            await self._bridge.runs.states.finish_control(run_id)
            raise
        task = asyncio.create_task(self._run_reply_task(
            conversation_id=conversation_id, message_id=message_id, agent_id=agent_id, run_id=run_id))
        self._reply_tasks[run_id] = task
        self.runtime.track("dm_reply", run_id, task)
        task.add_done_callback(lambda finished, rid=run_id: self._reply_tasks.pop(rid, None)
                               if self._reply_tasks.get(rid) is finished else None)
        return {"type": "dm_reply_started", "message_id": message_id, "run_id": run_id, "agent_id": agent_id}

    def _runtime_profile(self, agent_id: str) -> AgentRuntimeProfile:
        record = self._bridge.ensure_agent_exists(agent_id)
        return build_runtime_profile(record, default_workdir=self._default_workdir)

    async def list_active_replies(self):
        records = self._store.find_many("runs", {"kind": "dm_reply", "status": {"$in": ["pending", "running"]}})
        return [{**record, "cancel_requested": (await self._bridge.runs.states.get(record["run_id"]))["cancel_requested"]}
                for record in records]

    async def cancel_conversation_reply(self, *, conversation_id, message_id, run_id=None):
        self.get_conversation(conversation_id)
        message = self._get_message(message_id)
        if message.get("conversation_id") != conversation_id or message.get("sender_type") != "user":
            raise ValueError("只能取消当前会话的用户消息回复")
        run_id = run_id or message.get("run_id")
        record = self._store.find_one("runs", {"run_id": run_id})
        if not record or record.get("message_id") != message_id:
            raise KeyError("回复执行不存在")
        requested = False
        if record["status"] in {"pending", "running"}:
            control = await self.runtime.get_state("dm_reply", run_id)
            if control and control.get("owner_worker"):
                requested = bool((await self.runtime.request_cancel("dm_reply", run_id)).get("cancel_requested"))
            else:
                await self._mark_reply_cancelled(conversation_id=conversation_id, message_id=message_id,
                                                 agent_id=record["agent_id"], run_id=run_id, publish=True)
                await self._bridge.runs.states.finish_control(run_id)
        return {"type": "dm_reply_cancel_requested", "message_id": message_id, "run_id": run_id,
                "agent_id": record.get("agent_id"), "cancel_requested": requested}

    async def _update_reply(self, message_id, changes, *, run_id=None):
        run_id = run_id or self._get_message(message_id).get("run_id")
        await self._bridge.runs.states.update(run_id, changes)

    async def _run_reply_task(self, *, conversation_id, message_id, agent_id, run_id):
        from domain.run_context import current_run_id
        token = current_run_id.set(run_id)
        revoked = False
        try:
            with self.runtime.execution_context("dm_reply", run_id):
                await ensure_execution_active()
                agent = self._bridge.agents.build_run_agent(agent_id)
                record = self._store.find_one("runs", {"run_id": run_id})
                memory = await self._bridge.runs.memory_coordinator.prepare(record)
                await self._bridge.runs.memory_coordinator.inject(agent, memory, run_id)
                message = self._get_message(message_id)
                self._rebuild_agent_history(agent, conversation_id=conversation_id, before_message_id=message_id)
                prompt = self._compose_prompt(message)
                # The per-run IM factory already applied the immutable workdir snapshot.
                self._apply_pinned_context(agent, conversation_id)
                await self._events.publish(conversation_id, "workflow.started", {
                    "scope_id": conversation_id, "conversation_id": conversation_id, "run_id": run_id,
                    "message_id": message_id, "agent_id": agent_id, "mode": "direct", "prompt": prompt})
                started_at = time.time()
                async with self._bridge.events.execution(run_id, agent):
                    async with self._bridge.agents.image_runtime.scope():
                        await agent.start_with_history(prompt)
                await ensure_execution_active()
                final = agent.states.get("final", "") or agent.states.get("finish_reason", "")
                parts = [{"type": "text", "text": final or "Agent 已完成回复"}]
                parts.extend(collect_inline_artifact_parts(
                    await self._bridge.list_run_events(run_id), since=started_at, run_id=run_id))
                await self._update_reply(message_id, {"status": "finished", "final": final,
                                                       "finished_at": time.time(), "cancel_requested": False}, run_id=run_id)
                reply = await self.add_conversation_message(
                    conversation_id=conversation_id, sender_type="agent", sender_id=agent_id,
                    content_parts=parts, status="finished", run_id=run_id,
                    metadata={"source": "direct_agent_reply", "reply_to": message_id, "run_id": run_id})
                await self._events.publish(conversation_id, "agent.reply.finished", {
                    "message_id": message_id, "agent_id": agent_id, "run_id": run_id, "reply": reply})
                await self._events.publish(conversation_id, "workflow.finished", {
                    "scope_id": conversation_id, "conversation_id": conversation_id, "run_id": run_id,
                    "message_id": message_id, "agent_id": agent_id, "mode": "direct", "final": final})
        except ExecutionRevoked:
            revoked = True
        except asyncio.CancelledError as exc:
            revoked = exc.args in {('execution_revoked',), ('worker_shutdown',)}
            current = self._store.find_one('runs', {'run_id': run_id}) or {}
            if not revoked and current.get('status') in {'pending', 'running'}:
                await self._mark_reply_cancelled(conversation_id=conversation_id, message_id=message_id,
                                                 agent_id=agent_id, publish=True, run_id=run_id)
            raise
        except Exception as exc:
            await self._update_reply(message_id, {"status": "failed", "error": str(exc),
                                                   "finished_at": time.time()}, run_id=run_id)
            await self._events.publish(conversation_id, "workflow.failed", {
                "scope_id": conversation_id, "conversation_id": conversation_id, "run_id": run_id,
                "message_id": message_id, "agent_id": agent_id, "mode": "direct", "error": str(exc)})
            raise
        finally:
            current_run_id.reset(token)
            if not revoked:
                await self._bridge.runs.states.finish_control(run_id)

    async def _mark_reply_cancelled(self, *, conversation_id, message_id, agent_id, publish,
                                    reason="用户中断", run_id=None):
        run_id = run_id or self._get_message(message_id).get("run_id")
        await self._update_reply(message_id, {"status": "cancelled", "cancel_reason": reason,
                                               "finished_at": time.time(), "cancel_requested": False}, run_id=run_id)
        if publish:
            await self._events.publish(conversation_id, "workflow.failed", {
                "scope_id": conversation_id, "conversation_id": conversation_id, "run_id": run_id,
                "message_id": message_id, "agent_id": agent_id, "mode": "direct",
                "error": reason, "cancelled": True})

    def _get_message(self, message_id: str) -> dict[str, Any]:
        return require_im_message(self._store, message_id)

    def _apply_pinned_context(self, agent, conversation_id: str) -> None:
        """把当前会话的收藏作为固定上下文写入 agent state。"""
        states = getattr(agent, "states", None)
        if isinstance(states, dict):
            states["pinned_context"] = self._favorites.context_items("conversation", conversation_id)

    def _rebuild_agent_history(self, agent, *, conversation_id: str, before_message_id: str) -> None:
        memory = agent.context_engine.get_memory()
        memory.clear_field("tool_respond")
        memory.clear_field("agent_history")
        for history_message in self._conversation_history_before(conversation_id, before_message_id):
            memory.store("agent_history", "dialogue", self._format_history_message(history_message))

    def _conversation_history_before(self, conversation_id: str, message_id: str) -> list[dict[str, Any]]:
        return history_before(self.list_conversation_messages(conversation_id), message_id)

    def _format_history_message(self, message: dict[str, Any]) -> str:
        role = "用户" if message.get("sender_type") == "user" else "Agent"
        return f"### 历史消息\n{role}：{self._context_text(message)}"

    def _context_text(self, message: dict) -> str:
        return self.files.context_text(message) if self.files else message_text(message)

    def _compose_prompt(self, message: dict[str, Any]) -> str:
        """拼接被回复/引用消息的上下文，让 agent 看到完整意图。"""
        return compose_prompt_with_references(
            message,
            lookup=lambda mid: find_im_message(self._store, mid),
            text_of=self._context_text,
            reference_text_of=(lambda ref: self.files.context_text(ref, reference=True)) if self.files else None,
        )

    def _apply_agent_workdir(self, agent, agent_id: str) -> None:
        """把 agent 的工作目录落到运行实例的 work_path（按 profile 取，含默认值）。

        native Agent 按 profile.workdir 落盘。
        """
        profile = self._runtime_profile(agent_id)
        if not profile.workdir:
            return
        if hasattr(agent, "inject_attribute"):
            agent.inject_attribute(work_path=profile.workdir)
        elif hasattr(agent, "work_path"):
            agent.work_path = profile.workdir
