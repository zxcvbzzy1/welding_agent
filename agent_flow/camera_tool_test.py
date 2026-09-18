from __future__ import annotations

import json

import httpx
import pytest

from infra.tool.builtin import camera


def _capture_payload(file_type: str, path: str, *, point_cloud=None):
    return {
        "capture_id": "capture-1",
        "captured_at": "2026-09-18T12:00:00Z",
        "files": {file_type: path},
        "point_cloud": point_cloud,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "route", "file_type", "path"),
    [
        ("2d", "/api/v1/captures/2d", "image_2d", "captures/a b/image.png"),
        ("depth", "/api/v1/captures/depth", "depth_map", "captures/a/depth.tiff"),
        (
            "point-cloud",
            "/api/v1/captures/point-cloud",
            "textured_point_cloud",
            "captures/a/cloud.ply",
        ),
    ],
)
async def test_capture_reuses_connected_camera_and_builds_file_urls(
    kind, route, file_type, path
):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/cameras/current":
            return httpx.Response(200, json={"serial_number": "connected"})
        if request.url.path == route:
            stats = (
                {"original_points": 10, "saved_points": 10, "max_points": 0, "transformed": False}
                if kind == "point-cloud"
                else None
            )
            return httpx.Response(
                200,
                json=_capture_payload(file_type, path, point_cloud=stats),
            )
        return httpx.Response(404, json={"detail": "unexpected route"})

    client = camera.CameraHttpClient(
        "http://camera.test:7899",
        transport=httpx.MockTransport(handler),
    )
    result = await client.capture(kind)

    assert [request.url.path for request in requests] == [
        "/api/v1/cameras/current",
        route,
    ]
    assert result["file_urls"][file_type] == (
        f"http://camera.test:7899/api/v1/files/{path.replace(' ', '%20')}"
    )
    if kind == "point-cloud":
        assert json.loads(requests[-1].content) == camera.FIXED_POINT_CLOUD_PAYLOAD
    else:
        assert requests[-1].content == b""


@pytest.mark.asyncio
async def test_capture_auto_connects_preferred_camera():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/cameras/current":
            return httpx.Response(409, json={"detail": "No camera is connected"})
        if request.url.path == "/api/v1/cameras":
            return httpx.Response(
                200,
                json=[
                    {"serial_number": "first"},
                    {"serial_number": "preferred"},
                ],
            )
        if request.url.path == "/api/v1/cameras/connect":
            assert json.loads(request.content) == {"serial_number": "preferred"}
            return httpx.Response(200, json={"serial_number": "preferred"})
        if request.url.path == "/api/v1/captures/2d":
            return httpx.Response(
                200,
                json=_capture_payload("image_2d", "captures/a/image.png"),
            )
        return httpx.Response(404)

    client = camera.CameraHttpClient(
        "http://camera.test",
        "preferred",
        transport=httpx.MockTransport(handler),
    )
    await client.capture("2d")

    assert [request.url.path for request in requests] == [
        "/api/v1/cameras/current",
        "/api/v1/cameras",
        "/api/v1/cameras/connect",
        "/api/v1/captures/2d",
    ]


@pytest.mark.asyncio
async def test_capture_uses_first_camera_without_preference():
    connected_serials = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/cameras/current":
            return httpx.Response(409, json={"detail": "not connected"})
        if request.url.path == "/api/v1/cameras":
            return httpx.Response(200, json=[{"serial_number": "first"}, {"serial_number": "second"}])
        if request.url.path == "/api/v1/cameras/connect":
            connected_serials.append(json.loads(request.content)["serial_number"])
            return httpx.Response(200, json={"serial_number": "first"})
        return httpx.Response(
            200,
            json=_capture_payload("depth_map", "captures/a/depth.tiff"),
        )

    client = camera.CameraHttpClient(
        "http://camera.test",
        "",
        transport=httpx.MockTransport(handler),
    )
    await client.capture("depth")

    assert connected_serials == ["first"]


@pytest.mark.asyncio
async def test_capture_reports_missing_preferred_camera():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/cameras/current":
            return httpx.Response(409, json={"detail": "not connected"})
        return httpx.Response(200, json=[{"serial_number": "other"}])

    client = camera.CameraHttpClient(
        "http://camera.test",
        "missing",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(camera.CameraToolError, match="未发现指定相机: missing"):
        await client.capture("2d")


@pytest.mark.asyncio
async def test_capture_reports_no_discovered_camera_and_http_detail():
    def no_camera_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/cameras/current":
            return httpx.Response(409, json={"detail": "not connected"})
        return httpx.Response(200, json=[])

    client = camera.CameraHttpClient(
        "http://camera.test",
        transport=httpx.MockTransport(no_camera_handler),
    )
    with pytest.raises(camera.CameraToolError, match="未发现可连接的相机"):
        await client.capture("2d")

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "SDK capture failed"})

    client = camera.CameraHttpClient(
        "http://camera.test",
        transport=httpx.MockTransport(error_handler),
    )
    with pytest.raises(camera.CameraToolError, match="SDK capture failed"):
        await client.capture("2d")


@pytest.mark.asyncio
async def test_capture_reports_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = camera.CameraHttpClient(
        "http://camera.test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(camera.CameraToolError, match="相机服务响应超时"):
        await client.capture("2d")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_event"),
    [("2d", "artifacts.image"), ("point-cloud", "artifacts.point_cloud")],
)
async def test_capture_publishes_preview_artifact(monkeypatch, kind, expected_event):
    events = []

    class RecordingBus:
        async def publish(self, event):
            events.append(event)
            return []

    monkeypatch.setattr(camera, "bus", RecordingBus())
    stats = {
        "original_points": 20,
        "saved_points": 20,
        "max_points": 0,
        "transformed": False,
    }
    result = _capture_payload(
        "image_2d" if kind == "2d" else "textured_point_cloud",
        "captures/a/image.png" if kind == "2d" else "captures/a/cloud.ply",
        point_cloud=stats if kind == "point-cloud" else None,
    )
    result["file_urls"] = {
        "image_2d" if kind == "2d" else "textured_point_cloud": (
            "http://camera.test/api/v1/files/captures/a/image.png"
            if kind == "2d"
            else "http://camera.test/api/v1/files/captures/a/cloud.ply"
        )
    }

    await camera._publish_capture_artifact(
        agent_id="camera-agent",
        kind=kind,
        result=result,
    )

    assert len(events) == 1
    assert events[0].name == expected_event
    assert events[0].payload["artifact"]["url"].startswith("http://camera.test/")
    if kind == "point-cloud":
        assert events[0].payload["artifact"]["point_count"] == 20


@pytest.mark.asyncio
async def test_tool_handler_returns_failed_event(monkeypatch):
    async def fail_capture(self, kind):
        raise camera.CameraToolError("service unavailable")

    monkeypatch.setattr(camera.CameraHttpClient, "capture", fail_capture)
    event = await camera._capture(
        "camera_capture_depth",
        "depth",
        agent_id="camera-agent",
    )

    assert event.name.endswith(".failed")
    assert event.payload.success is False
    assert "service unavailable" in event.payload.respond
