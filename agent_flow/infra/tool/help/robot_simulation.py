"""HTTP planning adapter and the existing immutable front-end snapshot reader."""
import json
import os
import re
from pathlib import Path
from .weld_service_client import run_job

ROOT = Path(__file__).resolve().parents[4]


class SimulationCancelled(Exception):
    pass


def snapshot_root():
    local_workspace = Path(__file__).resolve().parents[2] / "FR_Weld_Seam_Planning_ws"
    default = local_workspace / "output/service/simulations" if local_workspace.is_dir() else ROOT / "agent_flow/store/robot_simulations"
    return Path(os.getenv("ROBOT_SIMULATION_ROOT", str(default))).expanduser().resolve()


def strict_json(raw):
    def invalid(value):
        raise ValueError(f"JSON contains invalid number {value}")
    return json.loads(raw, parse_constant=invalid)


def build_simulation(trajectory_path, *, run_id, seam_id=None, tcp_offset_pose=None, speed_mm_s=20, cancel=None):
    arguments = {"trajectory_path": str(trajectory_path), "run_id": run_id, "speed_mm_s": speed_mm_s}
    if seam_id is not None:
        arguments["seam_id"] = seam_id
    if tcp_offset_pose is not None:
        arguments["tcp_offset_pose"] = tcp_offset_pose
    return run_job("/api/v1/simulations", arguments, cancel=cancel)


def read_simulation(identity):
    if not isinstance(identity, str) or re.fullmatch(r"[a-f0-9]{32}", identity) is None:
        raise FileNotFoundError("仿真不存在")
    path = snapshot_root() / f"{identity}.json"
    try:
        return strict_json(path.read_bytes())
    except FileNotFoundError:
        from .weld_service_client import request, WeldServiceError
        from .weld_service_files import cache_json
        try:
            data = request("GET", f"/api/v1/simulations/{identity}")
        except WeldServiceError as exc:
            if exc.status_code == 404:
                raise FileNotFoundError("仿真不存在") from exc
            raise
        if data.get("simulation_id") != identity:
            raise ValueError("服务返回的仿真 ID 不匹配")
        cache_json(path, data)
        return data


def simulation_artifact(data, title="机械臂轨迹仿真"):
    return {"type": "robot_simulation", "title": title, "simulation_id": data["simulation_id"],
            "url": f"/api/im/robot-simulations/{data['simulation_id']}", "run_id": data["run_id"],
            "summary": data["summary"], "tcp_calibrated": data["tcp_calibrated"], "editable": False,
            "mime_type": "application/json"}
