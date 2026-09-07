import numpy as np
import trimesh
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from scipy.spatial import cKDTree
from scipy.interpolate import splprep, splev
from scipy.spatial.transform import Rotation as R_scipy
import csv
import json
from pathlib import Path
from threading import Lock
from uuid import uuid4

TEMP_DIR = Path(__file__).resolve().parents[4] / "temp"
_RENDER_LOCK = Lock()

# ================= 1. 基础数学与插补工具 =================

def smooth_path_bspline(path, num_points=50):
    """B样条平滑，用于柔化 RRT* 生成的折线"""
    if path is None or len(path) < 3: return path
    diffs = np.linalg.norm(np.diff(path, axis=0), axis=1)
    mask = np.concatenate(([True], diffs > 1e-5))
    path_filtered = path[mask]
    if len(path_filtered) < 4: return path_filtered
    
    tck, u = splprep([path_filtered[:,0], path_filtered[:,1], path_filtered[:,2]], s=2.0, k=3)
    u_new = np.linspace(0, 1, num_points)
    x, y, z = splev(u_new, tck)
    return np.vstack((x, y, z)).T

def linear_interpolation(p1, p2, step_size=0.5):
    """笛卡尔空间直线插补"""
    dist = np.linalg.norm(p2 - p1)
    num_steps = max(int(dist / step_size), 2)
    return np.linspace(p1, p2, num_steps)

# ================= 2. 空间避障规划器 (RRT*) =================

class RRTStar3D:
    """基于 KD-Tree 点云避障的 RRT* 规划器"""
    class Node:
        def __init__(self, p):
            self.p = np.array(p, dtype=float)
            self.parent = None
            self.cost = 0.0

    def __init__(self, start, goal, obstacle_kdtree, bounds, safe_dist=1.0, step_size=1.0, max_iter=800, rng=None):
        self.start = self.Node(start)
        self.goal = self.Node(goal)
        self.tree = obstacle_kdtree # 障碍物表面点云
        self.bounds = bounds
        self.safe_dist = safe_dist
        self.step_size = step_size
        self.max_iter = max_iter
        self.rng = rng if rng is not None else np.random.default_rng()
        self.node_list = [self.start]

    def check_collision(self, p1, p2):
        """检查线段上的点是否侵入工件安全距离"""
        steps = int(np.linalg.norm(p2 - p1) / max(self.safe_dist / 2, 1e-6)) + 2
        samples = np.linspace(p1, p2, steps)
        dists, _ = self.tree.query(samples)
        if np.any(dists < self.safe_dist):
            return True
        return False

    def plan(self):
        if not self.check_collision(self.start.p, self.goal.p):
            return np.array([self.start.p, self.goal.p])
        for _ in range(self.max_iter):
            rnd_p = self.goal.p if self.rng.random() < 0.1 else np.array([
                self.rng.uniform(self.bounds[0], self.bounds[1]),
                self.rng.uniform(self.bounds[2], self.bounds[3]),
                self.rng.uniform(self.bounds[4], self.bounds[5])
            ])
            
            dlist = [np.linalg.norm(node.p - rnd_p) for node in self.node_list]
            nearest_node = self.node_list[np.argmin(dlist)]
            
            d = np.linalg.norm(rnd_p - nearest_node.p)
            new_p = rnd_p if d <= self.step_size else nearest_node.p + (rnd_p - nearest_node.p) / d * self.step_size
            
            if not self.check_collision(nearest_node.p, new_p):
                new_node = self.Node(new_p)
                new_node.parent = nearest_node
                self.node_list.append(new_node)
                
                # 到达目标附近，寻路成功
                if np.linalg.norm(new_node.p - self.goal.p) <= self.step_size:
                    if not self.check_collision(new_node.p, self.goal.p):
                        final_node = self.Node(self.goal.p)
                        final_node.parent = new_node
                        self.node_list.append(final_node)
                        
                        path = []
                        node = final_node
                        while node is not None:
                            path.append(node.p)
                            node = node.parent
                        return np.array(path[::-1])
        return None 

# ================= 3. CAD 焊缝特征自动提取 =================

