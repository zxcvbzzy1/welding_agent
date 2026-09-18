"""Inline artifact 收集与转换。

agent 在运行期间调用 agent_flow 的 `inline_artifact` 工具时，会通过
FrontendEventBridge 把 `artifacts.{type}` 事件镜像到运行事件流（"events" 集合）。
本模块负责在一次回复结束时，把这些事件转换成消息的 content_parts（type=artifact），
让 "agent 返回的消息" 真正带上内联产物并在刷新后依然可见。
"""

from __future__ import annotations

import base64
import re
from typing import Any, Iterable

ARTIFACT_EVENT_PREFIX = "artifacts."
ARTIFACT_PART_TYPE = "artifact"
VALID_ARTIFACT_TYPES = {
    "message",
    "image",
    "diff",
    "document",
    "web",
    "deploy",
    "point_cloud",
}


def artifact_identity(artifact: dict[str, Any]) -> str:
    artifact_type = str(artifact.get("type", "message")).strip().lower()
    url = str(artifact.get("url") or "").strip()
    if url:
        return f"{artifact_type}|url|{url}"
    metadata = artifact.get("metadata") or {}
    file_path = str(artifact.get("file_path") or metadata.get("file_path") or "").strip()
    if file_path:
        return f"{artifact_type}|file|{file_path}"
    label = str(
        artifact.get("title")
        or artifact.get("preview_title")
        or ""
    )
    body = str(
        artifact.get("content")
        or artifact.get("after")
        or artifact.get("html")
        or artifact.get("alt")
        or ""
    )
    return f"{artifact_type}|{label}|{len(body)}|{body[:160]}"


def is_artifact_event(event: dict[str, Any]) -> bool:
    return str(event.get("name", "")).startswith(ARTIFACT_EVENT_PREFIX)


