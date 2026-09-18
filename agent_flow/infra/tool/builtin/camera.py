from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind
from infra.tool.builtin.artifacts import InlineArtifactTool


DEFAULT_CAMERA_SERVICE_BASE_URL = "http://10.42.0.1:7899"
CONTROL_TIMEOUT_SECONDS = 15.0
CAPTURE_TIMEOUT_SECONDS = 120.0

FIXED_POINT_CLOUD_PAYLOAD = {
    "textured": True,
    "custom_reference_frame": False,
    "max_points": 0,
    "parameters": {
        "surface_smoothing": "Strong",
        "noise_removal": "Strong",
        "outlier_removal": "Strong",
        "edge_preservation": "Sharp",
    },
    "transformation": None,
}


def _empty_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


CAMERA_CAPTURE_2D = Tool(
    name="camera_capture_2d",
    description=(
        "通过相机微服务采集一张 2D 图像。若相机尚未连接，工具会自动发现并连接相机；"
        "采集成功后返回文件地址并在消息中自动展示图片卡片，不并重复调用‘inline_artifact’工具。"
    ),
    field="camera",
    input_schema=_empty_input_schema(),
)

CAMERA_CAPTURE_DEPTH = Tool(
    name="camera_capture_depth",
    description=(
        "通过相机微服务采集一张深度图。若相机尚未连接，工具会自动发现并连接相机；"
        "深度图以 TIFF 文件保存，工具返回可下载地址。"
    ),
    field="camera",
    input_schema=_empty_input_schema(),
)

CAMERA_CAPTURE_POINT_CLOUD = Tool(
    name="camera_capture_point_cloud",
    description=(
        "通过相机微服务采集完整纹理点云，并自动展示可交互点云预览。"
        "采集参数固定为最高表面平滑、噪声去除、离群点去除和边缘保留质量。"
    ),
    field="camera",
    input_schema=_empty_input_schema(),
)


class CameraToolError(RuntimeError):
    pass