def generate_welding_trajectory_from_mesh(mesh, min_length_threshold=5.0):
    """基于向量点积提取内角焊缝，剔除短边，计算最佳焊枪姿态"""
    trajectories = []
    
    for i, (face_a_idx, face_b_idx) in enumerate(mesh.face_adjacency):
        n_a, n_b = mesh.face_normals[face_a_idx], mesh.face_normals[face_b_idx]
        
        # 判断两个面是否垂直
        if abs(np.dot(n_a, n_b)) < 0.1:
            center_a = mesh.triangles_center[face_a_idx]
            center_b = mesh.triangles_center[face_b_idx]
            
            # 判断是否为内凹死角 (A法向指向B)
            if np.dot(n_a, center_b - center_a) > 0.01:
                edge = mesh.face_adjacency_edges[i]
                p1, p2 = mesh.vertices[edge[0]], mesh.vertices[edge[1]]
                
                direction = p2 - p1
                length = np.linalg.norm(direction)
                
                # 过滤极短线段或噪声
                if length < min_length_threshold: 
                    continue
                    
                tangent = direction / length
                
                # 焊枪理想姿态：两面法向量之和的反方向
                torch_dir = -(n_a + n_b)
                torch_dir = torch_dir / np.linalg.norm(torch_dir) if np.linalg.norm(torch_dir) > 1e-4 else n_a

                trajectories.append({
                    'start_point': p1,
                    'end_point': p2,
                    'torch_direction': torch_dir,
                    'travel_direction': tangent,
                    'length': length
                })
    return trajectories

# ================= 4. 状态机：宏微观联合规划 =================

def plan_full_mission(mesh, weld_seams, home_pos, approach_dist=30.0,
                      safe_clearance=5.0, max_iter=3000, seed=0):
    """沿用表面点云避障；失败时停止，平滑后重新检查转移路径。"""
    rng = np.random.default_rng(seed)
    surface_points, _ = trimesh.sample.sample_surface(mesh, 10000, seed=rng)
    obstacle_tree = cKDTree(surface_points)
    targets = [np.asarray(home_pos, dtype=float), *mesh.bounds]
    for seam in weld_seams:
        targets.extend([
            seam['start_point'] - seam['torch_direction'] * approach_dist,
            seam['end_point'] - seam['torch_direction'] * approach_dist,
        ])
    targets = np.asarray(targets)
    extent = float(np.max(mesh.extents))
    margin = max(approach_dist, safe_clearance * 2, extent * 0.1)
    lower, upper = targets.min(axis=0) - margin, targets.max(axis=0) + margin
    space_bounds = np.column_stack((lower, upper)).ravel()

    def transfer(start, end, label):
        planner = RRTStar3D(
            start, end, obstacle_tree, space_bounds,
            safe_dist=safe_clearance, step_size=max(extent * 0.1, safe_clearance),
            max_iter=max_iter, rng=rng,
        )
        path = planner.plan()
        if path is None:
            raise ValueError(f"{label} 避障规划失败，请调整 home_pos、approach_dist、safe_clearance 或 max_iter")
        smoothed = smooth_path_bspline(path)
        # B 样条可能偏离端点或切入障碍物；不合格时保留原折线路径。
        smoothed[0], smoothed[-1] = path[0], path[-1]
        if any(planner.check_collision(a, b) for a, b in zip(smoothed[:-1], smoothed[1:])):
            return path
        return smoothed

    mission = []
    current_pos = np.asarray(home_pos, dtype=float)
    for index, seam in enumerate(weld_seams, 1):
        approach = seam['start_point'] - seam['torch_direction'] * approach_dist
        retract = seam['end_point'] - seam['torch_direction'] * approach_dist
        mission.append({'stage': 'AIR_MOVE', 'path': transfer(current_pos, approach, f"焊缝 {index}")})
        for stage, start, end in [
            ('APPROACH', approach, seam['start_point']),
            ('WELDING', seam['start_point'], seam['end_point']),
            ('RETRACT', seam['end_point'], retract),
        ]:
            mission.append({
                'stage': stage, 'path': linear_interpolation(start, end),
                'torch_dir': seam['torch_direction'], 'travel_dir': seam['travel_direction'],
            })
        current_pos = retract
    mission.append({'stage': 'RETURN_HOME', 'path': transfer(current_pos, home_pos, "回源")})
    return mission


# ================= 5. 数据导出：坐标与欧拉角 =================

