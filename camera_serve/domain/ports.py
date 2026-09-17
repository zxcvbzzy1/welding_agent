from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from .models import CameraDescriptor, PointCloudAcquisitionParameters


class CameraGateway(Protocol):
    def discover(self) -> list[CameraDescriptor]: ...

    def connect(self, serial_number: str) -> CameraDescriptor: ...

    def disconnect(self) -> None: ...

    def current_camera(self) -> CameraDescriptor | None: ...

    def capture_2d(self, output_path: Path) -> None: ...

    def capture_depth(self, output_path: Path) -> None: ...

    def capture_point_cloud(
        self,
        untextured_path: Path,
        textured_path: Path | None,
        custom_reference_frame: bool,
        acquisition: PointCloudAcquisitionParameters,
    ) -> None: ...

    def capture_all(
        self,
        image_path: Path,
        depth_path: Path,
        untextured_path: Path,
        textured_path: Path | None,
        custom_reference_frame: bool,
        acquisition: PointCloudAcquisitionParameters,
    ) -> None: ...

    def capture_live_frame(self) -> NDArray[np.uint8]: ...


class CaptureStorage(Protocol):
    @property
    def root(self) -> Path: ...

    def create_capture_directory(self) -> tuple[str, Path]: ...

    def relative_path(self, path: Path) -> str: ...

    def resolve_file(self, relative_path: str) -> Path: ...
