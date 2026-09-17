from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from camera_serve.api.schemas import (
    CameraResponse,
    CaptureOptions,
    CaptureResponse,
    ConnectCameraRequest,
    PointRequest,
    RtcAnswerResponse,
    RtcConfigurationResponse,
    RtcOfferRequest,
    RtcPeerResponse,
    TransformPointsRequest,
    TransformPointsResponse,
)
from camera_serve.domain.transform import transform_points

router = APIRouter()


def _container(request: Request):
    return request.app.state.container


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    container = _container(request)
    current = container.camera_service.current_camera()
    return {
        "status": "ok",
        "camera_connected": current is not None,
        "camera_serial_number": current.serial_number if current else None,
        "rtc_peer_count": await container.rtc_pool.count(),
        "stream_last_error": container.frame_source.last_error,
        "stream": container.frame_source.diagnostics(),
    }


@router.get("/api/v1/cameras", response_model=list[CameraResponse])
async def discover_cameras(request: Request) -> list[CameraResponse]:
    cameras = await _container(request).camera_service.discover()
    return [CameraResponse.from_domain(camera) for camera in cameras]


@router.post("/api/v1/cameras/connect", response_model=CameraResponse)
async def connect_camera(
    payload: ConnectCameraRequest, request: Request
) -> CameraResponse:
    camera = await _container(request).camera_service.connect(payload.serial_number)
    return CameraResponse.from_domain(camera)


@router.get("/api/v1/cameras/current", response_model=CameraResponse)
async def current_camera(request: Request) -> CameraResponse:
    camera = _container(request).camera_service.require_connected()
    return CameraResponse.from_domain(camera)


@router.post("/api/v1/cameras/disconnect", status_code=204)
async def disconnect_camera(request: Request) -> None:
    container = _container(request)
    await container.rtc_pool.close_peers()
    await container.camera_service.disconnect()


@router.post("/api/v1/captures/2d", response_model=CaptureResponse)
async def capture_2d(request: Request) -> CaptureResponse:
    result = await _container(request).camera_service.capture_2d()
    return CaptureResponse.from_domain(result)


@router.post("/api/v1/captures/depth", response_model=CaptureResponse)
async def capture_depth(request: Request) -> CaptureResponse:
    result = await _container(request).camera_service.capture_depth()
    return CaptureResponse.from_domain(result)


@router.post("/api/v1/captures/point-cloud", response_model=CaptureResponse)
async def capture_point_cloud(
    request: Request, payload: CaptureOptions | None = None
) -> CaptureResponse:
    payload = payload or CaptureOptions()
    result = await _container(request).camera_service.capture_point_cloud(
        payload.to_domain()
    )
    return CaptureResponse.from_domain(result)


@router.post("/api/v1/captures/all", response_model=CaptureResponse)
async def capture_all(
    request: Request, payload: CaptureOptions | None = None
) -> CaptureResponse:
    payload = payload or CaptureOptions()
    result = await _container(request).camera_service.capture_all(
        payload.to_domain()
    )
    return CaptureResponse.from_domain(result)


@router.post("/api/v1/transforms/points", response_model=TransformPointsResponse)
async def convert_points(payload: TransformPointsRequest) -> TransformPointsResponse:
    converted = transform_points(
        [point.to_domain() for point in payload.points], payload.transformation
    )
    return TransformPointsResponse(
        points=[PointRequest(x=point.x, y=point.y, z=point.z) for point in converted]
    )


@router.get("/api/v1/files/{relative_path:path}", response_class=FileResponse)
async def download_capture(relative_path: str, request: Request) -> FileResponse:
    path = _container(request).camera_service.resolve_capture_file(relative_path)
    return FileResponse(path, filename=path.name)


@router.post("/api/v1/rtc/offer", response_model=RtcAnswerResponse)
async def rtc_offer(
    payload: RtcOfferRequest, request: Request
) -> RtcAnswerResponse:
    container = _container(request)
    container.camera_service.require_connected()
    answer = await container.rtc_pool.create_answer(payload.sdp, payload.type)
    return RtcAnswerResponse(**answer)


@router.get(
    "/api/v1/rtc/config",
    response_model=RtcConfigurationResponse,
    response_model_exclude_none=True,
)
async def rtc_configuration(request: Request) -> RtcConfigurationResponse:
    return RtcConfigurationResponse(
        **_container(request).rtc_pool.browser_configuration()
    )


@router.get("/api/v1/rtc/peers", response_model=list[RtcPeerResponse])
async def rtc_peers(request: Request) -> list[RtcPeerResponse]:
    peers = await _container(request).rtc_pool.snapshot()
    return [RtcPeerResponse(**peer) for peer in peers]


@router.delete("/api/v1/rtc/peers/{peer_id}", status_code=204)
async def close_rtc_peer(peer_id: str, request: Request) -> None:
    if not await _container(request).rtc_pool.remove(peer_id):
        raise HTTPException(status_code=404, detail="RTC peer not found")
