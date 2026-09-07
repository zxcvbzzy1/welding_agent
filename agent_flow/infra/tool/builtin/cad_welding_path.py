"""CAD 焊接轨迹工具的声明、注册与事件处理。"""

from __future__ import annotations

import asyncio

from domain.event import Event
from domain.tool import Tool, Tool_respond
from infra.config import bus, factory
from infra.event_bind import On_bind
from infra.tool.builtin.welding.cad_path import generate_cad_welding_path

CAD_WELDING_PATH = Tool(
    name="cad_welding_path",
    field="system",
    description=(
        "从本地 CAD 三角网格提取内角焊缝并生成焊接轨迹预览 PNG 和 CSV。"
        "支持 STL/OBJ/PLY/OFF/GLB/GLTF，输入坐标按毫米解释，默认绕 X 轴旋转 90 度。"
        "产物保存到 agent_flow/temp/welding，返回 image_path、csv_path 和 "
        "inline_artifact_arguments；将后者原样传给 inline_artifact 即可展示图片。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "model_path": {"type": "string", "description": "本地网格文件绝对路径"},
            "min_length_threshold": {"type": "number", "exclusiveMinimum": 0, "default": 5.0,
                                     "description": "最小焊缝长度（毫米）"},
            "rotation_x_deg": {"type": "number", "default": 90.0, "description": "绕 X 轴旋转角度；不旋转填 0"},
            "home_pos": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                         "description": "旋转后模型坐标系的 Home [x,y,z]；省略则自动计算"},
            "approach_dist": {"type": "number", "exclusiveMinimum": 0,
                              "description": "下枪/退刀距离，默认模型最大尺寸的 0.5 倍"},
            "safe_clearance": {"type": "number", "exclusiveMinimum": 0,
                               "description": "空移表面采样安全间距，默认模型最大尺寸的 0.05 倍"},
            "max_iter": {"type": "integer", "minimum": 1, "maximum": 50000, "default": 3000},
            "seed": {"type": "integer", "minimum": 0, "default": 0, "description": "随机种子，便于复现"},
        },
        "required": ["model_path"],
        "additionalProperties": False,
    },
)


on_tool = On_bind()
factory._build_and_register_list([CAD_WELDING_PATH], bus)


@on_tool.on(factory.tool("cad_welding_path").called())
async def cad_welding_path(**kwargs) -> Event:
    agent_id = kwargs.get("agent_id", "")
    try:
        # 科学计算和渲染在工作线程执行，避免阻塞事件总线；启动时不加载重依赖。
        def generate():
            arguments = {
                key: kwargs[key] for key in CAD_WELDING_PATH.input_schema["properties"]
                if key in kwargs
            }
            return generate_cad_welding_path(**arguments)

        result = await asyncio.to_thread(generate)
    except Exception as exc:
        return factory.tool("cad_welding_path").failed(Tool_respond(
            agent_id=agent_id, name="cad_welding_path", success=False,
            respond=f"CAD 焊接轨迹生成失败: {exc}",
        ))
    return factory.tool("cad_welding_path").succeeded(Tool_respond(
        agent_id=agent_id, name="cad_welding_path", success=True, respond=result,
    ))
