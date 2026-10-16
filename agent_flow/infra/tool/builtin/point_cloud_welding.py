"""Point-cloud seam tool declaration and event binding, following system.py."""

import asyncio

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind


def _number(default, description, *, zero=False):
    return {"type": "number", "minimum" if zero else "exclusiveMinimum": 0,
            "default": default, "description": description}


_VECTOR3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
POINT_CLOUD_WELD_SEAMS = Tool(
    name="point_cloud_weld_seams", field="system",
    description=(
        "按 Geng 等 RCIM 2023 论文从本地点云提取三平面/H 型钢/方管对接焊缝，"
        "自动计算焊枪 TCP 姿态及接近退刀点，生成 JSON/CSV/PNG。构件须水平放置、底面 +Z 向上。"
        "点云可为 PLY、普通 PCD 或 XYZ；必须明确输入坐标系，相机点云须给 camera_to_base，"
        "基座点云须给 camera_position_base；H 型钢和方管须给实际宽度。"
        "返回轨迹路径和焊缝编号，可交给 robot_move_seam 预览或执行。"
        "只计算轨迹，不连接机器人；姿态支撑不足的焊缝标记不可执行。"
        "将 inline_artifact_arguments 原样传给 inline_artifact 可展示预览图片。"
    ),
    input_schema={
        "type": "object", "additionalProperties": False,
        "required": ["point_cloud_path", "model_type", "input_frame"],
        "properties": {
            "point_cloud_path": {"type": "string", "description": "本地点云文件路径"},
            "model_type": {"type": "string", "enum": ["three_plane", "h_section", "square_tube"]},
            "input_frame": {"type": "string", "enum": ["robot_base", "camera"]},
            "unit": {"type": "string", "enum": ["mm", "m"], "default": "mm"},
            "camera_to_base": {"type": "array", "minItems": 4, "maxItems": 4,
                               "items": {"type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "number"}},
                               "description": "基座←相机 4×4 刚性变换，平移单位 mm；仅相机点云必填"},
            "camera_position_base": {**_VECTOR3, "description": "基座点云必填：扫描时相机在基座坐标系中的位置，单位 mm"},
            "h_width_mm": {"type": "number", "exclusiveMinimum": 0, "description": "H 型钢实际宽度，H 型钢必填"},
            "tube_width_mm": {"type": "number", "exclusiveMinimum": 0, "description": "方管实际宽度，方管必填"},
            "voxel_size_mm": _number(1, "平面拟合降采样体素；0 表示关闭", zero=True),
            "sample_step_mm": _number(2, "沿缝最大采样间距"),
            "posture_width_mm": _number(5, "姿态计算的横截面邻域宽度"),
            "approach_distance_mm": _number(10, "沿理想枪轴反方向的接近距离"),
            "retract_distance_mm": _number(10, "沿理想枪轴反方向的退刀距离"),
            "plane_distance_threshold_mm": _number(2, "RANSAC 粗分割距离阈值"),
            "ransac_iterations": {"type": "integer", "minimum": 1, "maximum": 10000, "default": 10},
            "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "default": 0},
            "tcp_rotation_offset_deg": {**_VECTOR3, "default": [0, 0, 0],
                "description": "R_理想枪_TCP 的固定 XYZ 欧拉角，度；R_base_TCP=R_base_理想枪·R_理想枪_TCP；TCP 原点须为标定焊枪尖端"},
        },
    },
)

on_tool = On_bind()
factory._build_and_register_list([POINT_CLOUD_WELD_SEAMS], bus)


@on_tool.on(factory.tool("point_cloud_weld_seams").called())
async def point_cloud_weld_seams(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    try:
        from infra.tool.help.point_cloud_welding import extract_weld_seams

        arguments = {key: kwargs[key] for key in POINT_CLOUD_WELD_SEAMS.input_schema["properties"] if key in kwargs}
        response = await asyncio.to_thread(extract_weld_seams, **arguments)
    except Exception as exc:
        return factory.tool("point_cloud_weld_seams").failed(Tool_respond(
            agent_id=agent_id, name="point_cloud_weld_seams", success=False,
            respond=f"点云焊缝提取失败: {exc}"))
    return factory.tool("point_cloud_weld_seams").succeeded(Tool_respond(
        agent_id=agent_id, name="point_cloud_weld_seams", success=True, respond=response))
