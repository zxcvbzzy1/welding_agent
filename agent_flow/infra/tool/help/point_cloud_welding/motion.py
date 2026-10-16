"""Validated single-seam trajectory execution. SDK imports are deferred."""

import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from .io import positive, vector
from ..openvla_motion import FairinoLinearMotion


def load_seam(trajectory_path, seam_id):
    source = Path(trajectory_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"轨迹文件不存在: {source}")
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("轨迹 JSON 超过 64 MiB")

    def invalid_constant(value):
        raise ValueError(f"轨迹含非法数值: {value}")

    try:
        document = json.loads(source.read_text(encoding="utf-8"), parse_constant=invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"轨迹 JSON 无效: {exc}") from exc
    if not isinstance(document, dict) or type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("不支持的轨迹 schema_version")
    if (document.get("frame") != "robot_base"
            or document.get("units") != {"position": "mm", "angle": "degree"}
            or document.get("rotation_convention") != "Rz(rz) Ry(ry) Rx(rx)"
            or document.get("tcp_origin") != "calibrated_welding_tip"):
        raise ValueError("轨迹必须为机器人基座坐标、毫米/度及标定焊枪尖端 TCP")
    seams = document.get("seams")
    if not isinstance(seams, list) or not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in seams):
        raise ValueError("轨迹 seams 无效")
    if len({s["id"] for s in seams}) != len(seams):
        raise ValueError("轨迹焊缝编号重复")
    selected = next((s for s in seams if s["id"] == seam_id), None)
    if selected is None:
        raise ValueError(f"焊缝不存在: {seam_id}")
    if selected.get("executable") is not True:
        raise ValueError("该焊缝姿态不可用，禁止执行")
    waypoints = selected.get("waypoints")
    if not isinstance(waypoints, list) or not 4 <= len(waypoints) <= 50000:
        raise ValueError("轨迹点数量必须在 4—50000 之间")
    expected_stages = ["APPROACH", *(["WELDING"] * (len(waypoints) - 2)), "RETRACT"]
    poses = []
    for index, (item, stage) in enumerate(zip(waypoints, expected_stages)):
        if not isinstance(item, dict) or item.get("stage") != stage:
            raise ValueError("轨迹阶段必须为 APPROACH、连续 WELDING、RETRACT")
        poses.append(vector(item.get("pose"), 6, f"waypoints[{index}].pose"))
    poses = np.asarray(poses)
    weld = np.asarray(selected.get("poses"), dtype=float)
    if weld.shape != poses[1:-1].shape or not np.allclose(weld, poses[1:-1], atol=1e-8, rtol=0):
        raise ValueError("poses 与焊接阶段位姿不一致")
    start = vector(selected.get("start"), 3, "start")
    end = vector(selected.get("end"), 3, "end")
    if (not np.allclose(start, poses[1, :3], atol=1e-6, rtol=0)
            or not np.allclose(end, poses[-2, :3], atol=1e-6, rtol=0)
            or not np.allclose(vector(selected.get("approach_pose"), 6, "approach_pose"), poses[0], atol=1e-8, rtol=0)
            or not np.allclose(vector(selected.get("retract_pose"), 6, "retract_pose"), poses[-1], atol=1e-8, rtol=0)):
        raise ValueError("轨迹端点元数据不一致")
    parameters = document.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("轨迹缺少参数")
    step = positive(parameters.get("sample_step_mm"), "sample_step_mm")
    length = np.linalg.norm(end - start)
    if length < 3:
        raise ValueError("焊缝长度退化")
    tangent = (end - start) / length
    relative = poses[1:-1, :3] - start
    along = relative @ tangent
    if (np.any(np.diff(along) <= 0)
            or np.max(np.linalg.norm(relative - along[:, None] * tangent, axis=1)) > 1e-5
            or np.max(np.linalg.norm(np.diff(poses[1:-1, :3], axis=0), axis=1)) > step + 1e-5):
        raise ValueError("焊接采样点必须按顺序沿有限直线排列，且不得超过采样间距")
    offset = vector(parameters.get("tcp_rotation_offset_deg"), 3, "tcp_rotation_offset_deg")
    correction = Rotation.from_euler("xyz", offset, degrees=True).as_matrix()
    rotations = Rotation.from_euler("xyz", poses[:, 3:], degrees=True).as_matrix()
    if not np.allclose(rotations, rotations[1], atol=1e-6):
        raise ValueError("当前版本的平面直线焊缝要求一致的 TCP 姿态，禁止姿态跳变")
    for pose_index, weld_index, distance_name in ((0, 1, "approach_distance_mm"), (-1, -2, "retract_distance_mm")):
        distance = positive(parameters.get(distance_name), distance_name)
        ideal_z = (rotations[weld_index] @ correction.T)[:, 2]
        expected = poses[weld_index, :3] - ideal_z * distance
        if (not np.allclose(poses[pose_index, :3], expected, atol=1e-5, rtol=0)
                or not np.allclose(rotations[pose_index], rotations[weld_index], atol=1e-7)):
            raise ValueError("接近/退刀点与枪轴或指定距离不一致")
    return source, selected, waypoints


