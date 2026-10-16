# 点云焊缝提取与单缝机器人运动

复现论文：Yusen Geng et al., *A novel seam extraction and path planning method
for robotic welding of medium-thickness plate structural parts based on 3D vision*,
Robotics and Computer-Integrated Manufacturing 79 (2023), 102433.
DOI: https://doi.org/10.1016/j.rcim.2022.102433 。本地论文位于仓库 `docs/`。

支持单个、裁剪完成且水平放置的三平面对接、H 型钢对接、方管对接构件。
基座 +Z 向上；不从任意场景中识别构件类型，不计算手眼标定。

## 工具调用

先调用 `point_cloud_weld_seams`：

```json
{
  "point_cloud_path": "/absolute/path/workpiece.ply",
  "model_type": "three_plane",
  "input_frame": "robot_base",
  "camera_position_base": [300, 300, 500]
}
```

- `model_type`：`three_plane`、`h_section`、`square_tube`。
- H 型钢必填 `h_width_mm`；方管必填 `tube_width_mm`。必须使用实际尺寸，
  不默认采用论文中的 68 mm / 30 mm。与模型不相关的尺寸参数会被拒绝。
- `input_frame=robot_base`：必须提供采集时相机在基座坐标系中的位置
  `camera_position_base`，用于确定可见面法向及平面顺序。
- `input_frame=camera`：必须提供 `camera_to_base` 4×4 刚性变换，方向为
  **基座 ← 相机**。相机位置取矩阵的平移，不再另填 `camera_position_base`。
- `unit` 默认为 `mm`，也可为 `m`。先换算点云位置单位，再应用变换；
  **变换矩阵的平移、相机基座位置及所有 `_mm` 参数始终使用毫米**。
- PLY 支持 ASCII、binary，忽略 RGB 和法向等附加字段；PCD 支持 ASCII 和
  普通 binary、任意 XYZ 字段顺序及额外标量/向量字段，不支持 binary_compressed。
  XYZ 取前三列。无效点和重复点会被清理，不会根据颜色识别焊缝。

返回 `trajectory_path`、`csv_path`、`image_path`、`seams`、
`executable_seam_count` 和 `inline_artifact_arguments`。
将最后一项原样传给 `inline_artifact` 可展示预览图片。
每次运行的 JSON / CSV / PNG 保存到 `agent_flow/temp/point_cloud_welding/<运行编号>/`。
JSON 是机器人工具的输入，CSV 用于检查和导出，PNG 标注焊缝编号、方向和接近点。

再调用 `robot_move_seam` 做离线校验：

```json
{
  "trajectory_path": "/absolute/path/trajectory.json",
  "seam_id": "L1",
  "tool_id": 3,
  "vel": 5,
  "dry_run": true
}
```

`dry_run` 默认 `true`：不加载 FAIRINO SDK、不创建连接、不请求运动确认，
返回完整阶段序列和 `required_start_pose`。实际执行用同一参数并设置
`dry_run=false`，复用现有人工确认 / `auto_confirm` 配置。

实际执行前，控制器必须已应用匹配的已标定焊枪 TCP（`tool_id=1..14`）和
`user=0`，并已在接近点就位。程序检查当前 TCP、工件坐标系、位置误差
不超过 2 mm、姿态旋转误差不超过 2°；角度误差按旋转矩阵计算，兼容 ±180° 换支。
当前接近点通过检查后，从首个焊接点开始调用 `MoveL`，最后到达退刀点。
一次执行仅连接一次，使用 `blendR=-1`、`acc=0`、`ovl=100`；`vel` 是百分比。
任一点失败后停止发送后续点，不强行退刀，返回失败阶段、轨迹点索引、已完成命令数和 SDK 错误。
点索引从 0 开始，0 是已就位的接近点；`completed_count` 不包含该点。
单点、增量和单缝运动共用进程内串行锁。
连接与运动统一复用 `openvla_motion.py` 中的 `FairinoLinearMotion.connect()`，
串行锁也由该类持有，不另建连接模块。
这些调用在线程中执行，等待串行锁时不阻塞事件总线。运行被取消后，不再发送后续点；
已经发出的阻塞式 `MoveL` 完成后释放连接，不声称可以撤回控制器正在执行的运动。

