from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class CameraDescriptor:
    serial_number: str
    model: str
    ip_address: str
    port: int
    device_name: str = ""
    hardware_version: str = ""
    firmware_version: str = ""


@dataclass(frozen=True, slots=True)
class CaptureResult:
    capture_id: str
    captured_at: datetime
    files: dict[str, str] = field(default_factory=dict)
    point_cloud: "PointCloudStats | None" = None


@dataclass(frozen=True, slots=True)
class PointCloudStats:
    original_points: int
    saved_points: int
    max_points: int
    transformed: bool


ProcessingLevel = Literal["Off", "Weak", "Normal", "Strong"]
EdgePreservationLevel = Literal["Sharp", "Normal", "Smooth"]


@dataclass(frozen=True, slots=True)
class PointCloudAcquisitionParameters:
    depth_range_mm: tuple[int, int] | None = None
    roi: tuple[int, int, int, int] | None = None
    exposure_sequence_ms: tuple[float, ...] | None = None
    surface_smoothing: ProcessingLevel | None = None
    noise_removal: ProcessingLevel | None = None
    outlier_removal: ProcessingLevel | None = None
    edge_preservation: EdgePreservationLevel | None = None


@dataclass(frozen=True, slots=True)
class PointCloudCaptureOptions:
    textured: bool = True
    custom_reference_frame: bool = False
    max_points: int = 0
    acquisition: PointCloudAcquisitionParameters = field(
        default_factory=PointCloudAcquisitionParameters
    )
    transformation: tuple[tuple[float, ...], ...] | None = None


@dataclass(frozen=True, slots=True)
class Point3D:
    x: float
    y: float
    z: float
