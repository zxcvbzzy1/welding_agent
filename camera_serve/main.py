from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from camera_serve.api.routes import router
from camera_serve.application.camera_service import CameraApplicationService
from camera_serve.config import Settings
from camera_serve.domain.exceptions import (
    CameraAlreadyConnectedError,
    CameraNotConnectedError,
    CameraNotFoundError,
    CameraSdkError,
    CameraServiceError,
    CaptureFileNotFoundError,
    InvalidTransformationError,
    RtcCapacityError,
)
from camera_serve.infrastructure.mecheye_gateway import MechEyeCameraGateway
from camera_serve.infrastructure.storage import LocalCaptureStorage
from camera_serve.infrastructure.webrtc import LiveFrameSource, RtcPeerPool


@dataclass(slots=True)
class Container:
    camera_service: CameraApplicationService
    frame_source: LiveFrameSource
    rtc_pool: RtcPeerPool


def build_container(settings: Settings | None = None) -> Container:
    settings = settings or Settings.from_environment()
    service_root = Path(__file__).resolve().parent
    camera_service = CameraApplicationService(
        MechEyeCameraGateway(), LocalCaptureStorage(service_root)
    )
    frame_source = LiveFrameSource(
        camera_service,
        settings.stream_fps,
        width=settings.stream_width,
        height=settings.stream_height,
        bitrate_kbps=settings.stream_bitrate_kbps,
        encoder_name=settings.stream_encoder,
    )
    rtc_pool = RtcPeerPool(
        frame_source,
        max_peers=settings.rtc_max_peers,
        disconnect_grace_seconds=settings.rtc_disconnect_grace_seconds,
        reap_interval_seconds=settings.rtc_reap_interval_seconds,
        ice_servers=settings.rtc_ice_servers,
    )
    return Container(camera_service, frame_source, rtc_pool)


def create_app(container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.container = container or build_container()
        await app.state.container.rtc_pool.start()
        try:
            yield
        finally:
            await app.state.container.rtc_pool.close_all()
            await app.state.container.camera_service.shutdown()

    app = FastAPI(
        title="Mech-Eye Camera Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.exception_handler(CameraSdkError)
    async def handle_sdk_error(_: Request, exc: CameraSdkError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "detail": str(exc),
                "operation": exc.operation,
                "sdk_error_code": exc.code,
                "sdk_error_description": exc.description,
            },
        )

    status_codes = {
        CameraNotConnectedError: 409,
        CameraAlreadyConnectedError: 409,
        CameraNotFoundError: 404,
        CaptureFileNotFoundError: 404,
        InvalidTransformationError: 409,
        RtcCapacityError: 503,
    }

    @app.exception_handler(CameraServiceError)
    async def handle_service_error(
        _: Request, exc: CameraServiceError
    ) -> JSONResponse:
        status_code = next(
            (
                code
                for error_type, code in status_codes.items()
                if isinstance(exc, error_type)
            ),
            400,
        )
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def handle_value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


app = create_app()
