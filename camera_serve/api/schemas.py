from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from camera_serve.domain.models import (
    CameraDescriptor,
    CaptureResult,
    Point3D,
    PointCloudAcquisitionParameters,
    PointCloudCaptureOptions,
)


class CameraResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    serial_number: str
    model: str
    ip_address: str
    port: int
    device_name: str
    hardware_version: str
    firmware_version: str

    @classmethod
    def from_domain(cls, camera: CameraDescriptor) -> "CameraResponse":
        return cls.model_validate(camera)


class ConnectCameraRequest(BaseModel):
    serial_number: str = Field(min_length=1)


class DepthRangeOptions(BaseModel):
    min_mm: int = Field(ge=0)
    max_mm: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_order(self) -> "DepthRangeOptions":
        if self.min_mm >= self.max_mm:
            raise ValueError("depth range min_mm must be less than max_mm")
        return self


class RoiOptions(BaseModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class PointCloudParameterOptions(BaseModel):
    depth_range: DepthRangeOptions | None = None
    roi: RoiOptions | None = None
    exposure_sequence_ms: list[float] | None = Field(default=None, min_length=1)
    surface_smoothing: Literal["Off", "Weak", "Normal", "Strong"] | None = None
    noise_removal: Literal["Off", "Weak", "Normal", "Strong"] | None = None
    outlier_removal: Literal["Off", "Weak", "Normal", "Strong"] | None = None
    edge_preservation: Literal["Sharp", "Normal", "Smooth"] | None = None

    @field_validator("exposure_sequence_ms")
    @classmethod
    def validate_exposure_sequence(cls, value: list[float] | None):
        if value is not None and any(item <= 0 for item in value):
            raise ValueError("all exposure times must be greater than 0 ms")
        return value

    def to_domain(self) -> PointCloudAcquisitionParameters:
        return PointCloudAcquisitionParameters(
            depth_range_mm=(self.depth_range.min_mm, self.depth_range.max_mm)
            if self.depth_range
            else None,
            roi=(self.roi.x, self.roi.y, self.roi.width, self.roi.height)
            if self.roi
            else None,
            exposure_sequence_ms=tuple(self.exposure_sequence_ms)
            if self.exposure_sequence_ms
            else None,
            surface_smoothing=self.surface_smoothing,
            noise_removal=self.noise_removal,
            outlier_removal=self.outlier_removal,
            edge_preservation=self.edge_preservation,
        )


def _validate_transformation(value: list[list[float]]) -> list[list[float]]:
    if len(value) != 4 or any(len(row) != 4 for row in value):
        raise ValueError("transformation must be a 4x4 matrix")
    if value[3] != [0.0, 0.0, 0.0, 1.0]:
        raise ValueError("the last matrix row must be [0, 0, 0, 1]")
    return value


class CaptureOptions(BaseModel):
    textured: bool = True
    custom_reference_frame: bool = False
    max_points: int = Field(default=0, ge=0)
    parameters: PointCloudParameterOptions = Field(
        default_factory=PointCloudParameterOptions
    )
    transformation: list[list[float]] | None = None

    @field_validator("transformation")
    @classmethod
    def validate_transformation(cls, value: list[list[float]] | None):
        return _validate_transformation(value) if value is not None else None

    def to_domain(self) -> PointCloudCaptureOptions:
        return PointCloudCaptureOptions(
            textured=self.textured,
            custom_reference_frame=self.custom_reference_frame,
            max_points=self.max_points,
            acquisition=self.parameters.to_domain(),
            transformation=tuple(tuple(row) for row in self.transformation)
            if self.transformation is not None
            else None,
        )


class PointCloudStatsResponse(BaseModel):
    original_points: int
    saved_points: int
    max_points: int
    transformed: bool


class CaptureResponse(BaseModel):
    capture_id: str
    captured_at: datetime
    files: dict[str, str]
    point_cloud: PointCloudStatsResponse | None = None

    @classmethod
    def from_domain(cls, capture: CaptureResult) -> "CaptureResponse":
        return cls(
            capture_id=capture.capture_id,
            captured_at=capture.captured_at,
            files=capture.files,
            point_cloud=PointCloudStatsResponse.model_validate(
                capture.point_cloud, from_attributes=True
            )
            if capture.point_cloud
            else None,
        )


class PointRequest(BaseModel):
    x: float
    y: float
    z: float

    def to_domain(self) -> Point3D:
        return Point3D(self.x, self.y, self.z)


class TransformPointsRequest(BaseModel):
    points: list[PointRequest]
    transformation: list[list[float]]

    @field_validator("transformation")
    @classmethod
    def validate_transformation(cls, value: list[list[float]]):
        return _validate_transformation(value)


class TransformPointsResponse(BaseModel):
    points: list[PointRequest]


class RtcOfferRequest(BaseModel):
    sdp: str = Field(min_length=1)
    type: str = "offer"


class RtcAnswerResponse(BaseModel):
    peer_id: str
    sdp: str
    type: str


class IceServerResponse(BaseModel):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class RtcConfigurationResponse(BaseModel):
    iceServers: list[IceServerResponse]


class RtcPeerResponse(BaseModel):
    peer_id: str
    state: str
    ice_state: str
    ice_gathering_state: str
    created_at: float
    disconnected_at: float | None
