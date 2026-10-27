"""Publish offline robot playback from a local trajectory JSON path."""
import asyncio
from threading import Event as ThreadEvent

from domain.event import Event
from domain.run_context import current_run_id
from domain.runtime_hooks import get_run_context_provider
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind
from infra.tool.builtin.artifacts import InlineArtifactTool

ROBOT_SIMULATE_TRAJECTORY = Tool(
    name="robot_simulate_trajectory", field="robot",
    description=("读取 trajectory_path 指向的本地轨迹 JSON，通过 FastAPI/MoveIt 求解并检查 FR5 关节运动并自动发布循环播放的内联仿真。"
                 "输入是文件路径而非 JSON 内容；兼容 point_cloud_weld_seams 的 trajectory_path。"
                 "默认展示全部焊缝，可在卡片内选择 seam_id。不可达点标记失败，不连接或控制真实机械臂。"
                 "tcp_offset_pose 是法兰到焊枪 TCP 的安装位姿，mm/degree；省略时使用临时仿真偏移 [0,0,200,0,0,0]，不代表实际标定。"),
    input_schema={"type": "object", "additionalProperties": False, "required": ["trajectory_path"],
        "properties": {
            "trajectory_path": {"type": "string", "description": "服务器上 schema_version=1 轨迹 JSON 文件路径"},
            "seam_id": {"type": "string", "description": "可选：仅预览指定焊缝；省略展示全部"},
            "tcp_offset_pose": {"type": "array", "items": {"type": "number"}, "minItems": 6, "maxItems": 6,
                                "description": "法兰到 TCP [x,y,z,rx,ry,rz]，mm/degree，Rz Ry Rx"},
            "speed_mm_s": {"type": "number", "exclusiveMinimum": 0, "default": 20}}},
)
on_tool = On_bind()
factory._build_and_register_list([ROBOT_SIMULATE_TRAJECTORY], bus)


@on_tool.on(factory.tool("robot_simulate_trajectory").called())
async def robot_simulate_trajectory(**kwargs):
    from infra.tool.help.robot_simulation import build_simulation
    agent_id = kwargs.get("agent_id", "")
    cancel = ThreadEvent()
    try:
        provider = get_run_context_provider()
        run_id = current_run_id.get() or (provider.run_id_for_agent(agent_id) if provider and agent_id else "")
        arguments = {k: kwargs[k] for k in ROBOT_SIMULATE_TRAJECTORY.input_schema["properties"] if k in kwargs}
        artifact = await asyncio.to_thread(build_simulation, run_id=run_id, cancel=cancel, **arguments)
        payload = InlineArtifactTool().build_event_payload({"artifact_type": "robot_simulation",
                    "robot_simulation": {"simulation_id": artifact["simulation_id"]}, "run_id": run_id, "agent_id": agent_id})
        await bus.publish(Event(payload["event_name"], payload=payload))
    except asyncio.CancelledError:
        cancel.set()
        raise
    except Exception as exc:
        return factory.tool("robot_simulate_trajectory").failed(Tool_respond(
            agent_id=agent_id, name="robot_simulate_trajectory", success=False, respond=f"轨迹仿真失败: {exc}"))
    return factory.tool("robot_simulate_trajectory").succeeded(Tool_respond(
        agent_id=agent_id, name="robot_simulate_trajectory", success=True, respond=payload))
