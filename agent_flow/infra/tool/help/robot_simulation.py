"""Offline FR5 kinematics and immutable playback snapshots. No robot SDK/network."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from threading import Event
from uuid import uuid4
import xml.etree.ElementTree as ET

import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

ROOT = Path(__file__).resolve().parents[4]
URDF_PATH = ROOT / "urdf" / "fairino5_v6.urdf"
MAX_BYTES = 64 * 1024 * 1024
MAX_POINTS = 50000


class SimulationCancelled(Exception):
    pass


def snapshot_root():
    return Path(os.getenv("ROBOT_SIMULATION_ROOT", str(ROOT / "agent_flow" / "store" / "robot_simulations"))).expanduser().resolve()


def vector(value, size, name):
    if not isinstance(value, (list, tuple, np.ndarray)):
        raise ValueError(f"{name} 必须为 {size} 维有限数值数组")
    try:
        if np.asarray(value).dtype.kind not in "iuf" or any(isinstance(v, (bool, np.bool_)) for v in value):
            raise ValueError()
        result = np.asarray(value, dtype=float)
    except (ValueError, TypeError):
        raise ValueError(f"{name} 必须为 {size} 维有限数值数组") from None
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f"{name} 必须为 {size} 维有限数值数组")
    return result


def pose_matrix(pose):
    pose = vector(pose, 6, "pose")
    transform = np.eye(4)
    transform[:3, 3] = pose[:3] / 1000
    transform[:3, :3] = Rotation.from_euler("xyz", pose[3:], degrees=True).as_matrix()
    return transform


def matrix_pose(transform):
    return np.r_[transform[:3, 3] * 1000, Rotation.from_matrix(transform[:3, :3]).as_euler("xyz", degrees=True)].tolist()


def strict_json(raw):
    def invalid(value):
        raise ValueError(f"JSON 含非法数值 {value}")
    try:
        return json.loads(raw, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("轨迹文件不是有效的 UTF-8 JSON") from None


def load_trajectory(path, seam_id=None):
    if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
        raise ValueError("trajectory_path 必须为 JSON 文件路径")
    if seam_id is not None and (not isinstance(seam_id, str) or not seam_id.strip()):
        raise ValueError("seam_id 必须为非空字符串")
    source = Path(path).expanduser().resolve()
    if source.suffix.lower() != ".json" or not source.is_file():
        raise ValueError("trajectory_path 必须指向存在的 JSON 文件")
    # Read a bounded snapshot once, so source edits cannot change the simulation.
    with source.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("轨迹 JSON 超过 64 MiB")
    data = strict_json(raw)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("轨迹必须为 schema_version=1")
    if (data.get("frame") != "robot_base" or data.get("units") != {"position": "mm", "angle": "degree"}
            or data.get("rotation_convention") != "Rz(rz) Ry(ry) Rx(rx)"):
        raise ValueError("轨迹必须使用基座坐标、mm/degree 和 Rz(rz) Ry(ry) Rx(rx)")
    if not isinstance(data.get("seams"), list):
        raise ValueError("轨迹必须包含 seams 数组")
    normalized, identities, total = [], set(), 0
    for seam in data["seams"]:
        if not isinstance(seam, dict) or not isinstance(seam.get("id"), str) or not seam["id"].strip():
            raise ValueError("焊缝必须包含非空字符串 id")
        if seam["id"] in identities:
            raise ValueError("轨迹焊缝 ID 重复")
        identities.add(seam["id"])
        start = vector(seam.get("start"), 3, "seam.start").tolist()
        end = vector(seam.get("end"), 3, "seam.end").tolist()
        rows = seam.get("waypoints", [])
        if not isinstance(rows, list):
            raise ValueError("waypoints 必须为数组")
        if "poses" in seam and not isinstance(seam["poses"], list):
            raise ValueError("poses 必须为数组")
        if not rows and seam.get("poses"):
            if not isinstance(seam["poses"], list):
                raise ValueError("poses 必须为数组")
            rows = [{"stage": "WELDING", "pose": p} for p in seam["poses"]]
        total += len(rows)
        if total > MAX_POINTS:
            raise ValueError("轨迹总点数超过 50,000，不允许截断")
        points = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("stage"), str)
                    or row["stage"] not in {"APPROACH", "WELDING", "RETRACT"}):
                raise ValueError("轨迹点 stage 必须为 APPROACH/WELDING/RETRACT")
            points.append({"stage": row["stage"], "pose": vector(row.get("pose"), 6, "waypoint.pose").tolist()})
        normalized.append({"id": seam["id"], "start": start, "end": end, "points": points})
    if seam_id is not None and seam_id not in identities:
        raise ValueError(f"焊缝不存在: {seam_id}")
    return source, raw, data, [s for s in normalized if seam_id is None or s["id"] == seam_id]


class RobotKinematics:
    def __init__(self, urdf_path=URDF_PATH):
        root = ET.parse(urdf_path).getroot()
        children = {j.find("child").get("link"): j for j in root.findall("joint")}
        chain, link = [], "wrist3_link"
        while link != "base_link":
            joint = children[link]
            chain.append(joint)
            link = joint.find("parent").get("link")
        self.names, self.origins, self.axes, bounds, velocities = [], [], [], [], []
        for joint in reversed(chain):
            if joint.get("type") != "revolute":
                raise ValueError("当前 FR5 求解器要求六个转动关节")
            origin = joint.find("origin")
            transform = np.eye(4)
            transform[:3, 3] = np.fromstring(origin.get("xyz", "0 0 0"), sep=" ")
            transform[:3, :3] = Rotation.from_euler("xyz", np.fromstring(origin.get("rpy", "0 0 0"), sep=" ")).as_matrix()
            axis = np.fromstring(joint.find("axis").get("xyz"), sep=" ")
            limit = joint.find("limit")
            self.names.append(joint.get("name")); self.origins.append(transform)
            self.axes.append(axis / np.linalg.norm(axis))
            bounds.append([float(limit.get("lower")), float(limit.get("upper"))])
            velocities.append(float(limit.get("velocity")))
        if len(self.names) != 6:
            raise ValueError("FR5 模型必须包含六个关节")
        self.bounds = np.asarray(bounds).T
        self.velocities = np.asarray(velocities)
        self.reach = sum(np.linalg.norm(o[:3, 3]) for o in self.origins)

    def forward(self, q, tcp=None):
        transform = np.eye(4)
        for origin, axis, angle in zip(self.origins, self.axes, vector(q, 6, "joints")):
            rotation = np.eye(4)
            rotation[:3, :3] = Rotation.from_rotvec(axis * angle).as_matrix()
            transform = transform @ origin @ rotation
        return transform if tcp is None else transform @ tcp

    def solve(self, target, seed, tcp, cancel=None):
        if np.linalg.norm(target[:3, 3]) > self.reach + np.linalg.norm(tcp[:3, 3]) + .001:
            return None, {"reason": "目标超出模型最大工作半径"}
        def residual(q):
            if cancel is not None and cancel.is_set():
                raise SimulationCancelled()
            actual = self.forward(q, tcp)
            return np.r_[actual[:3, 3] - target[:3, 3],
                         Rotation.from_matrix(target[:3, :3] @ actual[:3, :3].T).as_rotvec() * .2]
        seeds = [seed, np.array([0, -1, 1, -.5, .8, 0]),
                 np.array([0, -.5, -1, 1, -.8, 0]), self.bounds.mean(axis=0)]
        best = None
        for initial in seeds:
            result = least_squares(residual, np.clip(initial, *self.bounds), bounds=self.bounds,
                                   max_nfev=120, xtol=1e-9, ftol=1e-9, gtol=1e-9)
            actual = self.forward(result.x, tcp)
            position = float(np.linalg.norm(actual[:3, 3] - target[:3, 3]) * 1000)
            angle = float(np.rad2deg(Rotation.from_matrix(target[:3, :3] @ actual[:3, :3].T).magnitude()))
            diagnostic = {"position_error_mm": position, "angle_error_deg": angle}
            if position <= 1 and angle <= 1:
                return result.x, diagnostic
            best = diagnostic if best is None or position < best["position_error_mm"] else best
        return None, {**best, "reason": "未找到满足关节限位及 1 mm / 1° 误差的逆解"}


def build_simulation(trajectory_path, *, run_id, seam_id=None, tcp_offset_pose=None, speed_mm_s=20, cancel=None):
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("仿真发布需要有效的运行上下文")
    if isinstance(speed_mm_s, bool) or not isinstance(speed_mm_s, (int, float)) or not np.isfinite(speed_mm_s) or speed_mm_s <= 0:
        raise ValueError("speed_mm_s 必须为有限正数")
    source, raw, original, seams = load_trajectory(trajectory_path, seam_id)
    model = RobotKinematics()
    offset = vector([0] * 6 if tcp_offset_pose is None else tcp_offset_pose, 6, "tcp_offset_pose")
    tcp = pose_matrix(offset)
    failures, reachable, discontinuities = 0, 0, 0
    for seam in seams:
        seed = np.zeros(6)
        segment = None
        seam["segments"] = []
        for index, point in enumerate(seam["points"]):
            if cancel is not None and cancel.is_set():
                raise SimulationCancelled()
            q, diagnostic = model.solve(pose_matrix(point["pose"]), seed, tcp, cancel)
            point.update(diagnostic)
            point.update(joints=None if q is None else q.tolist(), time_s=0., minimum_duration_s=0.,
                         status="unreachable" if q is None else "reachable")
            if q is None:
                failures += 1
                segment = None
                continue
            reachable += 1
            jump = segment is not None and np.max(np.abs(q - seed)) > np.pi / 2
            if jump:
                discontinuities += 1
                point["reason"] = "关节变化超过 90°，重置到新的连续段，不跨段插值"
            if segment is None or jump:
                segment = {"start": index, "end": index, "duration_s": 0.}
                seam["segments"].append(segment)
            else:
                previous = seam["points"][index - 1]
                distance = np.linalg.norm(np.asarray(point["pose"][:3]) - previous["pose"][:3])
                point["minimum_duration_s"] = float(np.max(np.abs(q - seed) / model.velocities))
                duration = max(float(distance / speed_mm_s), point["minimum_duration_s"], .02)
                point["time_s"] = previous["time_s"] + duration
                segment.update(end=index, duration_s=point["time_s"])
            seed = q
        if not seam["points"]:
            seam["reason"] = "缺少 TCP 姿态，仅显示焊缝几何"
    identity = uuid4().hex
    summary = {"seam_count": len(seams), "point_count": sum(len(s["points"]) for s in seams),
               "reachable_count": reachable, "unreachable_count": failures, "discontinuity_count": discontinuities,
               "geometry_only_count": sum(not s["points"] for s in seams)}
    data = {"schema_version": 1, "simulation_id": identity, "run_id": run_id,
            "created_at": time.time(), "robot": "fairino5_v6", "joint_names": model.names,
            "joint_velocity_limits_rad_s": model.velocities.tolist(), "joint_units": "radian",
            "frame": "robot_base", "units": {"position": "mm", "angle": "degree"},
            "tcp_offset_pose": offset.tolist(), "tcp_calibrated": tcp_offset_pose is not None,
            "speed_mm_s": speed_mm_s, "source_path": str(source), "source_sha256": hashlib.sha256(raw).hexdigest(),
            "input_snapshot": original, "summary": summary, "seams": seams}
    if cancel is not None and cancel.is_set():
        raise SimulationCancelled()
    root = snapshot_root()
    root.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        os.replace(temporary, root / f"{identity}.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return simulation_artifact(data)


def read_simulation(identity):
    if not isinstance(identity, str) or re.fullmatch(r"[a-f0-9]{32}", identity) is None:
        raise FileNotFoundError("仿真不存在")
    path = snapshot_root() / f"{identity}.json"
    return strict_json(path.read_bytes())


def simulation_artifact(data, title="机械臂轨迹仿真"):
    return {"type": "robot_simulation", "title": title, "simulation_id": data["simulation_id"],
            "url": f"/api/im/robot-simulations/{data['simulation_id']}", "run_id": data["run_id"],
            "summary": data["summary"], "tcp_calibrated": data["tcp_calibrated"], "editable": False,
            "mime_type": "application/json"}