def export_trajectory_to_csv(mission_plan, home_pos, filename="welding_robot_path.csv"):
    """导出相对起点坐标与 RxRyRz 欧拉角 (最短纯旋转优化版)"""
    default_torch_z = np.array([0, 0, -1]) 
    total_points = 0
    
    with open(filename, mode='w', newline='', encoding='utf-8') as file:
        writer = csv.writer(file)
        writer.writerow(['Index', 'Stage', 'X(mm)', 'Y(mm)', 'Z(mm)', 'Rx(deg)', 'Ry(deg)', 'Rz(deg)'])
        
        for segment in mission_plan:
            stage = segment['stage']
            path = segment['path']
            
            # 只获取焊枪的 Z 轴指向（即插入死角的方向），丢弃无关的 X 轴切向约束
            z_vec = segment.get('torch_dir', default_torch_z)
            z_vec = z_vec / np.linalg.norm(z_vec)
            
            # === 【核心数学修改：最短轴角旋转】 ===
            # 计算从默认垂直向下 [0,0,-1] 纯旋转到目标方向的向量
            axis = np.cross(default_torch_z, z_vec)
            axis_len = np.linalg.norm(axis)
            
            if axis_len < 1e-4:
                # 如果方向平行（垂直向下），不需要旋转
                rot_matrix = np.eye(3)
                if np.dot(default_torch_z, z_vec) < 0:
                    rot_matrix[0, 0] = -1
                    rot_matrix[2, 2] = -1
            else:
                # 计算出旋转轴与偏转角度 (严格的纯旋转)
                axis = axis / axis_len
                angle = np.arccos(np.clip(np.dot(default_torch_z, z_vec), -1.0, 1.0))
                # 通过 scipy 将“轴角”转换为旋转矩阵
                rot_vector = axis * angle
                rot_matrix = R_scipy.from_rotvec(rot_vector).as_matrix()
            
            # 转换为欧拉角 ('xyz' 表示固定轴外旋，匹配主流机器人的 RxRyRz)
            euler_angles = R_scipy.from_matrix(rot_matrix).as_euler('xyz', degrees=True)
            
            for p in path:
                # 减去 home_pos，以起点为零点
                rel_p = p - home_pos 
                writer.writerow([
                    total_points, stage,
                    round(rel_p[0], 3), round(rel_p[1], 3), round(rel_p[2], 3),
                    round(euler_angles[0], 3), round(euler_angles[1], 3), round(euler_angles[2], 3)
                ])
                total_points += 1
                
    return total_points


def render_mission(mesh, mission, home_pos, filename):
    """使用 Agg 离屏渲染，不打开 GUI；串行绘图以兼容并发工具调用。"""
    with _RENDER_LOCK:
        fig = Figure(figsize=(12, 10), layout="constrained")
        FigureCanvasAgg(fig)
        try:
            ax = fig.add_subplot(111, projection="3d")
            ax.add_collection3d(Poly3DCollection(
                mesh.triangles, alpha=0.35, facecolors="lightblue",
                edgecolors="gray", linewidths=0.2,
            ))
            styles = {
                "AIR_MOVE": ("b--", 1.5, "Air Move"),
                "RETURN_HOME": ("b--", 1.5, "Air Move"),
                "APPROACH": ("g-", 2, "Approach/Retract"),
                "RETRACT": ("g-", 2, "Approach/Retract"),
                "WELDING": ("r-", 4, "Welding Seam"),
            }
            seen = set()
            for segment in mission:
                points = segment["path"]
                style, width, label = styles[segment["stage"]]
                ax.plot(*points.T, style, linewidth=width, label=label if label not in seen else "")
                seen.add(label)
            ax.scatter(*home_pos, color="black", s=120, marker="*", label="Home (Zero Point)")
            # 同时包含工件、Home 和所有轨迹，避免自适应退刀点被裁掉。
            points = np.vstack([mesh.bounds, np.asarray(home_pos), *[s["path"] for s in mission]])
            center = (points.min(axis=0) + points.max(axis=0)) / 2
            radius = float(np.max(np.ptp(points, axis=0))) * 0.55
            for setter, value in zip((ax.set_xlim, ax.set_ylim, ax.set_zlim), center):
                setter(value - radius, value + radius)
            ax.set_box_aspect((1, 1, 1))
            ax.set(xlabel="X (mm)", ylabel="Y (mm)", zlabel="Z (mm)")
            ax.view_init(elev=35, azim=60)
            ax.set_title("CAD to Welding Path: Trajectory Preview")
            ax.legend(loc="upper left")
            fig.savefig(filename, dpi=140, bbox_inches="tight", pad_inches=0.25)
        finally:
            fig.clear()


