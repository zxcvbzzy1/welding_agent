from pathlib import Path
import asyncio
import math
import os
import sys
from threading import Event as ThreadEvent
from typing import Sequence

AGENT_FLOW_ROOT = Path(__file__).resolve().parents[3]
FAIRINO_SDK_ROOT = Path(__file__).resolve().parents[4] / "fr_robot"
for path in (AGENT_FLOW_ROOT, FAIRINO_SDK_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import factory, bus
from infra.event_bind import On_bind
from infra.tool.common_func import human_approval_service
from infra.tool.help.openvla_motion import FairinoLinearMotion


ROBOT_IP = "192.168.58.2"
TOOL = 0
USER = 0
VEL = 20.0
ACC = 0.0
OVL = 100.0
BLEND_R = -1.0
MAX_DELTA_VALUE = 300.0


# auto_confirm: "ask" 弹窗询问用户 ｜ "approve" 自动批准 ｜ "reject" 自动拒绝（headless/测试用）
_ROBOT_SETTINGS: dict[str, str] = {"auto_confirm": "ask"}


def get_robot_settings() -> dict:
    return dict(_ROBOT_SETTINGS)


def set_robot_settings(auto_confirm: str | None = None) -> dict:
    if auto_confirm in {"ask", "approve", "reject"}:
        _ROBOT_SETTINGS["auto_confirm"] = auto_confirm
    return get_robot_settings()


ROBOT_MOVE_TO = Tool(
    name="robot_move_to",
    description=(
        "控制 FAIRINO 机器人 TCP 直线移动到绝对目标位姿。"
        "只需要提供 target_pose: [x, y, z, rx, ry, rz]。"
    ),
    field="robot",
    input_schema={
        "type": "object",
        "properties": {
            "target_pose": {
                "type": "array",
                "description": "绝对目标 TCP 位姿 [x, y, z, rx, ry, rz]，位置单位 mm，角度单位 degree",
                "items": {"type": "number"},
                "minItems": 6,
                "maxItems": 6,
            }
        },
        "required": ["target_pose"],
        "additionalProperties": False,
    },
    metadata={"require_human_confirm": True},
)


ROBOT_MOVE_BY = Tool(
    name="robot_move_by",
    description=(
        "控制 FAIRINO 机器人 TCP 按增量做直线运动。"
        "只需要提供 delta_pose: [dx, dy, dz, drx, dry, drz]"
    ),
    field="robot",
    input_schema={
        "type": "object",
        "properties": {
            "delta_pose": {
                "type": "array",
                "description": "TCP 增量 [dx, dy, dz, drx, dry, drz]",
                "items": {"type": "number", "minimum": -MAX_DELTA_VALUE, "maximum": MAX_DELTA_VALUE},
                "minItems": 6,
                "maxItems": 6,
            }
        },
        "required": ["delta_pose"],
        "additionalProperties": False,
    },
    metadata={"require_human_confirm": True},
)


ROBOT_GET_CURRENT_POSE = Tool(
    name="robot_get_current_pose",
    description="获取 FAIRINO 机器人当前末端执行器 TCP 位姿，返回格式为 [x, y, z, rx, ry, rz]。",
    field="robot",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


ROBOT_MOVE_SEAM = Tool(
    name="robot_move_seam", field="robot",
    description=(
        "读取 point_cloud_weld_seams 输出的 JSON，执行指定单条焊缝的接近、沿缝直线运动和退刀。"
        "默认 dry_run=true，仅校验和展示轨迹，不连接机器人。实际执行前必须已到轨迹接近点，"
        "且当前工具为指定的已标定焊枪 TCP、工件坐标系为 0；位置/姿态误差不得超过 2 mm/2 度。"
        "tool_id 必须与提取时的 TCP 轴向修正对应。实际运动需要人工确认，任一点失败即停止。"
        "不负责焊缝间空移，不控制起弧。vel 为速度百分比。"
    ),
    input_schema={
        "type": "object", "additionalProperties": False,
        "required": ["trajectory_path", "seam_id", "tool_id"],
        "properties": {
            "trajectory_path": {"type": "string", "description": "提取工具输出的 trajectory.json 本地路径"},
            "seam_id": {"type": "string", "description": "单条焊缝编号，例如 L1"},
            "tool_id": {"type": "integer", "minimum": 1, "maximum": 14, "description": "已标定焊枪工具编号"},
            "vel": {"type": "number", "exclusiveMinimum": 0, "maximum": 100, "default": 5.0},
            "dry_run": {"type": "boolean", "default": True},
        },
    },
    metadata={"require_human_confirm": True},
)


class RobotMotionTool:
    def __init__(self):
        self.robot_ip = ROBOT_IP

    def move_to(self, target_pose: Sequence[float]) -> dict:
        pose = _validate_pose(target_pose, "target_pose")
        with FairinoLinearMotion.motion_lock:
            motion = self._connect_motion()
            try:
                motion.move_to(pose)
                return {
                    "message": "机器人运动完成",
                    "mode": "move_to",
                    "target_pose": _round_pose(pose),
                }
            finally:
                motion.robot.CloseRPC()

    def move_by(self, delta_pose: Sequence[float]) -> dict:
        delta = _validate_pose(delta_pose, "delta_pose")
        _validate_delta_limit(delta)
        with FairinoLinearMotion.motion_lock:
            motion = self._connect_motion()
            try:
                reached_pose = motion.move_by(delta, check_delta=True)
                return {
                    "message": "机器人运动完成",
                    "mode": "move_by",
                    "delta_pose": _round_pose(delta),
                    "reached_pose": _round_pose(reached_pose),
                }
            finally:
                motion.robot.CloseRPC()

    def get_current_pose(self) -> dict:
        with FairinoLinearMotion.motion_lock:
            motion = self._connect_motion()
            try:
                pose = motion.current_pose()
                return {
                    "message": "获取机器人当前末端执行器姿态成功",
                    "tcp_pose": _round_pose(pose),
                }
            finally:
                motion.robot.CloseRPC()

    def move_seam(self, trajectory_path, seam_id, tool_id, *, vel=5.0, dry_run=True, cancel_event=None):
        from infra.tool.help.point_cloud_welding.motion import execute_seam

        return execute_seam(trajectory_path, seam_id, tool_id, vel=vel, dry_run=dry_run,
                            robot_ip=self.robot_ip, cancel_event=cancel_event)

    def _connect_motion(self) -> FairinoLinearMotion:
        return FairinoLinearMotion.connect(
            robot_ip=self.robot_ip,
            tool=TOOL,
            user=USER,
            vel=VEL,
            acc=ACC,
            ovl=OVL,
            blendR=BLEND_R,
            max_linear_delta=MAX_DELTA_VALUE,
            max_angle_delta=MAX_DELTA_VALUE,
        )


def _validate_pose(values: Sequence[float], field_name: str) -> list[float]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} 必须是长度为 6 的数组")
    pose = [float(v) for v in values]
    if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
        raise ValueError(f"{field_name} 必须是长度为 6 的有限数值数组")
    return pose


def _round_pose(values: Sequence[float] | None) -> list[float] | None:
    if values is None:
        return None
    return [round(float(value), 2) for value in values]


def _validate_delta_limit(delta_pose: Sequence[float]) -> None:
    for index, value in enumerate(delta_pose):
        if abs(float(value)) > MAX_DELTA_VALUE:
            raise ValueError(
                f"delta_pose[{index}]={value} 超过安全限制，"
                f"每个值绝对值必须 <= {MAX_DELTA_VALUE}"
            )


def _failed_event(agent_id: str, tool_name: str, message: str) -> Event:
    tool_respond = Tool_respond(
        agent_id=agent_id,
        name=tool_name,
        success=False,
        respond=f"机器人运动失败: {message}",
    )
    return factory.tool(tool_name).failed(tool_respond)


def _succeeded_event(agent_id: str, tool_name: str, respond: dict) -> Event:
    tool_respond = Tool_respond(
        agent_id=agent_id,
        name=tool_name,
        success=True,
        respond=respond,
    )
    return factory.tool(tool_name).succeeded(tool_respond)


on_tool = On_bind()
factory._build_and_register_list(
    [ROBOT_MOVE_TO, ROBOT_MOVE_BY, ROBOT_GET_CURRENT_POSE, ROBOT_MOVE_SEAM],
    bus,
)


@on_tool.on(factory.tool("robot_move_to").called())
async def robot_move_to(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = await asyncio.to_thread(RobotMotionTool().move_to, kwargs["target_pose"])
    except Exception as exc:
        return _failed_event(agent_id, "robot_move_to", str(exc))
    return _succeeded_event(agent_id, "robot_move_to", respond)


@on_tool.on(factory.tool("robot_move_by").called())
async def robot_move_by(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = await asyncio.to_thread(RobotMotionTool().move_by, kwargs["delta_pose"])
    except Exception as exc:
        return _failed_event(agent_id, "robot_move_by", str(exc))
    return _succeeded_event(agent_id, "robot_move_by", respond)


@on_tool.on(factory.tool("robot_get_current_pose").called())
async def robot_get_current_pose(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = await asyncio.to_thread(RobotMotionTool().get_current_pose)
    except Exception as exc:
        return _failed_event(agent_id, "robot_get_current_pose", str(exc))
    return _succeeded_event(agent_id, "robot_get_current_pose", respond)


@on_tool.on(factory.tool("robot_move_seam").called())
async def robot_move_seam(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    cancelled = ThreadEvent()
    try:
        arguments = {key: kwargs[key] for key in ROBOT_MOVE_SEAM.input_schema["properties"] if key in kwargs}
        worker = asyncio.create_task(asyncio.to_thread(
            RobotMotionTool().move_seam, **arguments, cancel_event=cancelled))
        try:
            response = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A blocking MoveL cannot be withdrawn, but no subsequent point may
            # be submitted after the owning run is cancelled. Wait for cleanup.
            cancelled.set()
            await asyncio.shield(worker)
            raise
    except Exception as exc:
        return _failed_event(agent_id, "robot_move_seam", str(exc))
    if not response["success"]:
        return factory.tool("robot_move_seam").failed(Tool_respond(
            agent_id=agent_id, name="robot_move_seam", success=False, respond=response))
    return _succeeded_event(agent_id, "robot_move_seam", response)


@on_tool.on(Event("human.robot_move_seam"))
async def confirm_robot_move_seam(**kwargs) -> Event:
    arguments = kwargs.get("arguments", {})
    if arguments.get("dry_run", True) is True:
        return _human_confirm_event("robot_move_seam", True, "离线轨迹预览无需运动确认")
    return await _confirm_robot_motion(
        tool_name="robot_move_seam", agent_id=arguments.get("agent_id", ""),
        detail_name="trajectory", detail_value=arguments,
        prompt=(f"单条焊缝运动：{arguments.get('trajectory_path')} / {arguments.get('seam_id')}，"
                f"tool={arguments.get('tool_id')}，速度百分比={arguments.get('vel', 5.0)}。"
                "是否允许执行接近、沿缝运动和退刀？输入 yes/y 允许，其它拒绝: "),
    )


@on_tool.on(Event("human.robot_move_to"))
async def confirm_robot_move_to(**kwargs) -> Event:
    arguments = kwargs.get("arguments", {})
    target_pose = arguments.get("target_pose", "")
    return await _confirm_robot_motion(
        tool_name="robot_move_to",
        agent_id=arguments.get("agent_id", ""),
        detail_name="target_pose",
        detail_value=target_pose,
        prompt="是否允许机器人移动到该 target_pose？输入 yes/y 允许，其它拒绝: ",
    )


@on_tool.on(Event("human.robot_move_by"))
async def confirm_robot_move_by(**kwargs) -> Event:
    arguments = kwargs.get("arguments", {})
    delta_pose = arguments.get("delta_pose", "")
    return await _confirm_robot_motion(
        tool_name="robot_move_by",
        agent_id=arguments.get("agent_id", ""),
        detail_name="delta_pose",
        detail_value=delta_pose,
        prompt="是否允许机器人按该 delta_pose 直线运动？输入 yes/y 允许，其它拒绝: ",
    )


async def _confirm_robot_motion(
    tool_name: str,
    agent_id: str,
    detail_name: str,
    detail_value,
    prompt: str,
) -> Event:
    """人机协作中间件会对机器人运动工具调用先进入这里。

    环境变量优先（headless），其次读取工具页/运行时 auto_confirm 配置；
    ask 时通过 human_approval_service 触发 Web 或终端确认。
    """
    env_auto = os.getenv("AGENT_FLOW_AUTO_CONFIRM", "").lower()
    auto = get_robot_settings().get("auto_confirm", "ask")
    if env_auto in {"1", "true", "yes", "y"} or auto == "approve":
        return _human_confirm_event(
            tool_name,
            True,
            "机器人运动自动批准（auto_confirm=approve）",
        )
    if env_auto in {"0", "false", "no", "n"} or auto == "reject":
        return _human_confirm_event(
            tool_name,
            False,
            "机器人运动自动拒绝（auto_confirm=reject）",
        )

    print(f"\n[HUMAN CONFIRM] {tool_name} 工具请求执行：agent_id={agent_id} {detail_name}={detail_value}")
    answer = (await human_approval_service.input(prompt)).strip().lower()
    approved = answer in {"yes", "y"}
    return _human_confirm_event(
        tool_name,
        approved,
        f"用户确认执行 {tool_name}" if approved else f"用户拒绝执行 {tool_name}，{answer}",
    )


def _human_confirm_event(tool_name: str, approved: bool, reason: str) -> Event:
    return Event(
        f"human.{tool_name}.confirmed",
        payload={
            "approved": approved,
            "reason": reason,
        },
    )
