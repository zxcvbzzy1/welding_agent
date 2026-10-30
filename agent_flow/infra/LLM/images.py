"""Task-scoped DeepSeek Files API uploads; binary data never enters agent context."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import hashlib
import ipaddress
import io
import logging
from pathlib import Path
import socket
from urllib.parse import urlparse

import httpx
from PIL import Image

from domain.agent.vlm import ImageReference
from infra.runtime.control import ensure_execution_active

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 64 * 1024 * 1024
IMAGE_SCHEMA = {
    "type": "array", "minItems": 1,
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"kind": {"type": "string", "enum": ["upload", "path", "url"]},
                             "value": {"type": "string", "minLength": 1}},
              "required": ["kind", "value"]},
}
_current_session: ContextVar[ImageSession | None] = ContextVar("image_session", default=None)


class ImageSession:
    def __init__(self, runtime):
        self.runtime = runtime
        self.files: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def load(self, images, work_path):
        results = []
        for source in images:
            await ensure_execution_active()
            ref = ImageReference.parse(source)
            raw = await self.runtime.read(ref, work_path)
            mime, suffix = await asyncio.to_thread(self.runtime.validate, raw)
            digest = hashlib.sha256(raw).hexdigest()
            # Concurrent delegates share one upload for identical bytes.
            async with self._lock:
                if digest not in self.files:
                    uploaded = await self.runtime.client.client.files.create(
                        file=(f"image.{suffix}", raw, mime), purpose="user_data",
                        expires_after={"anchor": "created_at", "seconds": 86400},
                    )
                    self.files[digest] = uploaded.id
                results.append(self.files[digest])
            await ensure_execution_active()
        return results

    async def close(self):
        for file_id in set(self.files.values()):
            try:
                await asyncio.wait_for(self.runtime.client.client.files.delete(file_id), timeout=10)
            except Exception:
                log.warning("Could not delete remote image %s; expiration remains enabled", file_id, exc_info=True)
        self.files.clear()


class ImageRuntime:
    def __init__(self, client, *, resolve_upload=None):
        self.client = client
        self.resolve_upload = resolve_upload

    @staticmethod
    def current() -> ImageRuntime | None:
        session = _current_session.get()
        return session.runtime if session is not None else None

    @asynccontextmanager
    async def scope(self):
        existing = _current_session.get()
        if existing is not None and existing.runtime is self:
            yield existing
            return
        session = ImageSession(self)
        token = _current_session.set(session)
        try:
            yield session
        finally:
            # A cancelling caller must not interrupt cleanup of completed uploads.
            cleanup = asyncio.create_task(session.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Do not turn a second cancellation into an orphaned exception.
                cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                raise
            finally:
                _current_session.reset(token)

    async def load(self, images, work_path):
        session = _current_session.get()
        if session is None or session.runtime is not self:
            raise RuntimeError("图片必须在任务图片会话内加载")
        return await session.load(images, work_path)

    async def read(self, ref: ImageReference, work_path):
        if ref.kind == "url":
            return await self._download(ref.value)
        if ref.kind == "upload":
            if self.resolve_upload is None:
                raise ValueError("此入口未配置上传文件解析能力")
            path = await asyncio.to_thread(self.resolve_upload, ref.value)
        else:
            root = Path(work_path).expanduser().resolve()
            candidate = Path(ref.value).expanduser()
            path = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if not path.is_relative_to(root):
                raise ValueError("图片路径必须位于当前工作目录内")
        return await asyncio.to_thread(self._read_file, Path(path))

    @staticmethod
    def _read_file(path):
        with path.open("rb") as stream:
            raw = stream.read(MAX_IMAGE_BYTES + 1)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("VLM 单张图片不能超过 64 MiB")
        return raw

    @staticmethod
    def validate(raw):
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片为空或超过 64 MiB")
        formats = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"),
                   "GIF": ("image/gif", "gif"), "WEBP": ("image/webp", "webp")}
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format not in formats:
                    raise ValueError("VLM 仅支持 JPEG、PNG、GIF、WebP")
                if max(image.size) > 8192:
                    raise ValueError("图片单边不能超过 8192 像素")
                result = formats[image.format]
                image.verify()
                return result
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("无法读取有效图片") from exc

    async def _download(self, url):
        # Pin the connection to the validated public address, including redirects.
        for _ in range(6):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("图片 URL 必须是公网 HTTP(S) 地址")
            addresses = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
                raise ValueError("图片 URL 必须指向公网地址")
            target = httpx.URL(url).copy_with(host=addresses[0][4][0])
            # A separate client prevents TLS connections to one IP from being
            # reused for another hostname after a redirect.
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                async with client.stream("GET", target, headers={"Host": parsed.netloc},
                                         extensions={"sni_hostname": parsed.hostname}) as response:
                    if response.is_redirect:
                        url = str(httpx.URL(url).join(response.headers["location"]))
                        continue
                    response.raise_for_status()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(raw) + len(chunk) > MAX_IMAGE_BYTES:
                            raise ValueError("VLM 单张图片不能超过 64 MiB")
                        raw.extend(chunk)
                    return bytes(raw)
        raise ValueError("图片 URL 重定向次数过多")
