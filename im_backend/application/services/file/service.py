from __future__ import annotations

import copy
import asyncio
import logging
import json
import mimetypes
import threading
import time
from typing import BinaryIO
from uuid import UUID, uuid4

from im_backend.application.services._shared.message_text import message_text
from im_backend.infra.storage.files import LocalFileStorage

logger = logging.getLogger(__name__)
MAX_ATTACHMENTS = 10


class FileAccessError(ValueError):
    pass


class FileGoneError(ValueError):
    pass


class FileService:
    def __init__(self, store, storage: LocalFileStorage, events=None):
        self.store = store
        self.storage = storage
        self.events = events
        self._lock = threading.RLock()

    def ensure_indexes(self):
        self.store.ensure_index('im_messages', [('metadata.question_key', 1)], unique=True,
                                partialFilterExpression={'metadata.question_key': {'$type': 'string'}})
        for collection, keys, unique in [
            ('im_files', [('file_id', 1)], True),
            ('im_files', [('owner_user_id', 1), ('status', 1), ('created_at', -1), ('file_id', -1)], False),
            ('im_files', [('status', 1), ('expires_at', 1)], False),
            ('im_conversation_files', [('relation_id', 1)], True),
            ('im_conversation_files', [('file_id', 1), ('message_id', 1)], True),
            ('im_conversation_files', [('message_id', 1)], False),
            ('im_conversation_files', [('conversation_id', 1)], False),
            ('im_conversation_files', [('room_id', 1)], False),
        ]:
            self.store.ensure_index(collection, keys, unique=unique)
        self.store.ensure_index('im_messages', [('sender_id', 1), ('metadata.client_message_id', 1)],
                                unique=True, partialFilterExpression={
                                    'sender_type': 'user', 'metadata.client_message_id': {'$type': 'string'}})

    def upload(self, user_id: str, filename: str, mime_type: str, stream: BinaryIO) -> dict:
        file_id = str(uuid4())
        path, size = self.storage.save(file_id, filename, stream)
        record = dict(file_id=file_id, owner_user_id=user_id, original_name=filename or 'file.bin',
                      mime_type=mime_type or 'application/octet-stream', size=size, storage_path=path,
                      status='pending', expires_at=time.time() + 86400)
        try:
            return self.store.insert_one('im_files', record)
        except Exception:
            # In an ambiguous write failure, recover the metadata by the UUID on restart.
            try:
                self.storage.delete(record)
            except OSError:
                logger.exception('Could not roll back uploaded file %s', file_id)
            raise

    def get(self, file_id: str) -> dict:
        record = self.store.find_one('im_files', {'file_id': file_id})
        if record is None:
            raise KeyError('文件不存在')
        return record

    def list_owned(self, user_id: str, limit: int = 50, before: str = '') -> dict:
        query = {'owner_user_id': user_id, 'status': 'attached', 'expires_at': None}
        if before:
            cursor = self.get(before)
            if cursor['owner_user_id'] != user_id:
                raise FileAccessError('不能使用其他用户的文件游标')
            query['$or'] = [
                {'created_at': {'$lt': cursor['created_at']}},
                {'created_at': cursor['created_at'], 'file_id': {'$lt': before}},
            ]
        records = self.store.find_many('im_files', query, sort=[('created_at', -1), ('file_id', -1)], limit=limit + 1)
        items = records[:limit]
        return {'items': items, 'has_more': len(records) > limit,
                'next_cursor': items[-1]['file_id'] if items and len(records) > limit else ''}

    def _available(self, record: dict):
        if record['status'] not in {'pending', 'attached'}:
            raise FileGoneError('文件已删除或正在删除')
        if record['status'] == 'pending' and record.get('expires_at', 0) <= time.time():
            raise FileGoneError('暂存文件已过期，请重新上传')
        if not self.storage.path(record).is_file():
            raise FileGoneError('实体文件不存在')

    def download(self, file_id: str, user_id: str):
        record = self.get(file_id)
        if record['status'] == 'pending' and record['owner_user_id'] != user_id:
            raise FileAccessError('暂存文件仅上传者可访问')
        self._available(record)
        return record, self.storage.path(record)

    def part(self, record: dict) -> dict:
        return dict(type='file', file_id=record['file_id'], name=record['original_name'],
                    mime_type=record['mime_type'], size=record['size'],
                    url=f"/api/im/files/{record['file_id']}/download",
                    metadata={'owner_user_id': record['owner_user_id'], 'file_status': record['status']})

    def save_message(self, message: dict, user_id: str, *, first_write_wins: bool = False) -> dict:
        """Persist attachment references before publishing a message; compensate failed writes.

        expires_at stays set until the message commits. The sweeper can reconcile a
        process crash without a Mongo replica set / multi-document transactions.
        """
        with self._lock:
            metadata = message.get('metadata') or {}
            if not first_write_wins and ('question_key' in metadata or
                    metadata.get('source') in ('human_question', 'human_answer')):
                raise ValueError('问答消息元数据只能由问答接口生成')
            if first_write_wins:
                previous = self.store.find_one('im_messages', {'message_id': message['message_id']})
                if previous:
                    self._retain_message_files(previous)
                    return self.hydrate([previous])[0]
            retry_query = None
            client_id = (message.get('metadata') or {}).get('client_message_id')
            if client_id is not None:
                if message['sender_type'] != 'user' or not isinstance(client_id, str):
                    raise ValueError('client_message_id 仅用于用户消息')
                UUID(client_id)
                retry_query = {'sender_id': message['sender_id'], 'metadata.client_message_id': client_id}
                previous = self.store.find_one('im_messages', retry_query)
                if previous:
                    self._check_retry(previous, message)
                    return self.hydrate([previous])[0]
            parts = message.get('content_parts', [])
            file_parts = [p for p in parts if p.get('file_id')]
            if len([p for p in parts if p.get('type') == 'file']) > MAX_ATTACHMENTS:
                raise ValueError('每条消息最多 10 个附件')
            ids = [p['file_id'] for p in file_parts]
            if len(ids) != len(set(ids)):
                raise ValueError('同一条消息不能重复引用同一文件')
            files = {}
            for part in file_parts:
                if part.get('type') != 'file' or message['sender_type'] != 'user' or not user_id:
                    raise FileAccessError('聊天附件必须由登录用户发送')
                record = self.get(part['file_id'])
                if record['owner_user_id'] != user_id:
                    raise FileAccessError('只能发送自己上传的文件')
                self._available(record)
                files[record['file_id']] = record
            # Legacy agent artifact parts remain valid, but user-supplied local paths are never resolved.
            message = copy.deepcopy(message)
            message['content_parts'] = [self.part(files[p['file_id']]) if p.get('file_id') else p for p in parts]
            for part in message['content_parts']:
                if part.get('file_id'):
                    part['metadata']['file_status'] = 'attached'
            committed = False
            try:
                for record in files.values():
                    relation = dict(
                        relation_id=str(uuid4()), file_id=record['file_id'], message_id=message['message_id'],
                        conversation_id=message.get('conversation_id', ''), room_id=message.get('room_id', ''))
                    if first_write_wins:
                        self.store.update_operators('im_conversation_files',
                            {'file_id': record['file_id'], 'message_id': message['message_id']},
                            {'$setOnInsert': relation}, upsert=True)
                    else:
                        self.store.insert_one('im_conversation_files', relation)
                    claimed = self.store.update_one('im_files',
                        {'file_id': record['file_id'], 'status': {'$in': ['pending', 'attached']}},
                        {'status': 'attached'})
                    if claimed is None:
                        raise FileGoneError('文件正在删除，请移除附件后重试')
                result = self.store.insert_one('im_messages', message)
                committed = True
            except Exception:
                # A failed acknowledgement may still have committed the message.
                existing = self.store.find_one('im_messages', {'message_id': message['message_id']})
                if existing is None and retry_query:
                    existing = self.store.find_one('im_messages', retry_query)
                    if existing:
                        self.store.delete_many('im_conversation_files', {'message_id': message['message_id']})
                        self._check_retry(existing, message)
                if existing:
                    result, committed = existing, True
                else:
                    if first_write_wins:
                        # Another submitter may be committing the same deterministic
                        # message ID. Leave references for retry/sweeper reconciliation.
                        raise
                    self.store.delete_many('im_conversation_files', {'message_id': message['message_id']})
                    for record in files.values():
                        if record['status'] == 'pending':
                            refs = self.store.find_many('im_conversation_files', {'file_id': record['file_id']})
                            if not refs:
                                self.store.update_one('im_files', {'file_id': record['file_id'], 'status': 'attached'},
                                                      {'status': 'pending', 'expires_at': record['expires_at']})
                    raise
            if committed:
                if first_write_wins:
                    self._retain_message_files(result)
                    retained = {p.get('file_id') for p in result.get('content_parts', [])}
                    files = {key: value for key, value in files.items() if key in retained}
                for record in files.values():
                    try:
                        self.store.update_one('im_files', {'file_id': record['file_id'], 'status': 'attached'},
                                              {'expires_at': None})
                    except Exception:
                        logger.exception('File activation will be recovered: %s', record['file_id'])
                return result

    def _retain_message_files(self, message):
        """Remove losing concurrent submissions' references, keeping the saved answer."""
        retained = {p.get('file_id') for p in message.get('content_parts', [])}
        for relation in self.store.find_many('im_conversation_files', {'message_id': message['message_id']}):
            if relation['file_id'] in retained:
                continue
            self.store.delete_one('im_conversation_files', {'relation_id': relation['relation_id']})
            if not self.store.find_one('im_conversation_files', {'file_id': relation['file_id']}):
                self.store.update_one('im_files', {'file_id': relation['file_id'], 'status': 'attached'},
                                      {'status': 'pending', 'expires_at': time.time() + 86400})

    def _check_retry(self, previous: dict, message: dict):
        def shape(item):
            parts = [({'type': 'file', 'file_id': p['file_id']} if p.get('file_id') else p)
                     for p in item.get('content_parts', [])]
            return (item.get('room_id', ''), item.get('conversation_id', ''), parts,
                    item.get('mentions', []), item.get('reply_to', ''), item.get('quote_of', ''))
        if shape(previous) != shape(message):
            raise ValueError('此消息已提交，不能用同一 client_message_id 更改内容；请刷新聊天')

    def hydrate(self, messages: list[dict]) -> list[dict]:
        items = copy.deepcopy(messages)
        ids = list({p['file_id'] for m in items for p in m.get('content_parts', []) if p.get('file_id')})
        if not ids:
            return items
        records = {r['file_id']: r for r in self.store.find_many('im_files', {'file_id': {'$in': ids}})}
        for msg in items:
            for part in msg.get('content_parts', []):
                if part.get('file_id'):
                    record = records.get(part['file_id'])
                    if record:
                        part.update(self.part(record))
                    else:
                        part['metadata'] = {**part.get('metadata', {}), 'file_status': 'delete'}
        return items

    def context_text(self, message: dict, *, reference: bool = False) -> str:
        paths = []
        for part in message.get('content_parts', []):
            file_id = part.get('file_id')
            if not file_id or part.get('type') != 'file':
                continue
            relation = self.store.find_one('im_conversation_files', {
                'file_id': file_id, 'message_id': message['message_id'],
                'conversation_id': message.get('conversation_id', ''), 'room_id': message.get('room_id', '')})
            record = self.store.find_one('im_files', {'file_id': file_id, 'status': 'attached'}) if relation else None
            if record and self.storage.path(record).is_file():
                if not self._image_part(part, record):
                    paths.append(record['storage_path'])
        text = message_text(message)
        if reference and len(text) > 2000:
            text = text[:2000] + '\n…（引用内容已截断）'
        images = self.image_references(message, include_references=False)
        image_text = '图片引用：' + json.dumps(images, ensure_ascii=False) if images else ''
        return '\n\n'.join(filter(None, [text, '\n'.join(paths), image_text]))

    @staticmethod
    def _image_part(part, record=None):
        record = record or {}
        mime = record.get('mime_type') or part.get('mime_type') or ''
        guessed = mimetypes.guess_type(record.get('original_name') or part.get('name') or '')[0] or ''
        return mime.startswith('image/') or guessed.startswith('image/')

    def image_references(self, message, *, include_references=True):
        """Image sources are references, including in ordinary text-only history."""
        messages = [message]
        if include_references:
            for key in ('reply_to', 'quote_of'):
                ref = self.store.find_one('im_messages', {'message_id': message.get(key)}) if message.get(key) else None
                if ref and all(ref.get(k, '') == message.get(k, '') for k in ('room_id', 'conversation_id')):
                    messages.append(ref)
        sources = []
        for item in messages:
            for part in item.get('content_parts', []):
                if part.get('type') == 'file' and part.get('file_id'):
                    record = self.store.find_one('im_files', {'file_id': part['file_id']})
                    if self._image_part(part, record):
                        sources.append({'kind': 'upload', 'value': part['file_id']})
                else:
                    image = (part.get('metadata') or {}).get('artifact', part)
                    if not isinstance(image, dict) or image.get('type') != 'image':
                        continue
                    path = image.get('file_path') or (image.get('metadata') or {}).get('file_path')
                    if path:
                        sources.append({'kind': 'path', 'value': path})
                    elif image.get('url', '').startswith(('https://', 'http://')):
                        sources.append({'kind': 'url', 'value': image['url']})
        return [dict(pair) for pair in dict.fromkeys(tuple(source.items()) for source in sources)]

    def resolve_image(self, file_id, source_message):
        relation = self.store.find_one('im_conversation_files', {
            'file_id': file_id, 'conversation_id': source_message.get('conversation_id', ''),
            'room_id': source_message.get('room_id', ''),
        })
        if relation is None:
            raise FileAccessError('图片不属于当前会话')
        record = self.get(file_id)
        self._available(record)
        if record['status'] != 'attached':
            raise FileAccessError('图片尚未关联到消息')
        return self.storage.path(record)

    async def delete(self, file_id: str, user_id: str) -> dict:
        record = await asyncio.to_thread(self._delete_sync, file_id, user_id)
        await self._publish_deleted(file_id)
        return record

    def _delete_sync(self, file_id: str, user_id: str) -> dict:
        with self._lock:
            record = self.get(file_id)
            if record['owner_user_id'] != user_id:
                raise FileAccessError('只有上传者可以删除文件')
            if record['status'] == 'delete':
                return record
            record = self.store.update_one('im_files', {'file_id': file_id}, {'status': 'deleting'})
            self.storage.delete(record)
            return self.store.update_one('im_files', {'file_id': file_id}, {'status': 'delete', 'expires_at': None})

    async def _publish_deleted(self, file_id):
        if not self.events:
            return
        refs = self.store.find_many('im_conversation_files', {'file_id': file_id})
        scopes = {r.get('room_id') or r.get('conversation_id') for r in refs}
        for scope in filter(None, scopes):
            try:
                await self.events.publish(scope, 'file.deleted', {'file_id': file_id})
            except Exception:
                logger.exception('Could not publish file deletion %s', file_id)

    async def sweep(self):
        deleted = await asyncio.to_thread(self._sweep_sync)
        for file_id in deleted:
            await self._publish_deleted(file_id)

    def _sweep_sync(self):
        deleted = []
        with self._lock:
            # Activation interrupted after message commit: recover before expiring anything.
            for record in self.store.find_many('im_files', {'status': 'attached', 'expires_at': {'$ne': None}}):
                refs = self.store.find_many('im_conversation_files', {'file_id': record['file_id']})
                live = [r for r in refs if self.store.find_one('im_messages', {'message_id': r['message_id']})]
                if live:
                    self.store.update_one('im_files', {'file_id': record['file_id'], 'status': 'attached'}, {'expires_at': None})
                elif record.get('expires_at', 0) <= time.time():
                    self.store.delete_many('im_conversation_files', {'file_id': record['file_id']})
                    self.store.update_one('im_files', {'file_id': record['file_id'], 'status': 'attached'}, {'status': 'pending'})
            candidates = self.store.find_many('im_files', {'status': 'deleting'})
            candidates += self.store.find_many('im_files', {'status': 'pending', 'expires_at': {'$lte': time.time()}})
            for record in candidates:
                try:
                    self._delete_sync(record['file_id'], record['owner_user_id'])
                    deleted.append(record['file_id'])
                except Exception:
                    logger.exception('File cleanup will retry: %s', record['file_id'])

            self.storage.sweep_orphans(lambda fid: self.store.find_one('im_files', {'file_id': fid}) is not None)

        return deleted
