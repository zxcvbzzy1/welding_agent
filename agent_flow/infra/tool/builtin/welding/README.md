# CAD 焊接轨迹工具

`CADTpath copy.py` 现在是命令行兼容入口，算法和离屏渲染位于 `cad_path.py`。
`builtin/cad_welding_path.py` 集中声明 `cad_welding_path` 工具、注册事件并处理调用，格式与 `system.py` 一致。
`application/services/tools.py` 在启动时加载该工具；`welding/` 包保留算法、示例模型和命令行入口。

使用项目环境安装依赖（版本也已记录在根目录 `requirements.txt`）：

```bash
/Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python -m pip install trimesh==5.1.0 scipy==1.18.1
```

## Agent 调用

调用 `cad_welding_path`：

```json
{
  "model_path": "/Users/zxcvbzzy1/Desktop/项目/Welding_AgentHub/agent_flow/infra/tool/builtin/welding/small3.stl"
}
```

返回 `image_path`、`csv_path`、`seam_count`、`point_count`、计算参数以及
`inline_artifact_arguments`。将 `inline_artifact_arguments` 对象直接作为
`inline_artifact` 工具参数即可在消息中展示图片。例如：

```json
{
  "artifact_type": "image",
  "image": {
    "title": "welding_trajectory.png",
    "file_path": "/Users/zxcvbzzy1/Desktop/项目/Welding_AgentHub/agent_flow/temp/welding/<本次生成的ID>/welding_trajectory.png",
    "mime_type": "image/png"
  }
}
```

`inline_artifact` 将本地图片转为 data URL 交给已有产物事件链路，支持消息展示和图片打包下载，无需启动 HTTP 服务。
本地图片限定在 `agent_flow/temp` 内，最大 10 MiB；原有 `image.url` 参数仍可使用，不能与 `file_path` 同时填写。

每次生成保存在独立的 `agent_flow/temp/welding/<ID>/` 目录，包含 PNG 预览和 CSV，不覆盖旧产物。
CSV 包含 Index、Stage、相对 Home 的 XYZ（毫米）和 Rx/Ry/Rz（度）。

## 参数与边界

- 输入为 STL/OBJ/PLY/OFF/GLB/GLTF 三角网格，数值坐标按毫米解释；STEP/IGES 需先转换，米制模型需先缩放。
- 默认保留原脚本绕 X 轴旋转 90 度的行为，`rotation_x_deg: 0` 可关闭旋转。
- `min_length_threshold` 默认 5 mm，仅提取近似垂直面的内凹共享边。
- `home_pos` 是旋转后模型坐标系的 `[x,y,z]`；省略则按包围盒生成。
- `approach_dist` 和 `safe_clearance` 默认分别为最大尺寸的 0.5 和 0.05 倍。
- `max_iter` 默认 3000，`seed` 默认 0；固定种子可复现表面采样和随机路径。
- 避障失败返回 failed 事件，不再强行用直线连接。平滑轨迹会复检，失败则保留规划折线。
- 这是几何轨迹预览，沿用原脚本的表面点云避障；没有验证实体内部、焊枪体积、下枪/退刀碰撞或机器人运动学，不能据此直接驱动机器人。

## 本地运行与验证

在仓库根目录运行，不指定模型时使用同目录的 `small3.stl`：

```bash
/Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python 'agent_flow/infra/tool/builtin/welding/CADTpath copy.py'
/Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python -m pytest test_cad_welding_path.py im_backend/tests/test_inline_artifacts.py -v
```