def artifact_to_content_part(
    artifact: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把单个产物对象转换成一个 content_part。

    完整结构化产物放在 metadata.artifact 里，前端 ArtifactCard 据此渲染；
    同时把常用字段拍平到顶层，兼容已有的 content_part 读取逻辑。
    """

    artifact = dict(artifact or {})
    artifact_type = str(artifact.get("type", "")).strip().lower()
    base_metadata = dict(artifact.get("metadata") or {})
    part_metadata: dict[str, Any] = {
        **base_metadata,
        "artifact": artifact,
        "artifact_type": artifact_type,
        "editable": bool(artifact.get("editable", False)),
    }
    if source:
        part_metadata["artifact_source"] = source

    part: dict[str, Any] = {
        "type": ARTIFACT_PART_TYPE,
        "title": artifact.get("title", ""),
        "language": artifact.get("language", ""),
        "mime_type": artifact.get("mime_type", ""),
        "metadata": part_metadata,
    }
    if artifact_type in {"message", "document"}:
        part["text"] = artifact.get("content", "")
    elif artifact_type in {"image", "web", "deploy", "point_cloud"}:
        part["url"] = artifact.get("url", "")
    elif artifact_type == "diff":
        # diff 的前后内容保留在 metadata.artifact 里，after 拍平到 diff 字段做兜底。
        part["diff"] = artifact.get("after", "")
    return part


def collect_inline_artifact_parts(
    events: Iterable[dict[str, Any]] | None,
    *,
    since: float = 0.0,
    run_id: str = "",
) -> list[dict[str, Any]]:
    """从运行事件中筛出 artifacts.* 并转换成 content_parts。

    - since: 只收集该时间戳之后产生的产物，用来把本次回复和历史回复区分开。
    - run_id: 可选，按 run_id 再过滤一层（DM 下 run_id 即 conversation_id）。
    """

    parts: list[dict[str, Any]] = []
    seen_event_ids: set[str] = set()
    seen_artifact_identities: set[str] = set()
    ordered = sorted(events or [], key=lambda item: item.get("created_at") or 0)
    for event in ordered:
        if not is_artifact_event(event):
            continue
        created_at = event.get("created_at") or 0
        if since and created_at < since:
            continue
        payload = event.get("payload") or {}
        if run_id and payload.get("run_id") and payload.get("run_id") != run_id:
            continue
        artifact = payload.get("artifact") or {}
        if not artifact or str(artifact.get("type", "")).strip().lower() not in VALID_ARTIFACT_TYPES:
            continue
        event_id = event.get("event_id")
        if event_id:
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
        identity = artifact_identity(artifact)
        if identity in seen_artifact_identities:
            continue
        seen_artifact_identities.add(identity)
        parts.append(
            artifact_to_content_part(
                artifact,
                source={
                    "event_id": event_id or "",
                    "agent_id": payload.get("agent_id", ""),
                    "run_id": payload.get("run_id", ""),
                    "created_at": created_at,
                },
            )
        )
    return parts


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _safe_filename(name: str, fallback: str) -> str:
    """Replace filesystem-unsafe characters with underscore; fall back if empty."""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    return cleaned if cleaned else fallback


def _ext_from_mime(mime: str | None) -> str:
    """Map common MIME types to file extensions (no dot)."""
    _MAP = {
        "text/markdown": "md",
        "text/html": "html",
        "application/json": "json",
        "text/plain": "txt",
    }
    return _MAP.get((mime or "").strip().lower(), "")


def _artifact_id_from_url(url: str) -> str | None:
    """Extract artifact id from a URL ending in /api/im/artifacts/<id>."""
    m = re.search(r"/api/im/artifacts/([^/?#]+)", url)
    return m.group(1) if m else None


def _ensure_ext(filename: str, ext: str) -> str:
    """Append .ext to filename if it has no extension already."""
    if "." not in filename.rsplit("/", 1)[-1]:
        return filename + "." + ext
    return filename


def artifact_download_file(
    artifact: dict[str, Any],
    *,
    index: int = 0,
    storage: Any = None,
) -> tuple[str, bytes]:
    """Return (filename, raw_bytes) for a single artifact dict.

    storage, when provided, must be an ArtifactStorage instance whose
    .get(id) returns a dict with "path" and "filename" keys.
    """
    artifact_type = str(artifact.get("type", "")).strip().lower()

    if artifact_type == "document":
        text = str(artifact.get("content") or "")
        ext = (
            artifact.get("format")
            or _ext_from_mime(artifact.get("mime_type"))
            or "txt"
        )
        raw_title = str(artifact.get("title") or "")
        base = _safe_filename(raw_title, f"document-{index}")
        filename = base if "." in base.rsplit("/", 1)[-1] else f"{base}.{ext}"
        return filename, text.encode("utf-8")

    if artifact_type == "message":
        text = str(artifact.get("content") or "")
        raw_title = str(artifact.get("title") or "")
        base = _safe_filename(raw_title, f"message-{index}")
        filename = _ensure_ext(base, "txt")
        return filename, text.encode("utf-8")

    if artifact_type == "diff":
        text = str(artifact.get("after") or artifact.get("content") or "")
        raw_title = str(artifact.get("title") or "")
        base = _safe_filename(raw_title, f"diff-{index}")
        filename = _ensure_ext(base, "diff")
        return filename, text.encode("utf-8")

    if artifact_type == "web":
        html = artifact.get("html")
        url = artifact.get("url")
        raw_title = str(artifact.get("title") or "")
        if html:
            base = _safe_filename(raw_title, f"web-{index}")
            filename = _ensure_ext(base, "html")
            return filename, str(html).encode("utf-8")
        if url:
            return f"web-{index}.url.txt", str(url).encode("utf-8")
        base = _safe_filename(raw_title, f"web-{index}")
        filename = _ensure_ext(base, "html")
        return filename, b""

    if artifact_type == "image":
        url = str(artifact.get("url") or "")
        raw_title = str(artifact.get("title") or "")
        if url.startswith("data:"):
            # data:<mime>;base64,<payload>
            m = re.match(r"data:[^;]*;base64,(.+)", url, re.DOTALL)
            if m:
                raw_bytes = base64.b64decode(m.group(1))
            else:
                raw_bytes = b""
            base = _safe_filename(raw_title, f"image-{index}")
            # Try to keep extension from mime in data URI
            mime_m = re.match(r"data:([^;]+);", url)
            ext = _ext_from_mime(mime_m.group(1) if mime_m else "") or "bin"
            filename = base if "." in base.rsplit("/", 1)[-1] else f"{base}.{ext}"
            return filename, raw_bytes
        artifact_id = _artifact_id_from_url(url)
        if artifact_id and storage is not None:
            item = storage.get(artifact_id)
            if item:
                file_path = item.get("path", "")
                filename = _safe_filename(
                    item.get("filename") or raw_title, f"image-{index}"
                )
                try:
                    with open(file_path, "rb") as fh:
                        return filename, fh.read()
                except OSError:
                    pass
        return f"image-{index}.url.txt", url.encode("utf-8")

    if artifact_type == "point_cloud":
        url = str(artifact.get("url") or "")
        raw_title = str(artifact.get("source_label") or artifact.get("title") or "")
        base = _safe_filename(raw_title, f"point-cloud-{index}.ply")
        filename = f"{base}.url.txt"
        return filename, url.encode("utf-8")

    # default / unknown
    content = str(artifact.get("content") or artifact.get("url") or "")
    raw_title = str(artifact.get("title") or "")
    base = _safe_filename(raw_title, f"artifact-{index}")
    filename = _ensure_ext(base, "txt")
    return filename, content.encode("utf-8")