def execute_seam(trajectory_path, seam_id, tool_id, *, vel=5.0, dry_run=True,
                 robot_ip="192.168.58.2", cancel_event=None):
    if type(tool_id) is not int or not 1 <= tool_id <= 14:
        raise ValueError("tool_id 必须为已标定焊枪的工具编号 1—14")
    velocity = positive(vel, "vel")
    if velocity > 100:
        raise ValueError("vel 是速度百分比，必须 <= 100")
    if type(dry_run) is not bool:
        raise ValueError("dry_run 必须为布尔值")
    source, seam, waypoints = load_seam(trajectory_path, seam_id)
    response = {
        "success": True, "dry_run": dry_run, "trajectory_path": str(source),
        "seam_id": seam_id, "tool_id": tool_id, "user_id": 0,
        "vel": velocity, "blendR": -1.0, "completed_count": 0,
        "command_count": len(waypoints) - 1,
    }
    if dry_run:
        return {**response, "message": "轨迹校验通过；执行前须先到接近点并应用匹配的焊枪 TCP 和 user=0",
                "required_start_pose": waypoints[0]["pose"], "waypoints": waypoints}
    robot = None
    stage, point_index = "PREFLIGHT", None
    with FairinoLinearMotion.motion_lock:
        try:
            if cancel_event is not None and cancel_event.is_set():
                stage = "CANCELLED"
                raise RuntimeError("轨迹执行已取消，停止发送后续点")
            motion = FairinoLinearMotion.connect(robot_ip=robot_ip, tool=tool_id, user=0,
                                                vel=velocity, acc=0.0, ovl=100.0, blendR=-1.0)
            robot = motion.robot
            for method, expected, name in (("GetActualTCPNum", tool_id, "工具编号"),
                                             ("GetActualWObjNum", 0, "工件坐标系")):
                result = getattr(robot, method)()
                if (not isinstance(result, (tuple, list)) or len(result) != 2
                        or result[0] != 0 or result[1] != expected):
                    raise ValueError(f"当前{name}不匹配或读取失败，期望 {expected}，实际 {result}")
            current = vector(motion.current_pose(), 6, "当前 TCP 位姿")
            approach = np.asarray(waypoints[0]["pose"])
            position_error = float(np.linalg.norm(current[:3] - approach[:3]))
            current_rotation = Rotation.from_euler("xyz", current[3:], degrees=True)
            target_rotation = Rotation.from_euler("xyz", approach[3:], degrees=True)
            angle_error = float(np.rad2deg((current_rotation.inv() * target_rotation).magnitude()))
            response.update({"start_position_error_mm": position_error, "start_angle_error_deg": angle_error})
            if position_error > 2.0 or angle_error > 2.0:
                raise ValueError("机器人尚未到接近点：位置/姿态误差必须 <= 2 mm / 2 degree")
            # The APPROACH point has already been reached; do not perform an
            # unplanned move from an arbitrary current pose to the workpiece.
            for point_index, waypoint in enumerate(waypoints[1:], 1):
                if cancel_event is not None and cancel_event.is_set():
                    stage = "CANCELLED"
                    raise RuntimeError("轨迹执行已取消，停止发送后续点")
                stage = waypoint["stage"]
                motion.move_to(waypoint["pose"])
                response["completed_count"] += 1
            response["message"] = "单条焊缝接近、沿缝运动和退刀完成"
        except Exception as exc:
            response.update({"success": False, "failed_stage": stage, "failed_point_index": point_index,
                             "sdk_error": f"{type(exc).__name__}: {exc}", "message": str(exc)})
        finally:
            if robot is not None:
                try:
                    robot.CloseRPC()
                except Exception as exc:
                    response["close_error"] = str(exc)
                    if response["success"]:
                        response.update({"success": False, "failed_stage": "CLOSE_RPC", "failed_point_index": None,
                                         "sdk_error": str(exc), "message": "运动完成，但机器人连接关闭失败"})
    return response
