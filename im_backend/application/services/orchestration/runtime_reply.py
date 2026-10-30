from __future__ import annotations

from typing import Any

from im_backend.application.services.messaging.messages import GroupMessageService


class PlannerFinalReplyWriter:
    """把群聊 run 的 planner.final 运行事件物化成房间里的 agent 回复消息。

    原生群聊 run 只会发出 planner.final 运行事件 + 写一份 agent_flow 运行态消息，
    planner 的最终回复并不存在于房间 im_messages 里，因此前端无法对它做收藏/回复/引用，
    解散群聊时也无从按 room_id 清理。这里订阅 agent_flow 的事件流，在 planner.final 时
    按 run_id 反查派发的用户消息，落一条 sender_type=agent 的房间消息（携带 reply_to/run_id），
    前端即可把它当普通气泡渲染并获得完整消息操作。
    """

    def __init__(self, *, store, messages: GroupMessageService) -> None:
        self._store = store
        self._messages = messages

    async def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("name") not in {"planner.final", "agent.final"}:
            return
        payload = event.get("payload") or {}
        if payload.get('temporary'):
            return
        run_id = event.get("run_id") or payload.get("run_id") or ""
        final = (payload.get("final") or "").strip()
        if not run_id or not final:
            return

        # 按 run_id 反查派发的用户消息，拿到 room_id 与回复目标。
        user_message = self._store.find_one(
            "im_messages", {"run_id": run_id, "sender_type": "user"}
        )
        if not user_message:
            return
        room_id = user_message.get("room_id")
        if not room_id:
            return

        source = "planner_final" if event["name"] == "planner.final" else "agent_final"
        # A repeated delivery must not create another reply; distinct executions may reply.
        for message in self._store.find_many(
            "im_messages", {"run_id": run_id, "sender_type": "agent"}
        ):
            metadata = message.get("metadata") or {}
            if metadata.get("source_event_id") == event["event_id"]:
                return

        planner_id = payload.get("planner_id") or payload.get("agent_id") or event.get("agent_id") or "default_planner"
        await self._messages.add_message(
            room_id=room_id,
            conversation_id=user_message.get("conversation_id", ""),
            sender_type="agent",
            sender_id=planner_id,
            content_parts=[{"type": "text", "text": final}],
            reply_to=user_message.get("message_id", ""),
            run_id=run_id,
            status="finished",
            metadata={"source": source, "run_id": run_id, "source_event_id": event["event_id"],
                      "execution_id": event.get("execution_id", "")},
        )
