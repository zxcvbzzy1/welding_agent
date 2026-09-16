from pathlib import Path
import sys

AGENT_FLOW_ROOT = Path(__file__).resolve().parents[3]
FAIRINO_SDK_ROOT = Path(__file__).resolve().parents[4] / "fr_robot"
for path in (AGENT_FLOW_ROOT, FAIRINO_SDK_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from fairino import Robot

try:
    import pyzed.sl as sl
except ImportError as exc:
    sl = None
    _ZED_IMPORT_ERROR = exc
else:
    _ZED_IMPORT_ERROR = None

Pose6D = List[float]
Action7D = List[float]


# {
#   "timestamp": 1760000000.123,
#   "instruction": "move the robot arm in a straight line",
#   "observation": {
#     "state": [x, y, z, rx, ry, rz],
#     "tcp_pose": [x, y, z, rx, ry, rz],
#     "cartesian_position": [x, y, z],
#     "euler_angles": [rx, ry, rz],
#     "units": {
#       "position": "mm",
#       "angle": "degree"
#     },
#     "tool_id": 0,
#     "user_id": 0,
#     "tcp_speed": [vx, vy, vz, vrx, vry, vrz],
#     "image": "xxx.jpg"
#   },
#   "action": [dx, dy, dz, drx, dry, drz, gripper],
#   "done": false,
#   "metadata": {
#     "robot": "fairino",
#     "action_space": "delta_tcp_pose_7d"
#   }
# }


@dataclass
class CartesianPoseSample:
    timestamp: float
    instruction: str
    observation: Dict[str, Any]
    action: Action7D
    done: bool = False
    metadata: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class ZEDCameraManager:
    """Small ZED SDK wrapper for image capture and camera metadata."""

    def __init__(
        self,
        image_dir: str = "openvla_images",
        resolution: Optional[Any] = None,
        fps: int = 30,
        depth_mode: Optional[Any] = None,
        coordinate_units: Optional[Any] = None,
        view: Optional[Any] = None,
        stream_ip: Optional[str] = None,
        stream_port: int = 30000,
        image_format: str = "png",
        filename_prefix: str = "zed",
    ):
        if sl is None:
            raise ImportError(
                "pyzed.sl is not available. Check the ZED SDK Python API and "
                f"its native library dependencies. Original error: {_ZED_IMPORT_ERROR}"
            )

        self.image_dir = image_dir
        self.image_format = image_format.lower().lstrip(".")
        self.filename_prefix = filename_prefix
        self.camera = sl.Camera()
        self.runtime_parameters = sl.RuntimeParameters()
        self.image = sl.Mat()
        self.depth = sl.Mat()
        self.view = view if view is not None else sl.VIEW.LEFT
        self.stream_ip = stream_ip
        self.stream_port = int(stream_port)
        self._is_open = False
        self._last_grab_timestamp_ns = None

        init_params = sl.InitParameters()
        if stream_ip:
            init_params.set_from_stream(stream_ip, self.stream_port)
        if resolution is not None:
            init_params.camera_resolution = resolution
        if fps:
            init_params.camera_fps = int(fps)
        if depth_mode is not None:
            init_params.depth_mode = depth_mode
        if coordinate_units is not None:
            init_params.coordinate_units = coordinate_units
        self.init_params = init_params

    def open(self) -> None:
        if self._is_open:
            return
        error = self.camera.open(self.init_params)
        if error != sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f"ZED camera open failed: {error}")
        os.makedirs(self.image_dir, exist_ok=True)
        self._is_open = True

    def close(self) -> None:
        if self._is_open:
            self.camera.close()
            self._is_open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def last_grab_timestamp_ns(self) -> Optional[int]:
        return self._last_grab_timestamp_ns

    def grab(self) -> bool:
        self.open()
        error = self.camera.grab(self.runtime_parameters)
        if error != sl.ERROR_CODE.SUCCESS:
            return False
        timestamp = self.camera.get_timestamp(sl.TIME_REFERENCE.IMAGE)
        self._last_grab_timestamp_ns = int(timestamp.get_nanoseconds())
        return True

    def retrieve_image(self):
        if not self.grab():
            raise RuntimeError("ZED camera grab failed")
        return self.retrieve_image_from_last_grab()

    def retrieve_image_from_last_grab(self):
        self.camera.retrieve_image(self.image, self.view, sl.MEM.CPU)
        return self.image

    def retrieve_image_array(self, grab: bool = True):
        if grab:
            image = self.retrieve_image()
        else:
            image = self.retrieve_image_from_last_grab()
        return image.get_data()

    def retrieve_image_bgr(self, grab: bool = True):
        import cv2
        import numpy as np

        frame = np.asarray(self.retrieve_image_array(grab=grab))
        if frame.ndim == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        if frame.shape[2] == 4:
            return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        return np.ascontiguousarray(frame)

    def save_image(self, path: str) -> str:
        image = self.retrieve_image()
        error = image.write(path)
        if error != sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f"ZED image write failed: {error}")
        return path

    def retrieve_depth(self, measure: Optional[Any] = None):
        if not self.grab():
            raise RuntimeError("ZED camera grab failed")
        depth_measure = measure if measure is not None else sl.MEASURE.DEPTH
        error = self.camera.retrieve_measure(
            self.depth,
            depth_measure,
            sl.MEM.CPU,
        )
        if error != sl.ERROR_CODE.SUCCESS:
            raise RuntimeError(f"ZED depth retrieve failed: {error}")
        return self.depth.get_data()

    def depth_summary(self, depth_data=None) -> Dict[str, Any]:
        import numpy as np

        depth = self.retrieve_depth() if depth_data is None else depth_data
        values = np.asarray(depth, dtype=np.float32)
        valid = np.isfinite(values) & (values > 0)
        height, width = values.shape[:2]
        center = values[height // 2, width // 2]

        summary = {
            "width": int(width),
            "height": int(height),
            "center_depth": None if not np.isfinite(center) or center <= 0 else float(center),
            "valid_ratio": float(valid.mean()),
            "unit": "mm",
        }
        if valid.any():
            valid_values = values[valid]
            summary.update(
                {
                    "min_depth": float(valid_values.min()),
                    "max_depth": float(valid_values.max()),
                    "mean_depth": float(valid_values.mean()),
                }
            )
        return summary

    def save_depth_preview(
        self,
        depth_data,
        path: str,
        min_depth: Optional[float] = None,
        max_depth: Optional[float] = None,
    ) -> str:
        import cv2
        import numpy as np

        depth = np.asarray(depth_data, dtype=np.float32)
        valid = np.isfinite(depth) & (depth > 0)
        preview = np.zeros(depth.shape[:2], dtype=np.uint8)
        if valid.any():
            lo = float(np.nanpercentile(depth[valid], 2)) if min_depth is None else float(min_depth)
            hi = float(np.nanpercentile(depth[valid], 98)) if max_depth is None else float(max_depth)
            if hi <= lo:
                hi = lo + 1.0
            normalized = np.clip((depth - lo) * 255.0 / (hi - lo), 0, 255)
            preview[valid] = normalized[valid].astype(np.uint8)
        color = cv2.applyColorMap(preview, cv2.COLORMAP_TURBO)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not cv2.imwrite(path, color):
            raise RuntimeError(f"Failed to write depth preview: {path}")
        return path

    def capture_depth(
        self,
        timestamp: Optional[float] = None,
        index: Optional[int] = None,
        save_npy: bool = True,
        save_preview: bool = True,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        import numpy as np

        capture_time = time.time() if timestamp is None else float(timestamp)
        base = os.path.splitext(self._make_filename(capture_time, index))[0]
        depth = self.retrieve_depth()
        paths = {}
        if save_npy:
            npy_path = os.path.join(self.image_dir, f"{base}_depth.npy")
            os.makedirs(os.path.dirname(os.path.abspath(npy_path)), exist_ok=True)
            np.save(npy_path, depth)
            paths["depth_npy"] = npy_path
        if save_preview:
            preview_path = os.path.join(self.image_dir, f"{base}_depth_preview.png")
            self.save_depth_preview(depth, preview_path)
            paths["depth_preview"] = preview_path
        info = self.depth_summary(depth)
        info["depth_timestamp"] = capture_time
        info["image_timestamp_ns"] = self._last_grab_timestamp_ns
        return paths, info

    def capture_image(
        self,
        timestamp: Optional[float] = None,
        index: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        capture_time = time.time() if timestamp is None else float(timestamp)
        filename = self._make_filename(capture_time, index)
        path = os.path.join(self.image_dir, filename)
        self.save_image(path)
        info = self.frame_info()
        info["image_timestamp"] = capture_time
        info["image_timestamp_ns"] = self._last_grab_timestamp_ns
        return path, info

    def frame_info(self) -> Dict[str, Any]:
        self.open()
        camera_info = self.camera.get_camera_information()
        config = camera_info.camera_configuration
        resolution = config.resolution
        info = {
            "camera": "zed",
            "serial_number": int(camera_info.serial_number),
            "model": str(camera_info.camera_model),
            "fps": int(config.fps),
            "resolution": {
                "width": int(resolution.width),
                "height": int(resolution.height),
            },
        }

        calibration = getattr(config, "calibration_parameters", None)
        left = getattr(calibration, "left_cam", None)
        if left is not None:
            info["left_camera_intrinsics"] = {
                "fx": float(left.fx),
                "fy": float(left.fy),
                "cx": float(left.cx),
                "cy": float(left.cy),
                "distortion": [float(v) for v in left.disto],
            }
        return info

    def _make_filename(self, timestamp: float, index: Optional[int]) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(timestamp))
        millis = int((timestamp % 1.0) * 1000)
        suffix = f"{index:06d}" if index is not None else f"{int(timestamp * 1000)}"
        return f"{self.filename_prefix}_{stamp}_{millis:03d}_{suffix}.{self.image_format}"

class OpenVLAEpisodeRecorder:
    """Record synchronized robot-control steps as an episode directory.

    Output layout:
    - camera_left.mp4: one frame per recorded control step
    - data.parquet: one row per recorded control step
    - meta.json: episode metadata and schema hints
    """

    def __init__(
        self,
        robot,
        episode_dir: str,
        camera: ZEDCameraManager,
        instruction: str = "",
        sample_hz: float = 10.0,
        episode_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        default_gripper_action: float = 0.0,
        video_filename: str = "camera_left.mp4",
        parquet_filename: str = "data.parquet",
        meta_filename: str = "meta.json",
    ):
        self.robot = robot
        self.episode_dir = episode_dir
        self.camera = camera
        self.instruction = instruction
        self.sample_hz = float(sample_hz)
        self.episode_id = episode_id or os.path.basename(os.path.normpath(episode_dir))
        self.metadata = metadata or {}
        self.default_gripper_action = float(default_gripper_action)
        self.video_path = os.path.join(episode_dir, video_filename)
        self.parquet_path = os.path.join(episode_dir, parquet_filename)
        self.meta_path = os.path.join(episode_dir, meta_filename)

        self._rows = []
        self._video_writer = None
        self._frame_size = None
        self._start_time = None
        self._closed = False
        self._stop_event = threading.Event()
        self._thread = None
        self._record_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._current_action = None
        self._current_instruction = None
        self._current_extra = None
        self._thread_exception = None
        self._last_pose = None

    @classmethod
    def next_episode_dir(
        cls,
        root_dir: str = ".",
        prefix: str = "episode_",
        digits: int = 6,
    ) -> str:
        os.makedirs(root_dir, exist_ok=True)
        used = []
        for name in os.listdir(root_dir):
            if not name.startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if suffix.isdigit():
                used.append(int(suffix))
        episode_index = max(used, default=0) + 1
        return os.path.join(root_dir, f"{prefix}{episode_index:0{digits}d}")

    def __enter__(self):
        self._ensure_started()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def _ensure_started(self) -> None:
        if self._start_time is not None:
            return
        os.makedirs(self.episode_dir, exist_ok=True)
        self.camera.open()
        self._start_time = time.time()
        self._closed = False

    def start(
        self,
        action: Optional[Sequence[float]] = None,
        instruction: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Start background step recording at sample_hz."""
        if self._thread and self._thread.is_alive():
            return
        self._ensure_started()
        if action is not None or instruction is not None or extra is not None:
            self.set_action(action=action, instruction=instruction, extra=extra)
        self._thread_exception = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def set_action(
        self,
        action: Optional[Sequence[float]] = None,
        instruction: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update the command action that the background recorder writes.

        Pass action=None to use observed TCP pose deltas between samples.
        """
        with self._action_lock:
            self._current_action = (
                None if action is None else [float(v) for v in action]
            )
            if instruction is not None:
                self._current_instruction = instruction
            if extra is not None:
                self._current_extra = extra

    def stop(self, mark_done: bool = True) -> None:
        """Stop background recording and write camera_left.mp4/data.parquet/meta.json."""
        if self._closed:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join()
        if mark_done:
            self.mark_done()
        self.close()
        if self._thread_exception is not None:
            raise self._thread_exception

    def record_step(
        self,
        action: Optional[Sequence[float]] = None,
        instruction: Optional[str] = None,
        done: bool = False,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Synchronously record camera frame, robot state, and action.

        If action is None, action is the observed delta TCP pose from the
        previous recorded step plus default_gripper_action.
        """
        self._ensure_started()
        with self._record_lock:
            step_index = len(self._rows)
            timestamp = time.time()

            if not self.camera.grab():
                raise RuntimeError("ZED camera grab failed")
            image = self.camera.retrieve_image_bgr(grab=False)
            observation = _current_robot_observation(self.robot)
            action_values = self._resolve_action(action, observation["tcp_pose"])

            frame = self._prepare_video_frame(image)
            self._write_video_frame(frame)

            camera_info = self.camera.frame_info() if step_index == 0 else None
            row = {
                "episode_id": self.episode_id,
                "step_index": step_index,
                "timestamp": float(timestamp),
                "elapsed_time": float(timestamp - self._start_time),
                "camera_frame_index": step_index,
                "camera_timestamp_ns": self.camera.last_grab_timestamp_ns,
                "video_path": os.path.basename(self.video_path),
                "instruction": instruction if instruction is not None else self.instruction,
                "action": action_values,
                "done": bool(done),
                "tcp_pose": observation["tcp_pose"],
                "cartesian_position": observation["cartesian_position"],
                "euler_angles": observation["euler_angles"],
                "tool_id": observation.get("tool_id"),
                "user_id": observation.get("user_id"),
                "tcp_speed": observation.get("tcp_speed"),
                "observation_json": json.dumps(observation, ensure_ascii=False),
                "action_json": json.dumps(action_values, ensure_ascii=False),
                "extra_json": json.dumps(extra or {}, ensure_ascii=False),
            }
            if camera_info is not None:
                row["camera_json"] = json.dumps(camera_info, ensure_ascii=False)
            else:
                row["camera_json"] = None
            self._rows.append(row)
            return row

    def record_for(
        self,
        seconds: float,
        action_provider=None,
        default_action: Optional[Sequence[float]] = None,
        instruction: Optional[str] = None,
    ) -> None:
        deadline = time.time() + float(seconds)
        period = 1.0 / self.sample_hz
        step_index = 0
        while time.time() < deadline:
            loop_start = time.time()
            if action_provider is None and default_action is None:
                action = None
            elif action_provider is None:
                action = default_action
            else:
                action = action_provider(step_index, loop_start)
            self.record_step(action=action, instruction=instruction)
            step_index += 1
            sleep_time = period - (time.time() - loop_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
        self.mark_done()

    def mark_done(self) -> None:
        with self._record_lock:
            if not self._rows:
                return
            self._rows[-1]["done"] = True

    def close(self) -> None:
        if self._closed:
            return
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()
        if self._video_writer is not None:
            self._video_writer.release()
            self._video_writer = None
        self._write_parquet()
        self._write_meta()
        self._closed = True

    def _run(self) -> None:
        period = 1.0 / self.sample_hz
        while not self._stop_event.is_set():
            loop_start = time.time()
            with self._action_lock:
                action = None if self._current_action is None else list(self._current_action)
                instruction = self._current_instruction
                extra = dict(self._current_extra or {})
            try:
                self.record_step(
                    action=action,
                    instruction=instruction,
                    extra=extra,
                )
            except Exception as exc:
                self._thread_exception = exc
                self._stop_event.set()
                break
            sleep_time = period - (time.time() - loop_start)
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _resolve_action(
        self,
        action: Optional[Sequence[float]],
        pose: Pose6D,
    ) -> Action7D:
        if action is not None:
            return [float(v) for v in action]
        if self._last_pose is None:
            resolved = [
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,
                self.default_gripper_action,
            ]
        else:
            resolved = [
                float(pose[i] - self._last_pose[i])
                for i in range(6)
            ] + [self.default_gripper_action]
        self._last_pose = list(pose)
        return resolved

    def _prepare_video_frame(self, image):
        import cv2
        import numpy as np

        frame = np.asarray(image)
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        elif frame.shape[2] == 4:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        return np.ascontiguousarray(frame)

    def _write_video_frame(self, frame) -> None:
        import cv2

        height, width = frame.shape[:2]
        if self._video_writer is None:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._frame_size = (int(width), int(height))
            self._video_writer = cv2.VideoWriter(
                self.video_path,
                fourcc,
                self.sample_hz,
                self._frame_size,
            )
            if not self._video_writer.isOpened():
                raise RuntimeError(f"Failed to open video writer: {self.video_path}")
        self._video_writer.write(frame)

    def _write_parquet(self) -> None:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ImportError(
                "Writing data.parquet requires pyarrow. Install it in this "
                "environment, then rerun the episode recording. Example: "
                "python -m pip install pyarrow"
            ) from exc

        table = pa.Table.from_pylist(self._rows)
        pq.write_table(table, self.parquet_path)

    def _write_meta(self) -> None:
        meta = {
            "episode_id": self.episode_id,
            "schema_version": "openvla_episode_v1",
            "created_at": self._start_time,
            "closed_at": time.time(),
            "sample_hz": self.sample_hz,
            "step_period_sec": 1.0 / self.sample_hz,
            "num_steps": len(self._rows),
            "files": {
                "camera_left": os.path.basename(self.video_path),
                "data": os.path.basename(self.parquet_path),
                "meta": os.path.basename(self.meta_path),
            },
            "columns": {
                "action": "upcoming delta tcp pose [dx, dy, dz, drx, dry, drz, gripper]",
                "tcp_pose": "[x, y, z, rx, ry, rz]",
                "camera_frame_index": "0-based frame index in camera_left.mp4",
            },
            "units": {"position": "mm", "angle": "degree"},
            "metadata": self.metadata,
        }
        if self._frame_size is not None:
            meta["video"] = {
                "width": self._frame_size[0],
                "height": self._frame_size[1],
                "fps": self.sample_hz,
            }
        try:
            meta["camera"] = self.camera.frame_info()
        except Exception as exc:
            meta["camera_error"] = str(exc)
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)


class FairinoLinearMotion:
    """Small wrapper around FAIRINO MoveL for Cartesian linear motion."""

    def __init__(
        self,
        robot,
        tool: int = 0,
        user: int = 0,
        vel: float = 20.0,
        acc: float = 0.0,
        ovl: float = 100.0,
        blendR: float = -1.0,
        max_linear_delta: Optional[float] = 50.0,
        max_angle_delta: Optional[float] = 20.0,
    ):
        self.robot = robot
        self.tool = int(tool)
        self.user = int(user)
        self.vel = float(vel)
        self.acc = float(acc)
        self.ovl = float(ovl)
        self.blendR = float(blendR)
        self.max_linear_delta = max_linear_delta
        self.max_angle_delta = max_angle_delta

    @classmethod
    def connect(
        cls,
        robot_ip: str,
        tool: int = 0,
        user: int = 0,
        vel: float = 20.0,
        acc: float = 0.0,
        ovl: float = 100.0,
        blendR: float = -1.0,
        max_linear_delta: Optional[float] = 50.0,
        max_angle_delta: Optional[float] = 20.0,
    ):
        robot = Robot.RPC(robot_ip)
        return cls(
            robot=robot,
            tool=tool,
            user=user,
            vel=vel,
            acc=acc,
            ovl=ovl,
            blendR=blendR,
            max_linear_delta=max_linear_delta,
            max_angle_delta=max_angle_delta,
        )

    def current_pose(self) -> Pose6D:
        return _get_actual_tcp_pose(self.robot)

    def move_to(
        self,
        desc_pos: Sequence[float],
        tool: Optional[int] = None,
        user: Optional[int] = None,
        vel: Optional[float] = None,
        acc: Optional[float] = None,
        ovl: Optional[float] = None,
        blendR: Optional[float] = None,
        **kwargs,
    ) -> int:
        pose = self._validate_pose(desc_pos)
        error = self.robot.MoveL(
            desc_pos=pose,
            tool=self.tool if tool is None else int(tool),
            user=self.user if user is None else int(user),
            vel=self.vel if vel is None else float(vel),
            acc=self.acc if acc is None else float(acc),
            ovl=self.ovl if ovl is None else float(ovl),
            blendR=self.blendR if blendR is None else float(blendR),
            **kwargs,
        )
        if error != 0:
            raise RuntimeError(f"MoveL failed: {error}")
        return error

    def move_by(
        self,
        delta_pose: Sequence[float],
        check_delta: bool = True,
        **kwargs,
    ) -> Pose6D:
        delta = self._validate_pose(delta_pose)
        if check_delta:
            self._check_delta(delta)
        target = [
            current + delta_value
            for current, delta_value in zip(self.current_pose(), delta)
        ]
        self.move_to(target, **kwargs)
        return target

    @staticmethod
    def _validate_pose(pose: Sequence[float]) -> Pose6D:
        values = [float(v) for v in pose]
        if len(values) != 6:
            raise ValueError("pose must be [x, y, z, rx, ry, rz]")
        return values

    def _check_delta(self, delta: Pose6D) -> None:
        if self.max_linear_delta is not None:
            max_linear = max(abs(v) for v in delta[:3])
            if max_linear > float(self.max_linear_delta):
                raise ValueError(
                    "move_by expects a small delta pose, but linear delta "
                    f"{delta[:3]} exceeds {self.max_linear_delta} mm. "
                    "Use move_to() for an absolute target pose, or pass "
                    "check_delta=False intentionally."
                )
        if self.max_angle_delta is not None:
            max_angle = max(abs(v) for v in delta[3:])
            if max_angle > float(self.max_angle_delta):
                raise ValueError(
                    "move_by expects a small delta pose, but angle delta "
                    f"{delta[3:]} exceeds {self.max_angle_delta} degree. "
                    "Use move_to() for an absolute target pose, or pass "
                    "check_delta=False intentionally."
                )


def _safe_sdk_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except TypeError as exc:
        if "_ctypes.CField" in str(exc) or "not subscriptable" in str(exc):
            return None
        raise


def _safe_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float_list(value, expected_len: int) -> Optional[List[float]]:
    try:
        values = [float(value[i]) for i in range(expected_len)]
    except (TypeError, ValueError, IndexError):
        return None
    return values


def _current_robot_observation(robot) -> Dict[str, Any]:
    pose = _get_actual_tcp_pose(robot, flag=1)
    observation = {
        "state": pose,
        "tcp_pose": pose,
        "cartesian_position": pose[:3],
        "euler_angles": pose[3:],
        "units": {"position": "mm", "angle": "degree"},
    }

    tool_result = _safe_sdk_call(robot.GetActualTCPNum)
    if isinstance(tool_result, tuple) and tool_result[0] == 0:
        tool_id = _safe_int(tool_result[1])
        if tool_id is not None:
            observation["tool_id"] = tool_id

    user_result = _safe_sdk_call(robot.GetActualWObjNum)
    if isinstance(user_result, tuple) and user_result[0] == 0:
        user_id = _safe_int(user_result[1])
        if user_id is not None:
            observation["user_id"] = user_id

    speed_result = _safe_sdk_call(robot.GetActualTCPSpeed)
    if isinstance(speed_result, tuple) and speed_result[0] == 0:
        tcp_speed = _safe_float_list(speed_result[1], 6)
        if tcp_speed is not None:
            observation["tcp_speed"] = tcp_speed

    return observation


def _get_actual_tcp_pose(robot, flag: int = 1, timeout: float = 3.0) -> Pose6D:
    deadline = time.time() + timeout
    last_error = None

    while time.time() < deadline:
        try:
            result = robot.GetActualTCPPose(flag)
            if isinstance(result, tuple) and result[0] == 0:
                return [float(v) for v in result[1]]
            last_error = result
        except TypeError as exc:
            if "_ctypes.CField" not in str(exc) and "not subscriptable" not in str(exc):
                raise
            last_error = exc

        direct_pose = _get_actual_tcp_pose_from_xmlrpc(robot, flag)
        if direct_pose is not None:
            return direct_pose

        state_pose = _get_actual_tcp_pose_from_state(robot)
        if state_pose is not None:
            return state_pose

        time.sleep(0.05)

    raise RuntimeError(
        "GetActualTCPPose failed because robot realtime state is not ready. "
        "Check robot connection and realtime state port 20004. "
        f"Last error: {last_error}"
    )


def _get_actual_tcp_pose_from_xmlrpc(robot, flag: int) -> Optional[Pose6D]:
    server = getattr(robot, "robot", None)
    if server is None:
        return None

    try:
        result = server.GetActualTCPPose(int(flag))
    except Exception:
        return None

    if not isinstance(result, (list, tuple)) or len(result) < 7:
        return None
    if int(result[0]) != 0:
        return None
    return [float(v) for v in result[1:7]]


def _get_actual_tcp_pose_from_state(robot) -> Optional[Pose6D]:
    state = getattr(robot, "robot_state_pkg", None)
    pose = getattr(state, "tl_cur_pos", None)
    if pose is None or not hasattr(pose, "__getitem__"):
        return None

    try:
        return [float(pose[i]) for i in range(6)]
    except TypeError:
        return None

if __name__ == "__main__":
    print("Import OpenVLACartesianRecorder and FairinoLinearMotion from this module.")
    print("This file does not move the robot when run directly.")
