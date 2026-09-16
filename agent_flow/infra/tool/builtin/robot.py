from pathlib import Path
import os
import sys
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
from fairino import Robot


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


class RobotMotionTool:
    def __init__(self):
        self.robot_ip = ROBOT_IP

    def move_to(self, target_pose: Sequence[float]) -> dict:
        pose = _validate_pose(target_pose, "target_pose")
        robot = Robot.RPC(self.robot_ip)
        try:
            motion = self._make_motion(robot)
            motion.move_to(pose)
            return {
                "message": "机器人运动完成",
                "mode": "move_to",
                "target_pose": _round_pose(pose),
            }
        finally:
            robot.CloseRPC()

    def move_by(self, delta_pose: Sequence[float]) -> dict:
        delta = _validate_pose(delta_pose, "delta_pose")
        _validate_delta_limit(delta)
        robot = Robot.RPC(self.robot_ip)
        try:
            motion = self._make_motion(robot)
            reached_pose = motion.move_by(delta, check_delta=True)
            return {
                "message": "机器人运动完成",
                "mode": "move_by",
                "delta_pose": _round_pose(delta),
                "reached_pose": _round_pose(reached_pose),
            }
        finally:
            robot.CloseRPC()

    def get_current_pose(self) -> dict:
        robot = Robot.RPC(self.robot_ip)
        try:
            motion = self._make_motion(robot)
            pose = motion.current_pose()
            return {
                "message": "获取机器人当前末端执行器姿态成功",
                "tcp_pose": _round_pose(pose),
            }
        finally:
            robot.CloseRPC()

    def _make_motion(self, robot) -> FairinoLinearMotion:
        return FairinoLinearMotion(
            robot=robot,
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
    if len(pose) != 6:
        raise ValueError(f"{field_name} 必须是长度为 6 的数组")
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
    [ROBOT_MOVE_TO, ROBOT_MOVE_BY, ROBOT_GET_CURRENT_POSE],
    bus,
)


@on_tool.on(factory.tool("robot_move_to").called())
def robot_move_to(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = RobotMotionTool().move_to(kwargs["target_pose"])
    except Exception as exc:
        return _failed_event(agent_id, "robot_move_to", str(exc))
    return _succeeded_event(agent_id, "robot_move_to", respond)


@on_tool.on(factory.tool("robot_move_by").called())
def robot_move_by(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = RobotMotionTool().move_by(kwargs["delta_pose"])
    except Exception as exc:
        return _failed_event(agent_id, "robot_move_by", str(exc))
    return _succeeded_event(agent_id, "robot_move_by", respond)


@on_tool.on(factory.tool("robot_get_current_pose").called())
def robot_get_current_pose(**kwargs) -> Event:
    agent_id = kwargs["agent_id"]
    try:
        respond = RobotMotionTool().get_current_pose()
    except Exception as exc:
        return _failed_event(agent_id, "robot_get_current_pose", str(exc))
    return _succeeded_event(agent_id, "robot_get_current_pose", respond)


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