def generate_cad_welding_path(
    model_path: str,
    *,
    min_length_threshold: float = 5.0,
    rotation_x_deg: float = 90.0,
    home_pos: list[float] | None = None,
    approach_dist: float | None = None,
    safe_clearance: float | None = None,
    max_iter: int = 3000,
    seed: int = 0,
) -> dict:
    """加载毫米制网格，输出轨迹 PNG、CSV 和 inline_artifact 调用参数。"""
    model = Path(model_path).expanduser().resolve()
    if not model.is_file():
        raise ValueError(f"模型文件不存在: {model}")
    if model.suffix.lower() not in {".stl", ".obj", ".ply", ".off", ".glb", ".gltf"}:
        raise ValueError("仅支持 STL/OBJ/PLY/OFF/GLB/GLTF 网格；STEP/IGES 请先转换为网格")
    if not np.isfinite(min_length_threshold) or min_length_threshold <= 0:
        raise ValueError("min_length_threshold 必须为正数")
    if not np.isfinite(rotation_x_deg):
        raise ValueError("rotation_x_deg 必须为有限数值")
    if isinstance(max_iter, bool) or not isinstance(max_iter, int) or not 1 <= max_iter <= 50000:
        raise ValueError("max_iter 必须为 1 到 50000 的整数")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed 必须为非负整数")
    mesh = trimesh.load(model, force="mesh")
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty or not np.isfinite(mesh.vertices).all():
        raise ValueError("模型必须包含有效的三角网格")
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.radians(rotation_x_deg), [1, 0, 0]))
    mesh.process()
    mesh.fix_normals()
    max_extent = float(np.max(np.ptp(mesh.bounds, axis=0)))
    if max_extent <= 0:
        raise ValueError("模型尺寸必须大于零")
    approach_dist = max_extent * 0.5 if approach_dist is None else approach_dist
    safe_clearance = max_extent * 0.05 if safe_clearance is None else safe_clearance
    for name, value in (("approach_dist", approach_dist), ("safe_clearance", safe_clearance)):
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须为正数")
    if home_pos is None:
        low, high = mesh.bounds
        home_pos = [high[0] + max_extent * 0.5, low[1] - max_extent * 0.5, high[2] + max_extent * 0.5]
    home = np.asarray(home_pos, dtype=float)
    if home.shape != (3,) or not np.isfinite(home).all():
        raise ValueError("home_pos 必须包含三个有限坐标值")
    seams = generate_welding_trajectory_from_mesh(mesh, min_length_threshold)
    if not seams:
        raise ValueError("未提取到符合条件的内角焊缝，请检查模型结构或降低 min_length_threshold")
    mission = plan_full_mission(
        mesh, seams, home, approach_dist, safe_clearance, max_iter=max_iter, seed=seed,
    )
    output_dir = TEMP_DIR / "welding" / uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=False)
    image_path = output_dir / "welding_trajectory.png"
    csv_path = output_dir / "welding_robot_path.csv"
    count = export_trajectory_to_csv(mission, home, csv_path)
    render_mission(mesh, mission, home, image_path)
    return {
        "model_path": str(model),
        "image_path": str(image_path),
        "csv_path": str(csv_path),
        "seam_count": len(seams),
        "point_count": count,
        "home_pos": home.tolist(),
        "units": "mm",
        "rotation_x_deg": rotation_x_deg,
        "approach_dist": approach_dist,
        "safe_clearance": safe_clearance,
        "seed": seed,
        "note": "几何轨迹预览：表面采样避障未验证焊枪体积、机器人运动学及碰撞；CSV 坐标相对旋转后的 Home。",
        "inline_artifact_arguments": {
            "artifact_type": "image",
            "image": {
                "title": "welding_trajectory.png",
                "file_path": str(image_path),
                "alt": f"CAD 焊接轨迹预览，共 {len(seams)} 条焊缝",
                "mime_type": "image/png",
                "metadata": {"csv_path": str(csv_path), "seam_count": len(seams)},
            },
        },
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="从 CAD 网格生成焊接轨迹 PNG 和 CSV")
    parser.add_argument("model_path", nargs="?", default=str(Path(__file__).with_name("small3.stl")))
    parser.add_argument("--rotation-x-deg", type=float, default=90.0)
    parser.add_argument("--min-length-threshold", type=float, default=5.0)
    args = parser.parse_args()
    print(json.dumps(generate_cad_welding_path(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
