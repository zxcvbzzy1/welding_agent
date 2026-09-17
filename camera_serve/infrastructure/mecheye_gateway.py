from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from mecheye.area_scan_3d_camera import (
    Camera,
    ColorTypeOf2DCamera_Color,
    ColorTypeOf2DCamera_Monochrome,
    Frame2D,
    Frame2DAnd3D,
    Frame3D,
    PointCloudEdgePreservation,
    PointCloudNoiseRemoval,
    PointCloudOutlierRemoval,
    PointCloudSurfaceSmoothing,
    Scanning3DDepthRange,
    Scanning3DExposureSequence,
    Scanning3DROI,
    get_transformation_params,
    transform_point_cloud,
    transform_textured_point_cloud,
)
from mecheye.shared import FileFormat_PLY, ROI, RangeInt

from camera_serve.domain.exceptions import (
    CameraAlreadyConnectedError,
    CameraNotConnectedError,
    CameraNotFoundError,
    CameraSdkError,
    InvalidTransformationError,
)
from camera_serve.domain.models import CameraDescriptor, PointCloudAcquisitionParameters


class MechEyeCameraGateway:
    """Thin synchronous adapter around the Mech-Eye area scan SDK."""

    def __init__(self) -> None:
        self._camera = Camera()
        self._current: CameraDescriptor | None = None

    def discover(self) -> list[CameraDescriptor]:
        return [self._descriptor(info) for info in Camera.discover_cameras()]

    def connect(self, serial_number: str) -> CameraDescriptor:
        if self._current is not None:
            if self._current.serial_number == serial_number:
                return self._current
            raise CameraAlreadyConnectedError(
                f"Camera {self._current.serial_number} is already connected"
            )

        camera_info = next(
            (
                info
                for info in Camera.discover_cameras()
                if info.serial_number == serial_number
            ),
            None,
        )
        if camera_info is None:
            raise CameraNotFoundError(f"Camera not found: {serial_number}")

        self._check(self._camera.connect(camera_info), "connect camera")
        self._current = self._descriptor(camera_info)
        return self._current

    def disconnect(self) -> None:
        if self._current is None:
            return
        self._camera.disconnect()
        self._current = None

    def current_camera(self) -> CameraDescriptor | None:
        return self._current

    def capture_2d(self, output_path: Path) -> None:
        frame = Frame2D()
        self._check(self._camera.capture_2d(frame), "capture 2D image")
        self._write_image(output_path, self._frame_to_ndarray(frame))

    def capture_depth(self, output_path: Path) -> None:
        frame = Frame3D()
        self._check(self._camera.capture_3d(frame), "capture depth map")
        self._write_image(output_path, frame.get_depth_map().data())

    def capture_point_cloud(
        self,
        untextured_path: Path,
        textured_path: Path | None,
        custom_reference_frame: bool,
        acquisition: PointCloudAcquisitionParameters,
    ) -> None:
        with self._temporary_parameters(acquisition):
            if textured_path is None:
                frame_3d = Frame3D()
                self._check(self._camera.capture_3d(frame_3d), "capture 3D frame")
                self._save_untextured(
                    frame_3d, untextured_path, custom_reference_frame
                )
                return

            frame = Frame2DAnd3D()
            self._check(
                self._camera.capture_2d_and_3d(frame), "capture 2D and 3D frame"
            )
            self._save_untextured(
                frame.frame_3d(), untextured_path, custom_reference_frame
            )
            self._save_textured(frame, textured_path, custom_reference_frame)

    def capture_all(
        self,
        image_path: Path,
        depth_path: Path,
        untextured_path: Path,
        textured_path: Path | None,
        custom_reference_frame: bool,
        acquisition: PointCloudAcquisitionParameters,
    ) -> None:
        with self._temporary_parameters(acquisition):
            frame = Frame2DAnd3D()
            self._check(
                self._camera.capture_2d_and_3d(frame), "capture 2D and 3D frame"
            )
            self._write_image(image_path, self._frame_to_ndarray(frame.frame_2d()))
            self._write_image(depth_path, frame.frame_3d().get_depth_map().data())
            self._save_untextured(
                frame.frame_3d(), untextured_path, custom_reference_frame
            )
            if textured_path is not None:
                self._save_textured(frame, textured_path, custom_reference_frame)

    def capture_live_frame(self) -> np.ndarray:
        frame = Frame2D()
        self._check(self._camera.capture_2d(frame), "capture live 2D frame")
        image = self._frame_to_ndarray(frame)
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        return np.ascontiguousarray(image.copy())

    def _save_untextured(
        self, frame: Frame3D, output_path: Path, custom_reference_frame: bool
    ) -> None:
        if custom_reference_frame:
            transformation = self._custom_transformation()
            cloud = transform_point_cloud(
                transformation, frame.get_untextured_point_cloud()
            )
            status = Frame3D.save_point_cloud(
                cloud, FileFormat_PLY, str(output_path)
            )
        else:
            status = frame.save_untextured_point_cloud(
                FileFormat_PLY, str(output_path)
            )
        self._check(status, "save untextured point cloud")

    def _save_textured(
        self,
        frame: Frame2DAnd3D,
        output_path: Path,
        custom_reference_frame: bool,
    ) -> None:
        if custom_reference_frame:
            transformation = self._custom_transformation()
            cloud = transform_textured_point_cloud(
                transformation, frame.get_textured_point_cloud()
            )
            status = Frame2DAnd3D.save_point_cloud(
                cloud, FileFormat_PLY, str(output_path)
            )
        else:
            status = frame.save_textured_point_cloud(
                FileFormat_PLY, str(output_path)
            )
        self._check(status, "save textured point cloud")

    def _custom_transformation(self):
        transformation = get_transformation_params(self._camera)
        if not transformation.__is__valid__():
            raise InvalidTransformationError(
                "Custom reference frame is not configured in Mech-Eye Viewer"
            )
        return transformation

    @contextmanager
    def _temporary_parameters(
        self, parameters: PointCloudAcquisitionParameters
    ) -> Iterator[None]:
        """Apply capture-scoped values and restore the in-memory User Set."""
        user_set = self._camera.current_user_set()
        restorations: list[tuple[object, str, object]] = []

        def replace(getter_name: str, setter_name: str, name: str, value: object) -> None:
            getter = getattr(user_set, getter_name)
            setter = getattr(user_set, setter_name)
            status, original = getter(name)
            self._check(status, f"read camera parameter {name}")
            restorations.append((setter, name, original))
            self._check(setter(name, value), f"set camera parameter {name}")

        processing_parameters = (
            (
                PointCloudSurfaceSmoothing,
                parameters.surface_smoothing,
            ),
            (PointCloudNoiseRemoval, parameters.noise_removal),
            (PointCloudOutlierRemoval, parameters.outlier_removal),
            (PointCloudEdgePreservation, parameters.edge_preservation),
        )

        try:
            if parameters.depth_range_mm is not None:
                replace(
                    "get_range_value",
                    "set_range_value",
                    Scanning3DDepthRange.name,
                    RangeInt(*parameters.depth_range_mm),
                )
            if parameters.roi is not None:
                replace(
                    "get_roi_value",
                    "set_roi_value",
                    Scanning3DROI.name,
                    ROI(*parameters.roi),
                )
            if parameters.exposure_sequence_ms is not None:
                replace(
                    "get_float_array_value",
                    "set_float_array_value",
                    Scanning3DExposureSequence.name,
                    list(parameters.exposure_sequence_ms),
                )
            for definition, level in processing_parameters:
                if level is not None:
                    replace(
                        "get_enum_value",
                        "set_enum_value",
                        definition.name,
                        getattr(definition, f"Value_{level}"),
                    )
            yield
        finally:
            for setter, name, original in reversed(restorations):
                self._check(setter(name, original), f"restore camera parameter {name}")

    @staticmethod
    def _frame_to_ndarray(frame: Frame2D) -> np.ndarray:
        if frame.color_type() == ColorTypeOf2DCamera_Monochrome:
            return frame.get_gray_scale_image().data()
        if frame.color_type() == ColorTypeOf2DCamera_Color:
            return frame.get_color_image().data()
        raise CameraSdkError("read 2D frame", -1, "Unsupported camera color type")

    @staticmethod
    def _write_image(output_path: Path, image: np.ndarray) -> None:
        if not cv2.imwrite(str(output_path), image):
            raise CameraSdkError("save image", -1, f"Cannot write {output_path}")

    @staticmethod
    def _check(status, operation: str) -> None:
        if not status.is_ok():
            raise CameraSdkError(
                operation,
                int(status.error_code),
                str(status.error_description),
            )

    @staticmethod
    def _descriptor(info) -> CameraDescriptor:
        return CameraDescriptor(
            serial_number=str(info.serial_number),
            model=str(info.model),
            ip_address=str(info.ip_address),
            port=int(info.port),
            device_name=str(info.device_name),
            hardware_version=info.hardware_version.to_string(),
            firmware_version=info.firmware_version.to_string(),
        )
