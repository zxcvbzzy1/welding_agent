> 跨机器输入已支持：帮助层会上传工具机器上可读的本地文件，下载提取结果并清理临时上传。服务地址使用 `WELD_SERVICE_URL=http://100.82.31.1:18800`；手动启动及 curl 上传示例见工作区 `docs/MANUAL_START.md`。只传一个两边均读不到的第三台机器路径仍不可用，需要先上传文件内容。

> 当前工具入口已改为调用 FastAPI 服务，算法实现位于 `FR_Weld_Seam_Planning_ws/service/weld_service/point_cloud/`。设置 `WELD_SERVICE_URL` 和共享产物目录后使用；完整部署及 API 见工作区 `docs/FASTAPI_SERVICE.md`。下文方法说明继续适用，旧离线仿真说明以新版服务文档为准。

# 通用平面相交焊缝提取

采用 Geng 等论文 *A novel seam extraction and path planning method for robotic welding of medium-thickness plate structural parts* 的核心方法：局部近邻 RANSAC、多平面质心精拟合和二面角姿态计算。DOI: https://doi.org/10.1016/j.rcim.2022.102433；论文原文位于 `docs/`。

实现不按三平面、H 型钢、方管模板枚举焊缝，不要求工件水平放置。自动寻找真实点云支持的有限平面交线，输出内角和外角的几何候选焊缝。交线不等于经工艺确认的焊接位置；共面且没有可见边界的对接缝和曲面焊缝不属于当前范围。

## 调用

```json
{
  "point_cloud_path": "/absolute/path/workpiece.ply",
  "input_frame": "robot_base",
  "unit": "mm",
  "camera_position_base": [400, 300, 800]
}
```

- 文件支持 PLY、普通 ASCII / binary PCD 和 XYZ，不支持压缩 PCD。
- `input_frame` 为 `robot_base` 或 `camera`。相机点云必须提供基座←相机的 `camera_to_base` 4×4 刚性变换，变换平移始终使用 mm。
- 基座点云的 `camera_position_base` 可省略。省略时提取几何，但姿态与轨迹标记为不可执行；相机点云从变换中取得相机位置。
- `model_type` 默认 `auto`。旧的 `three_plane/h_section/square_tube` 和正数宽度参数暂时兼容，会返回弃用提示，不参与几何计算。
- `max_planes=20` 限制自动拟合平面数量；`ransac_iterations=100`；`plane_distance_threshold_mm=2`；`voxel_size_mm=1`（0 关闭降采样）。
- `min_seam_length_mm=3`（不得更小）。`max_gap_mm` 默认按实测点间距与拟合阈值自动计算，控制交线支撑断裂的拆分，不跨越大缺口。
- `sample_step_mm=2`、`posture_width_mm=5`、`approach_distance_mm=10`、`retract_distance_mm=10` 控制姿态邻域及轨迹。
- `tcp_rotation_offset_deg=[0,0,0]` 为理想枪坐标系到 TCP 的固定 XYZ 欧拉角；TCP 原点为标定焊枪尖端。

## 方法与输出

1. 清理无效点、统一 mm、转换坐标并降采样；原始有效点用于交线支撑和姿态计算。
2. 使用欧氏近邻局部采样 RANSAC，逐个精拟合平面，按残余有效支撑停止；合并重复平面并重新分配边界点。
3. 枚举夹角至少 5° 的平面对，求数学交线；仅接受两侧测量点在交线邻域内的重叠区间，按支撑缺口拆分有限线段。
4. 焊缝按几何位置稳定编号 `L1...`；有相机位置及完整边缘支撑时，以实测二面角边缘计算右手枪坐标系，生成接近、沿缝、退刀位姿。支撑不足保留焊缝并附原因。

返回 `trajectory_path`、`csv_path`、`image_path`、`seams`、`seam_count`、`executable_seam_count`、`intersection_quality`、`warnings` 和 `inline_artifact_arguments`。将后者传给 `inline_artifact` 可展示 PNG。合法点云未找到焊缝时返回空列表和诊断原因。

JSON 保持 `schema_version=1`、基座 mm / degree、`Rz(rz) Ry(ry) Rx(rx)`、`APPROACH/WELDING/RETRACT` 格式，可交给 `robot_move_seam` 的原有读取器。`executable` 仅表示几何姿态和轨迹格式可用，不表示已完成逆解、碰撞或真实机器人可达性校验；工具本身不会连接机器人。

```json
{"trajectory_path":"/path/trajectory.json","seam_id":"L1","tool_id":0,"dry_run":true}
```

## 验证

```bash
PYTHONPATH=agent_flow /Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python -m pytest tests/test_point_cloud_welding.py tests/test_robot_move_seam.py -v
```

合成几何测试覆盖任意朝向、锐钝角、多个平面、噪声和离群点、缺口、平行面、无实际接触的假交线、坐标变换、轨迹格式与工具事件。误差检查仅针对合成夹具，不作为真实扫描精度承诺。

## 机械臂内联仿真

提取后调用 `robot_simulate_trajectory`，输入 `trajectory_path` 文件路径即可，无需把文件内容传回模型，也无需再调用一次 `inline_artifact`。仿真工具按 FR5 URDF 离线计算关节运动，自动发布 `artifacts.robot_simulation`，默认循环播放所有焊缝，卡片内可以选择 `seam_id`。

```json
{"trajectory_path":"/path/trajectory.json","speed_mm_s":20}
```

可选 `tcp_offset_pose=[x,y,z,rx,ry,rz]` 是法兰到焊枪 TCP 的安装变换，mm/degree、Rz Ry Rx；省略时使用临时 200 mm 工具 Z 偏移，不能代表已标定焊枪。可选 `seam_id` 限制本次产物仅包含某条焊缝。

输入限制为 64 MiB、总计 50,000 个轨迹点；位姿必须为基座坐标、毫米/度。逆解同时检查位置 ≤1 mm、姿态 ≤1° 和 URDF 关节限位。不可达点保留为红色诊断点，不跨越失败点播放；缺少姿态时仅显示几何。关节变化超过 90° 的跳变拆为新的连续段。仿真不调用真实机器人 SDK；由容器内 MoveIt 检查自碰撞及可选工件碰撞，并进行关节速度／加速度约束的时间参数化。

快照保存于 `agent_flow/store/robot_simulations`，可用 `ROBOT_SIMULATION_ROOT` 覆盖（多 worker 须共享该目录）。快照包括输入副本与 SHA-256、分段关节序列和诊断；改动或删除原始轨迹不改变已有产物。事件中仅传仿真 ID、摘要和 URL；`GET /api/im/robot-simulations/{simulation_id}` 要求登录且身份匹配原始运行的 `memory_scope.user_id`。API 不接受本地文件路径。

`inline_artifact` 也支持重新展示已有产物：

```json
{"artifact_type":"robot_simulation","robot_simulation":{"simulation_id":"工具返回的仿真ID","title":"FR5 轨迹预览"}}
```

针对性检查：`PYTHONPATH=agent_flow /Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python -m pytest tests/test_robot_simulation.py -v`。
