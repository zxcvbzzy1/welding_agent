from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np
from numpy.typing import NDArray

from camera_serve.domain.exceptions import CameraNotConnectedError
from camera_serve.domain.models import (
    CameraDescriptor,
    CaptureResult,
    PointCloudCaptureOptions,
    PointCloudStats,
)
from camera_serve.domain.ply import process_ascii_ply
from camera_serve.domain.ports import CameraGateway, CaptureStorage

T = TypeVar("T")


class CameraApplicationService:
    """Serializes access to the blocking, single-camera Mech-Eye SDK."""

    def __init__(self, gateway: CameraGateway, storage: CaptureStorage) -> None:
        self._gateway = gateway
        self._storage = storage
        self._operation_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mecheye-sdk"
        )

    async def discover(self) -> list[CameraDescriptor]:
        async with self._operation_lock:
            return await self._run_blocking(self._gateway.discover)

    async def connect(self, serial_number: str) -> CameraDescriptor:
        async with self._operation_lock:
            return await self._run_blocking(self._gateway.connect, serial_number)

    async def disconnect(self) -> None:
        async with self._operation_lock:
            await self._run_blocking(self._gateway.disconnect)

    async def shutdown(self) -> None:
        await self.disconnect()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def current_camera(self) -> CameraDescriptor | None:
        return self._gateway.current_camera()

    def require_connected(self) -> CameraDescriptor:
        camera = self.current_camera()
        if camera is None:
            raise CameraNotConnectedError("No camera is connected")
        return camera

    async def capture_2d(self) -> CaptureResult:
        async with self._operation_lock:
            self.require_connected()
            capture_id, directory = self._storage.create_capture_directory()
            output = directory / "image_2d.png"
            await self._run_blocking(self._gateway.capture_2d, output)
        return self._result(capture_id, image_2d=output)

    async def capture_depth(self) -> CaptureResult:
        async with self._operation_lock:
            self.require_connected()
            capture_id, directory = self._storage.create_capture_directory()
            output = directory / "depth_map.tiff"
            await self._run_blocking(self._gateway.capture_depth, output)
        return self._result(capture_id, depth_map=output)

    async def capture_point_cloud(self, options: PointCloudCaptureOptions) -> CaptureResult:
        async with self._operation_lock:
            self.require_connected()
            capture_id, directory = self._storage.create_capture_directory()
            untextured = directory / "point_cloud.ply"
            textured_path = (
                directory / "textured_point_cloud.ply" if options.textured else None
            )
            await self._run_blocking(
                self._gateway.capture_point_cloud,
                untextured,
                textured_path,
                options.custom_reference_frame,
                options.acquisition,
            )
            files, stats = await self._process_point_cloud_files(
                untextured, textured_path, options
            )
        return self._result(capture_id, point_cloud_stats=stats, **files)

    async def capture_all(self, options: PointCloudCaptureOptions) -> CaptureResult:
        async with self._operation_lock:
            self.require_connected()
            capture_id, directory = self._storage.create_capture_directory()
            image = directory / "image_2d.png"
            depth = directory / "depth_map.tiff"
            untextured = directory / "point_cloud.ply"
            textured_path = (
                directory / "textured_point_cloud.ply" if options.textured else None
            )
            await self._run_blocking(
                self._gateway.capture_all,
                image,
                depth,
                untextured,
                textured_path,
                options.custom_reference_frame,
                options.acquisition,
            )
            cloud_files, stats = await self._process_point_cloud_files(
                untextured, textured_path, options
            )
        files: dict[str, Path] = {"image_2d": image, "depth_map": depth, **cloud_files}
        return self._result(capture_id, point_cloud_stats=stats, **files)

    async def capture_live_frame(self) -> NDArray[np.uint8]:
        async with self._operation_lock:
            self.require_connected()
            return await self._run_blocking(self._gateway.capture_live_frame)

    def resolve_capture_file(self, relative_path: str) -> Path:
        return self._storage.resolve_file(relative_path)

    async def _process_point_cloud_files(
        self,
        untextured: Path,
        textured: Path | None,
        options: PointCloudCaptureOptions,
    ) -> tuple[dict[str, Path], PointCloudStats]:
        untextured_result = await self._run_blocking(
            process_ascii_ply,
            untextured,
            untextured,
            max_points=options.max_points,
        )
        files: dict[str, Path] = {"point_cloud": untextured}

        if textured is not None:
            await self._run_blocking(
                process_ascii_ply,
                textured,
                textured,
                max_points=options.max_points,
            )
            files["textured_point_cloud"] = textured

        if options.transformation is not None:
            transformed = untextured.with_name("transformed_point_cloud.ply")
            await self._run_blocking(
                process_ascii_ply,
                untextured,
                transformed,
                transformation=options.transformation,
            )
            files["transformed_point_cloud"] = transformed
            if textured is not None:
                transformed_textured = textured.with_name(
                    "transformed_textured_point_cloud.ply"
                )
                await self._run_blocking(
                    process_ascii_ply,
                    textured,
                    transformed_textured,
                    transformation=options.transformation,
                )
                files["transformed_textured_point_cloud"] = transformed_textured

        return files, PointCloudStats(
            original_points=untextured_result.original_points,
            saved_points=untextured_result.saved_points,
            max_points=options.max_points,
            transformed=options.transformation is not None,
        )

    def _result(
        self,
        capture_id: str,
        *,
        point_cloud_stats: PointCloudStats | None = None,
        **files: Path,
    ) -> CaptureResult:
        from datetime import datetime, timezone

        return CaptureResult(
            capture_id=capture_id,
            captured_at=datetime.now(timezone.utc),
            files={key: self._storage.relative_path(path) for key, path in files.items()},
            point_cloud=point_cloud_stats,
        )

    async def _run_blocking(
        self, function: Callable[..., T], *args: object, **kwargs: object
    ) -> T:
        """Run SDK work off-loop and remain reliable on runtimes with lost wakeups."""
        future = self._executor.submit(partial(function, *args, **kwargs))
        while not future.done():
            await asyncio.sleep(0.001)
        return future.result()
