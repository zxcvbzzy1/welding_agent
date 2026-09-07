import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.interpolate import splprep, splev

# --- RRT* 节点与核心类 (保持不变) ---
class Node:
    def __init__(self, x, y, z):
        self.p = np.array([x, y, z], dtype=float)
        self.parent = None
        self.cost = 0.0

class RRTStar3D:
    def __init__(self, start, goal, obstacles, bounds, step_size=0.8, max_iter=1500, search_radius=1.5):
        self.start = Node(*start)
        self.goal = Node(*goal)
        self.obstacles = obstacles
        self.bounds = bounds
        self.step_size = step_size
        self.max_iter = max_iter
        self.search_radius = search_radius
        self.node_list = [self.start]

    def get_random_node(self):
        if np.random.rand() > 0.1:
            x = np.random.uniform(self.bounds[0], self.bounds[1])
            y = np.random.uniform(self.bounds[2], self.bounds[3])
            z = np.random.uniform(self.bounds[4], self.bounds[5])
            return Node(x, y, z)
        return Node(*self.goal.p)

    def get_nearest_node_index(self, rnd_node):
        dlist = [np.linalg.norm(node.p - rnd_node.p) for node in self.node_list]
        return dlist.index(min(dlist))

    def steer(self, from_node, to_node):
        new_node = Node(*from_node.p)
        d = np.linalg.norm(to_node.p - from_node.p)
        if d <= self.step_size:
            new_node.p = to_node.p
        else:
            new_node.p = from_node.p + (to_node.p - from_node.p) / d * self.step_size
        new_node.cost = from_node.cost + np.linalg.norm(new_node.p - from_node.p)
        new_node.parent = from_node
        return new_node

    def check_collision(self, node1, node2):
        steps = 10
        for i in range(steps + 1):
            p = node1.p + (node2.p - node1.p) * (i / steps)
            for (ox, oy, oz, r) in self.obstacles:
                if np.linalg.norm(p - np.array([ox, oy, oz])) <= r:
                    return True
        return False

    def get_near_nodes(self, new_node):
        dlist = [np.linalg.norm(node.p - new_node.p) for node in self.node_list]
        return [i for i in range(len(dlist)) if dlist[i] <= self.search_radius]

    def plan(self):
        for i in range(self.max_iter):
            rnd_node = self.get_random_node()
            nearest_ind = self.get_nearest_node_index(rnd_node)
            nearest_node = self.node_list[nearest_ind]
            new_node = self.steer(nearest_node, rnd_node)

            if not self.check_collision(nearest_node, new_node):
                near_inds = self.get_near_nodes(new_node)
                
                min_cost = new_node.cost
                best_parent = nearest_node
                for idx in near_inds:
                    near_node = self.node_list[idx]
                    if not self.check_collision(near_node, new_node):
                        cost = near_node.cost + np.linalg.norm(new_node.p - near_node.p)
                        if cost < min_cost:
                            min_cost = cost
                            best_parent = near_node
                
                new_node.cost = min_cost
                new_node.parent = best_parent
                self.node_list.append(new_node)

                for idx in near_inds:
                    near_node = self.node_list[idx]
                    if not self.check_collision(new_node, near_node):
                        cost = new_node.cost + np.linalg.norm(near_node.p - new_node.p)
                        if cost < near_node.cost:
                            near_node.parent = new_node
                            near_node.cost = cost

        dlist = [np.linalg.norm(node.p - self.goal.p) for node in self.node_list]
        if min(dlist) <= self.step_size:
            goal_idx = dlist.index(min(dlist))
            path = []
            node = self.node_list[goal_idx]
            path.append(self.goal.p)
            while node is not None:
                path.append(node.p)
                node = node.parent
            return np.array(path[::-1])
        return None

# --- 新增的 B 样条轨迹平滑函数 ---
def smooth_path_bspline(path, num_points=200):
    """
    使用 B 样条对 3D 路径进行平滑处理
    """
    # 1. 过滤掉距离过近的冗余点，防止 splprep 抛出奇点错误
    diffs = np.linalg.norm(np.diff(path, axis=0), axis=1)
    mask = np.concatenate(([True], diffs > 1e-5))
    path_filtered = path[mask]
    
    # 3阶样条曲线至少需要 4 个控制点
    if len(path_filtered) < 4:
        return path_filtered

    # 2. 计算 B 样条参数
    # s (平滑因子): s=0 表示严格插值经过每个点，s>0 允许在拐角处进行近似平滑以减少曲率。
    # 对于焊接来说，可以适当放大 s 以得到更圆滑的过渡，但需要注意平滑后不要撞到障碍物。
    tck, u = splprep([path_filtered[:,0], path_filtered[:,1], path_filtered[:,2]], s=2.0, k=3)
    
    # 3. 在曲线上重新采样更密集的点 (num_points)
    u_new = np.linspace(0, 1, num_points)
    x_new, y_new, z_new = splev(u_new, tck)
    
    return np.vstack((x_new, y_new, z_new)).T

if __name__ == '__main__':
    np.random.seed(42)
    start_pt = [0, 0, 0]
    goal_pt = [10, 10, 10]
    obstacles_list = [
        (4, 4, 4, 2.0),
        (7, 6, 7, 1.8),
        (2, 8, 3, 1.5),
        (8, 2, 8, 1.5)
    ]
    space_bounds = [0, 10, 0, 10, 0, 10]

    print("开始规划原始路径...")
    rrt = RRTStar3D(start_pt, goal_pt, obstacles_list, space_bounds, step_size=0.8, max_iter=1500)
    path = rrt.plan()

    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection='3d')

    # 绘制障碍物
    u, v = np.mgrid[0:2*np.pi:20j, 0:np.pi:10j]
    for (ox, oy, oz, r) in obstacles_list:
        x = ox + r*np.cos(u)*np.sin(v)
        y = oy + r*np.sin(u)*np.sin(v)
        z = oz + r*np.cos(v)
        ax.plot_surface(x, y, z, color='red', alpha=0.2)

    if path is not None:
        print("执行轨迹平滑...")
        smoothed_path = smooth_path_bspline(path)
        
        # 绘制对比图
        ax.plot(path[:,0], path[:,1], path[:,2], color='gray', linestyle='--', linewidth=2, label='Original RRT* Path')
        ax.plot(smoothed_path[:,0], smoothed_path[:,1], smoothed_path[:,2], color='blue', linewidth=3, label='Smoothed Path (B-Spline)')
    else:
        print("未找到路径。")

    ax.scatter(*start_pt, color='green', s=100, label='Start', zorder=5)
    ax.scatter(*goal_pt, color='magenta', s=100, label='Goal', zorder=5)

    ax.set_xlim(space_bounds[0], space_bounds[1])
    ax.set_ylim(space_bounds[2], space_bounds[3])
    ax.set_zlim(space_bounds[4], space_bounds[5])
    ax.set_xlabel('X')
    ax.set_ylabel('Y')
    ax.set_zlabel('Z')
    ax.legend()
    plt.title('3D Path Planning for Welding with B-Spline Smoothing')
    plt.show()