class CameraHttpClient:
    """Small async client for the existing camera_serve HTTP API."""

    CAPTURE_ROUTES = {
        "2d": "/api/v1/captures/2d",
        "depth": "/api/v1/captures/depth",
        "point-cloud": "/api/v1/captures/point-cloud",
    }

    def __init__(
        self,
        base_url: str | None = None,
        preferred_serial_number: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = self._normalize_base_url(
            base_url
            if base_url is not None
            else os.getenv("CAMERA_SERVICE_BASE_URL", DEFAULT_CAMERA_SERVICE_BASE_URL)
        )
        self.preferred_serial_number = (
            preferred_serial_number
            if preferred_serial_number is not None
            else os.getenv("CAMERA_SERIAL_NUMBER", "")
        ).strip()
        self._transport = transport

    async def capture(self, kind: str) -> dict[str, Any]:
        route = self.CAPTURE_ROUTES.get(kind)
        if route is None:
            raise CameraToolError(f"不支持的相机采集类型: {kind}")

        async with httpx.AsyncClient(
            base_url=self.base_url,
            timeout=CONTROL_TIMEOUT_SECONDS,
            transport=self._transport,
        ) as client:
            await self._ensure_connected(client)
            response = await self._request(
                client,
                "POST",
                route,
                timeout=CAPTURE_TIMEOUT_SECONDS,
                json=FIXED_POINT_CLOUD_PAYLOAD if kind == "point-cloud" else None,
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise CameraToolError("相机服务返回了无效的采集结果") from exc
        if not isinstance(result, dict):
            raise CameraToolError("相机服务返回了无效的采集结果")

        files = result.get("files")
        if not isinstance(files, dict):
            files = {}
        result["files"] = files
        result["file_urls"] = {
            file_type: self.file_url(str(relative_path))
            for file_type, relative_path in files.items()
            if relative_path
        }
        return result

    async def _ensure_connected(self, client: httpx.AsyncClient) -> dict[str, Any]:
        current = await self._current_camera(client)
        if current is not None:
            return current

        response = await self._request(client, "GET", "/api/v1/cameras")
        try:
            cameras = response.json()
        except ValueError as exc:
            raise CameraToolError("相机服务返回了无效的设备列表") from exc
        if not isinstance(cameras, list) or not cameras:
            raise CameraToolError("未发现可连接的相机")

        selected: dict[str, Any] | None = None
        if self.preferred_serial_number:
            selected = next(
                (
                    camera
                    for camera in cameras
                    if isinstance(camera, dict)
                    and str(camera.get("serial_number", ""))
                    == self.preferred_serial_number
                ),
                None,
            )
            if selected is None:
                raise CameraToolError(
                    f"未发现指定相机: {self.preferred_serial_number}"
                )
        else:
            selected = next(
                (camera for camera in cameras if isinstance(camera, dict)), None
            )
            if selected is None:
                raise CameraToolError("相机服务返回了无效的设备列表")

        serial_number = str(selected.get("serial_number", "")).strip()
        if not serial_number:
            raise CameraToolError("相机设备缺少序列号")
        connected = await self._request(
            client,
            "POST",
            "/api/v1/cameras/connect",
            timeout=CAPTURE_TIMEOUT_SECONDS,
            json={"serial_number": serial_number},
        )
        try:
            result = connected.json()
        except ValueError as exc:
            raise CameraToolError("相机服务返回了无效的连接结果") from exc
        if not isinstance(result, dict):
            raise CameraToolError("相机服务返回了无效的连接结果")
        return result

    async def _current_camera(
        self, client: httpx.AsyncClient
    ) -> dict[str, Any] | None:
        try:
            response = await client.get("/api/v1/cameras/current")
        except httpx.TimeoutException as exc:
            raise CameraToolError("相机服务响应超时") from exc
        except httpx.RequestError as exc:
            raise CameraToolError(f"无法连接相机服务: {exc}") from exc
        if response.status_code == 409:
            return None
        self._raise_for_status(response)
        try:
            result = response.json()
        except ValueError as exc:
            raise CameraToolError("相机服务返回了无效的当前设备信息") from exc
        if not isinstance(result, dict):
            raise CameraToolError("相机服务返回了无效的当前设备信息")
        return result

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        timeout: float = CONTROL_TIMEOUT_SECONDS,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            request_kwargs: dict[str, Any] = {"timeout": timeout}
            if json is not None:
                request_kwargs["json"] = json
            response = await client.request(
                method,
                path,
                **request_kwargs,
            )
        except httpx.TimeoutException as exc:
            raise CameraToolError("相机服务响应超时") from exc
        except httpx.RequestError as exc:
            raise CameraToolError(f"无法连接相机服务: {exc}") from exc
        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                raw_detail = payload.get("detail")
                if isinstance(raw_detail, str):
                    detail = raw_detail
                elif isinstance(raw_detail, list):
                    detail = "；".join(
                        str(item.get("msg", ""))
                        for item in raw_detail
                        if isinstance(item, dict) and item.get("msg")
                    )
        except ValueError:
            detail = ""
        raise CameraToolError(
            detail or f"相机服务请求失败（HTTP {response.status_code}）"
        )

    def file_url(self, relative_path: str) -> str:
        encoded_path = "/".join(
            quote(part, safe="")
            for part in str(relative_path).split("/")
            if part
        )
        return f"{self.base_url}/api/v1/files/{encoded_path}"

    @staticmethod
    def _normalize_base_url(value: str) -> str:
        raw = str(value or "").strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise CameraToolError("相机微服务地址必须是有效的 HTTP 或 HTTPS URL")
        if parsed.username or parsed.password:
            raise CameraToolError("相机微服务地址不能包含用户名或密码")
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _point_cloud_file(files: dict[str, Any]) -> tuple[str, str]:
    for key in (
        "transformed_textured_point_cloud",
        "transformed_point_cloud",
        "textured_point_cloud",
        "point_cloud",
    ):
        path = files.get(key)
        if path:
            return key, str(path)
    return "", ""


async def _publish_capture_artifact(
    *,
    agent_id: str,
    kind: str,
    result: dict[str, Any],
) -> None:
    files = result.get("files") or {}
    file_urls = result.get("file_urls") or {}
    common_metadata = {
        "capture_id": result.get("capture_id", ""),
        "captured_at": result.get("captured_at", ""),
    }
    arguments: dict[str, Any] | None = None

    if kind == "2d" and file_urls.get("image_2d"):
        arguments = {
            "agent_id": agent_id,
            "artifact_type": "image",
            "image": {
                "title": "相机 2D 图像",
                "url": file_urls["image_2d"],
                "alt": "相机采集的 2D 图像",
                "mime_type": "image/png",
                "metadata": {
                    **common_metadata,
                    "file_path": files.get("image_2d", ""),
                },
            },
        }
    elif kind == "point-cloud":
        file_type, file_path = _point_cloud_file(files)
        url = file_urls.get(file_type) if file_type else ""
        if url:
            stats = result.get("point_cloud") or {}
            arguments = {
                "agent_id": agent_id,
                "artifact_type": "point_cloud",
                "point_cloud": {
                    "title": "点云预览",
                    "url": url,
                    "source_label": file_path.rsplit("/", 1)[-1],
                    "mime_type": "application/octet-stream",
                    "point_count": stats.get("saved_points", 0),
                    "metadata": {
                        **common_metadata,
                        "file_path": file_path,
                        "file_type": file_type,
                        "point_cloud": stats,
                    },
                },
            }

    if arguments is None:
        return
    payload = InlineArtifactTool().build_event_payload(arguments)
    await bus.publish(Event(payload["event_name"], payload=payload))


async def _capture(tool_name: str, kind: str, **kwargs: Any) -> Event:
    agent_id = str(kwargs.get("agent_id", ""))
    try:
        result = await CameraHttpClient().capture(kind)
        await _publish_capture_artifact(
            agent_id=agent_id,
            kind=kind,
            result=result,
        )
    except Exception as exc:
        respond = Tool_respond(
            agent_id=agent_id,
            name=tool_name,
            success=False,
            respond=f"相机采集失败: {exc}",
        )
        return factory.tool(tool_name).failed(respond)

    respond = Tool_respond(
        agent_id=agent_id,
        name=tool_name,
        success=True,
        respond=result,
    )
    return factory.tool(tool_name).succeeded(respond)


on_tool = On_bind()
factory._build_and_register_list(
    [CAMERA_CAPTURE_2D, CAMERA_CAPTURE_DEPTH, CAMERA_CAPTURE_POINT_CLOUD],
    bus,
)


@on_tool.on(factory.tool("camera_capture_2d").called())
async def camera_capture_2d(**kwargs: Any) -> Event:
    return await _capture("camera_capture_2d", "2d", **kwargs)


@on_tool.on(factory.tool("camera_capture_depth").called())
async def camera_capture_depth(**kwargs: Any) -> Event:
    return await _capture("camera_capture_depth", "depth", **kwargs)


@on_tool.on(factory.tool("camera_capture_point_cloud").called())
async def camera_capture_point_cloud(**kwargs: Any) -> Event:
    return await _capture("camera_capture_point_cloud", "point-cloud", **kwargs)