该工具执行几何轨迹，不控制起弧，不规划焊缝间空移，不执行全机器人碰撞仿真。
实机可达性和控制器错误仍由 SDK 报告。`executable=true` 表示点云姿态和轨迹完整，
不是实机已经通过可达性验证。无需 SDK 的离线功能不会连接相机、机器人、LLM 或数据库。

## 姿态和标定约定

TCP 位姿为 `[x,y,z,rx,ry,rz]`，毫米/度，
`R = Rz(rz) @ Ry(ry) @ Rx(rx)`，等价于 SciPy 固定轴 `xyz` 欧拉角。
理想枪坐标 `Xg` 沿焊接方向，`Zg` 指向焊缝，
`Yg = Zg × Xg`。采样位姿保存为控制器所需的 TCP 位姿。

`tcp_rotation_offset_deg=[rx,ry,rz]` 默认为零，定义 `R_理想枪_TCP`：

```text
R_base_TCP = R_base_理想枪 @ R_理想枪_TCP
```

控制器的工具标定负责法兰到焊枪尖端的平移；本参数只修正两套 TCP 轴的固定旋转，
不重复补偿枪长。必须选择与这项修正对应的 `tool_id`。
接近点和退刀点沿**理想枪轴的反方向**偏移，不随 TCP 轴向修正改变物理位置。

## 论文步骤与实现差异

1. 第 3.2.1 节：±10 mm 包围盒、50 个近邻、随机选择第二/第三点；
   每轮默认 10 次候选，粗分割距离 2 mm。
2. 第 3.2.2 节：以质心及其近邻进行第二次平面拟合，再用稳健总最小二乘
   稳定带噪结果，并从完整点云恢复粗分割阶段移除的边界条带。
   “二次拟合”并不是二次曲面。
3. 第 3.3 节：最低水平面为 P1，按相机可见方向确定法向，
   用标量三重积确定 P2/P3，剩余水平面按高度排序。
4. 第 4 节：两侧交线邻域做 SVD；投影回两拟合平面以消除单侧条带的质心偏差。
   用较宽的 8 mm 条带估计有限纵向支撑范围，降低稀疏末端的截短偏差，
   两侧范围取交集，使用 0.5% / 99.5% 分位数抑制孤立端点。
5. 三平面对接合并公共端点；H 型钢按式（15）—（17）生成 M→N、E→C、B→F、F→G；
   方管生成 A→B、B→G、B→F、A→H，排除 P3/P4 的非焊接外边。
6. 第 5.2 节：局部横截面每侧取远端点，SVD 拟合并约束边线平行于焊缝，
   同纵向位置插值，计算二面角平分方向。必须有覆盖整条焊缝的两侧支撑；
   共面接缝使用可见平面法向。姿态不可用时保留几何结果，但 `executable=false`，
   不输出运动点，机器人工具拒绝执行。

记录的论文修正：式（7）y 分量使用 `y-y_Cc`；式（9）使用朝向相机的符号判定；
式（11）用三重积避免除零；式（25）修正为右手叉乘顺序；方管末段按式（19）
与图 23 使用 A→H，而非表 6 的 B→H。共面姿态处理、稳健精拟合和有限条带支撑
属于明确记录的工程补充，不声称恢复了作者未提供的源码。

## 离线验证

使用项目环境运行：

```bash
/Users/zxcvbzzy1/miniconda3/envs/test_for_master/bin/python -m pytest \
  test_point_cloud_welding.py test_robot_move_seam.py \
  test_cad_welding_path.py agent_flow/camera_tool_test.py -v
```

`synthetic.fixture_cloud` 提供三类构件的独立几何真值，可生成噪声、离群点、
非均匀密度、旋转平移后的样例。验证目标是这些合成样例的端点误差 ≤1.5 mm，
不是论文原始数据的复测，也不是实际相机和机器人标定后的精度承诺。

测试覆盖坐标和单位、文件格式、模型尺寸、姿态退化、轨迹损坏、预览零连接、
人工拒绝零运动、一次确认、一次连接、SDK 中途失败、连接释放及与现有单点运动串行化。
