from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from im_backend.application.services.messaging.actions import MessageActionService
from im_backend.application.services.messaging.agents import IMAgentService
from im_backend.application.services.platform.cleanup import IMCleanupService
from im_backend.application.services.platform.workspaces import WorkspaceService
from im_backend.application.services.messaging.conversations import ConversationService
from im_backend.application.services.platform.events import RoomEventStreamService
from im_backend.application.services.messaging.favorites import FavoriteService
from im_backend.application.services.messaging.messages import GroupMessageService
from im_backend.application.services.messaging.rooms import RoomService
from im_backend.application.services.orchestration.monitor import RunMonitorService
from im_backend.application.services.orchestration.runs import GroupRunService
from im_backend.application.services.orchestration.runtime_reply import PlannerFinalReplyWriter
from im_backend.infra.agent_flow_bridge.bridge import AgentFlowBridge


class IMService:
    def __init__(
        self,
        *,
        store,
        bridge: AgentFlowBridge,
        room_events: RoomEventStreamService,
        default_workdir: str | Path,
        files=None,
    ) -> None:
        self.files = files
        bridge.agents.files = files
        self._store = store
        self._bridge = bridge
        self._events = room_events
        self.cleanup = IMCleanupService(store, bridge.runtime, long_memory=getattr(bridge, "long_memory", None))
        self.favorites = FavoriteService(store=store, events=room_events)
        self.agents = IMAgentService(bridge=bridge, cleanup=self.cleanup)
        self.rooms = RoomService(store=store, bridge=bridge, events=room_events, cleanup=self.cleanup, agents=self.agents,
                                 default_workdir=str(Path(default_workdir).expanduser().resolve()))
        self.workspaces = WorkspaceService(self.rooms, Path(__file__).resolve().parents[3] / "upload" / "workspaces")
        self.messages = GroupMessageService(store=store, bridge=bridge, events=room_events, rooms=self.rooms, agents=self.agents, files=files)
        self.actions = MessageActionService(store=store, events=room_events)
        self.conversations = ConversationService(
            store=store,
            bridge=bridge,
            events=room_events,
            default_workdir=default_workdir,
            agents=self.agents,
            favorites=self.favorites,
            cleanup=self.cleanup,
            files=files,
        )
        self.runs = GroupRunService(
            store=store,
            bridge=bridge,
            events=room_events,
            rooms=self.rooms,
            messages=self.messages,
            agents=self.agents,
            favorites=self.favorites,
            default_workdir=default_workdir,
        )
        self.monitor = RunMonitorService(
            store=store,
            bridge=bridge,
            conversations=self.conversations,
            group_runs=self.runs,
        )
        # planner 的最终回复落库成房间消息，使其获得完整消息操作并可随群聊一起清理。
        self._planner_final_writer = PlannerFinalReplyWriter(store=store, messages=self.messages)
        bridge.events.subscribe(self._planner_final_writer.handle_event)
        bridge.events.subscribe(self._sync_run_message_states)
        bridge.events.subscribe(self._sync_human_question)
        room_events.subscribe(self._sync_run_message_states)

    @staticmethod
    def _question_message_id(confirmation_id, role):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f'human-question:{confirmation_id}:{role}'))

    async def _question_source(self, run_id):
        run = await asyncio.to_thread(self._store.find_one, 'runs', {'run_id': run_id})
        if not run:
            raise KeyError('运行不存在')
        source_id = run.get('source_message_id') or run.get('message_id')
        source = await asyncio.to_thread(self._store.find_one, 'im_messages', {'message_id': source_id}) if source_id else None
        if not source or source.get('sender_type') != 'user':
            raise KeyError('运行的发起消息不存在')
        return run, source

    async def _sync_human_question(self, event):
        if event.get('name') not in {'human.confirmation.requested', 'human.confirmation.resolved'}:
            return
        item = event.get('payload') or {}
        if item.get('kind') != 'question':
            return
        # Consult the shared fact; a delayed requested event must not reopen a card.
        current = await self._bridge.runtime.get_confirmation(item['confirmation_id'])
        await self._save_question_message(current or item)

    async def _save_question_message(self, item):
        try:
            run, source = await self._question_source(item['run_id'])
        except KeyError:
            return None
        question = self._bridge.human_confirmations.public_item(item)
        if question['status'] == 'pending' and (run.get('status') not in {'pending', 'running'} or
                                                source.get('run_id') != item['run_id']):
            question = {**question, 'status': 'cancelled', 'reason': '本次执行已结束'}
        metadata = {'source': 'human_question', 'question': question,
                    'owner_user_id': source['sender_id'], 'related_run_id': item['run_id']}
        message_id = self._question_message_id(item['confirmation_id'], 'question')
        metadata['question_key'] = message_id
        text = '\n\n'.join(q['question'] for q in item['questions'])
        record = dict(message_id=message_id, sender_type='agent', sender_id=item['agent_id'],
            content_parts=[{'type': 'text', 'text': text}], room_id=source.get('room_id', ''),
            conversation_id=source.get('conversation_id', ''), run_id=item['run_id'], status='sent',
            reply_to=source['message_id'], mentions=[], quote_of='', metadata=metadata,
            created_at=item['created_at'])
        # Insert once, then update only metadata so history order stays stable.
        await asyncio.to_thread(self._store.update_operators, 'im_messages', {'metadata.question_key': message_id},
                                {'$setOnInsert': record}, upsert=True)
        query = {'message_id': message_id}
        if question['status'] == 'pending':
            query['metadata.question.status'] = 'pending'
        record = await asyncio.to_thread(self._store.update_one, 'im_messages', query, {'metadata': metadata})
        if record is None:
            record = await asyncio.to_thread(self._store.find_one, 'im_messages', {'message_id': message_id})
        await self._events.publish(source.get('room_id') or source['conversation_id'],
            'message.created', {'message': record},
            event_id=self._question_message_id(item['confirmation_id'], 'card:' + record['metadata']['question']['status']))
        return record

    async def _check_question_execution(self, item, run, source):
        if run.get('status') not in {'pending', 'running'} or source.get('run_id') != item['run_id']:
            raise ValueError('本次执行已结束或已有新的执行，不能回答')
        control = await self._bridge.runtime.get_state(item['execution_kind'], item['run_id'])
        if (not control or control.get('revoked') or control.get('cancel_requested') or
                control.get('status') not in {'pending', 'running'} or
                control.get('execution_token') != item.get('execution_token')):
            raise ValueError('执行已取消或权限已撤销，不能回答')

    def _question_attachments(self, message):
        attachments = []
        for part in message.get('content_parts', []):
            if not part.get('file_id'):
                continue
            record = self.files.get(part['file_id'])
            info = {key: record[key] for key in ('file_id', 'original_name', 'mime_type', 'size')}
            try:
                self.files._available(record)
                info['path'] = str(self.files.storage.path(record))
            except (ValueError, OSError):
                info['unavailable'] = True
            attachments.append(info)
        return attachments

    async def _deliver_question_answer(self, item, message):
        attachments = await asyncio.to_thread(self._question_attachments, message)
        result = await self._bridge.human_confirmations.answer(run_id=item['run_id'],
            confirmation_id=item['confirmation_id'], answers=message['metadata']['answers'], attachments=attachments)
        scope = message.get('room_id') or message['conversation_id']
        await self._events.publish(scope, 'message.created', {'message': self._hydrate([message])[0]},
            event_id=self._question_message_id(item['confirmation_id'], 'answer-event'))
        return result

    async def answer_question(self, *, run_id, confirmation_id, user_id, answers, file_ids):
        from infra.tool.common_func import validate_question_answers
        from im_backend.application.services.file.service import FileAccessError

        run, source = await self._question_source(run_id)
        if source['sender_id'] != user_id:
            raise FileAccessError('只有发起本次任务的用户可以回答')
        item = await self._bridge.runtime.get_confirmation(confirmation_id)
        if not item or item.get('run_id') != run_id or item.get('kind') != 'question':
            raise KeyError('问题不存在')
        mid = self._question_message_id(confirmation_id, 'answer')
        saved = await asyncio.to_thread(self._store.find_one, 'im_messages', {'message_id': mid})
        if item.get('answer') is not None and saved:
            result = await self._deliver_question_answer(item, saved)
            return {'item': result, 'message': self._hydrate([saved])[0]}
        await self._check_question_execution(item, run, source)
        if item['status'] != 'pending':
            raise ValueError('问题已取消或已处理')
        if saved is None:
            canonical = validate_question_answers(item['questions'], answers)
            if len(file_ids) > 10 or len(set(file_ids)) != len(file_ids):
                raise ValueError('每次最多 10 个不同附件')
            text = '\n\n'.join(a['question'] + '\n回答：' + '；'.join(filter(None, [
                '、'.join(a['selected_options']), a['text']])) for a in canonical)
            message = dict(message_id=mid, sender_type='user', sender_id=user_id,
                room_id=source.get('room_id', ''), conversation_id=source.get('conversation_id', ''),
                content_parts=[{'type': 'text', 'text': text}] + [{'type': 'file', 'file_id': fid} for fid in file_ids],
                # Correlation lives in metadata: this is not another dispatchable task.
                run_id='', status='sent', reply_to=self._question_message_id(confirmation_id, 'question'),
                mentions=[], quote_of='', created_at=time.time(),
                metadata={'source': 'human_answer', 'confirmation_id': confirmation_id,
                          'related_run_id': run_id, 'answers': canonical, 'question_key': mid})
            saved = await asyncio.to_thread(self.files.save_message, message, user_id, first_write_wins=True)
        result = await self._deliver_question_answer(item, saved)
        return {'item': result, 'message': self._hydrate([saved])[0]}

    async def list_human_confirmations(self, run_id):
        """Reconcile saved answers and cards before the browser resumes its subscription."""
        try:
            run, source = await self._question_source(run_id)
        except KeyError:
            # Keep the pre-existing approval lookup usable for runs without IM messages.
            return await self._bridge.human_confirmations.list_pending(run_id)
        runtime = self._bridge.runtime
        for item in await runtime.confirmations(run_id):
            if item.get('kind') != 'question':
                continue
            saved = await asyncio.to_thread(self._store.find_one, 'im_messages', {
                'message_id': self._question_message_id(item['confirmation_id'], 'answer')})
            try:
                await self._check_question_execution(item, run, source)
            except ValueError:
                await self._bridge.human_confirmations.resolve(run_id=run_id,
                    confirmation_id=item['confirmation_id'], approved=False, reason='本次执行已结束')
            else:
                if saved:
                    await self._deliver_question_answer(item, saved)
            current = await runtime.get_confirmation(item['confirmation_id'])
            await self._save_question_message(current or item)
        # Resolved cards also need refreshing when a terminal event was missed.
        cards = await asyncio.to_thread(self._store.find_many, 'im_messages',
            {'run_id': run_id, 'metadata.source': 'human_question'})
        for card in cards:
            prior = card['metadata']['question']
            if prior.get('status') == 'pending':
                current = await runtime.get_confirmation(prior['confirmation_id'])
                if current and current.get('answer') is not None:
                    saved = await asyncio.to_thread(self._store.find_one, 'im_messages', {
                        'message_id': self._question_message_id(prior['confirmation_id'], 'answer')})
                    if saved:
                        await self._deliver_question_answer(current, saved)
                await self._save_question_message(current or prior)
        return await self._bridge.human_confirmations.list_pending(run_id)

    async def _sync_run_message_states(self, event):
        if event.get('name') not in {
            'task.updated', 'workflow.started', 'workflow.finished', 'workflow.failed',
            'run.cancelled', 'agent.reply.started', 'agent.reply.finished',
        }:
            return
        run_id = event.get('run_id') or event.get('payload', {}).get('run_id')
        if not run_id:
            return
        # Read the fact, rather than trusting a delayed/repeated event's status.
        record = await asyncio.to_thread(self._store.find_one, 'runs', {'run_id': run_id})
        if not record:
            return
        messages = await asyncio.to_thread(self._store.find_many, 'im_messages', {
            'run_id': run_id, 'sender_type': 'user',
        })
        for message in messages:
            await asyncio.to_thread(self._store.update_one, 'im_messages', {
                'message_id': message['message_id'], 'run_id': run_id,
                'status': {'$in': ['pending', 'sent', 'running']},
            }, {'status': record['status']})

    async def overlay_message_states(self, items):
        for item in items:
            if item.get("sender_type") != "user" or not item.get("run_id"):
                continue
            state = await self._bridge.runs.states.get(item["run_id"])
            if state:
                item.update(status=state["status"], cancel_requested=state.get("cancel_requested", False),
                            execution_health=state.get("execution_health"))
        return items

    def _hydrate(self, items):
        return self.files.hydrate(items) if self.files else items

    async def create_room(self, **kwargs) -> dict[str, Any]:
        return await self.rooms.create_room(**kwargs)

    def list_rooms(self) -> list[dict[str, Any]]:
        return self.rooms.list_rooms()

    def get_room(self, room_id: str) -> dict[str, Any]:
        return self.rooms.get_room(room_id)

    async def update_room(self, room_id: str, **kwargs) -> dict[str, Any]:
        return await self.rooms.update_room(room_id, **kwargs)

    async def delete_room(self, room_id: str) -> dict[str, Any]:
        return await self.rooms.delete_room(room_id)

    def list_messages(self, room_id: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return self._hydrate(self.messages.list_messages(room_id, conversation_id=conversation_id))

    def list_messages_window(
        self,
        room_id: str,
        *,
        conversation_id: str | None = None,
        limit: int | None = None,
        before_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        items, more = self.messages.list_messages_window(
            room_id, conversation_id=conversation_id, limit=limit, before_id=before_id
        )
        return self._hydrate(items), more

    def get_message(self, message_id: str) -> dict[str, Any]:
        return self._hydrate([self.messages.get_message(message_id)])[0]

    async def add_message(self, **kwargs) -> dict[str, Any]:
        return await self.messages.add_message(**kwargs)

    def list_agent_messages(self, agent_id: str, user_id: str = "") -> list[dict[str, Any]]:
        return self.messages.list_agent_messages(agent_id, user_id=user_id)

    async def list_room_tasks(self, room_id: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        return await self.runs.list_room_tasks(room_id, conversation_id=conversation_id)

    async def list_room_conversations(self, room_id: str, user_id: str = "") -> list[dict[str, Any]]:
        return await self.conversations.list_room_conversations(room_id, user_id=user_id)

    async def create_room_conversation(self, *, room_id: str, created_by: str = "", title: str = "") -> dict[str, Any]:
        return await self.conversations.create_room_conversation(
            room_id=room_id, created_by=created_by, title=title
        )

    def list_room_run_ids(self, room_id: str) -> list[str]:
        return self.runs.list_room_run_ids(room_id)

    async def dispatch_message(self, **kwargs) -> dict[str, Any]:
        return await self.runs.dispatch_message(**kwargs)

    async def cancel_room_run(self, **kwargs) -> dict[str, Any]:
        return await self.runs.cancel_room_run(**kwargs)

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.conversations.get_conversation(conversation_id)

    def list_conversation_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._hydrate(self.conversations.list_conversation_messages(conversation_id))

    def list_conversation_messages_window(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        before_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        items, more = self.conversations.list_conversation_messages_window(
            conversation_id, limit=limit, before_id=before_id
        )
        return self._hydrate(items), more

    def list_agent_conversations(self, agent_id: str, user_id: str = "") -> list[dict[str, Any]]:
        return self.conversations.list_agent_conversations(agent_id, user_id=user_id)

    def list_activity(self, user_id: str = "") -> list[dict[str, Any]]:
        return self.conversations.list_activity(user_id=user_id)

    async def create_agent_conversation(self, **kwargs) -> dict[str, Any]:
        return await self.conversations.create_agent_conversation(**kwargs)

    async def delete_conversation(self, conversation_id: str) -> dict[str, Any]:
        return await self.conversations.delete_conversation(conversation_id)

    async def update_conversation(self, conversation_id: str, **kwargs) -> dict[str, Any]:
        return await self.conversations.update_conversation(conversation_id, **kwargs)

    async def regenerate_conversation_reply(self, **kwargs) -> dict[str, Any]:
        return await self.conversations.regenerate_reply(**kwargs)

    # ── favorites / pinned context ────────────────────────────────
    def list_favorites(self, *, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        return self.favorites.list_favorites(scope_type=scope_type, scope_id=scope_id)

    async def create_favorite(self, **kwargs) -> dict[str, Any]:
        return await self.favorites.create_favorite(**kwargs)

    async def favorite_message(self, **kwargs) -> dict[str, Any]:
        return await self.favorites.favorite_message(**kwargs)

    async def update_favorite(self, favorite_id: str, **kwargs) -> dict[str, Any]:
        return await self.favorites.update_favorite(favorite_id, **kwargs)

    async def delete_favorite(self, favorite_id: str) -> dict[str, Any]:
        return await self.favorites.delete_favorite(favorite_id)

    def list_tools(self) -> list[dict[str, Any]]:
        return self.agents.list_tools()

    async def add_conversation_message(self, **kwargs) -> dict[str, Any]:
        return await self.conversations.add_conversation_message(**kwargs)

    async def reply_to_conversation_message(self, **kwargs) -> dict[str, Any]:
        return await self.conversations.reply_to_conversation_message(**kwargs)

    async def cancel_conversation_reply(self, **kwargs) -> dict[str, Any]:
        return await self.conversations.cancel_conversation_reply(**kwargs)

    async def reply_to_dm_message(self, *, room_id: str, message_id: str, auto_start: bool = True) -> dict[str, Any]:
        raise ValueError("单聊不再使用 room，请使用 /api/im/conversations/{conversation_id}/reply")

    def list_agents(self, user_id: str = "") -> list[dict[str, Any]]:
        return self.agents.list_visible_agents(user_id) if user_id else self.agents.list_agents()

    def list_contexts(self, user_id: str = "") -> list[dict[str, Any]]:
        return self.agents.list_visible_contexts(user_id) if user_id else self.agents.list_contexts()

    def create_agent(self, **kwargs) -> dict[str, Any]:
        return self.agents.create_agent(**kwargs)

    def update_agent(self, agent_id: str, **kwargs) -> dict[str, Any]:
        return self.agents.update_agent(agent_id, **kwargs)

    async def delete_agent(self, agent_id: str, *, user_id: str = "") -> dict[str, Any]:
        return await self.agents.delete_agent(agent_id, user_id=user_id)

    async def record_action(self, **kwargs) -> dict[str, Any]:
        return await self.actions.record_action(**kwargs)
