import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from matplotlib.animation import FuncAnimation
from scipy.interpolate import splprep, splev

def create_smooth_transition(p_start, p_mid, p_end, num_points=50):
    """生成三点之间的平滑过渡曲线"""
    points = np.vstack((p_start, p_mid, p_end))
    tck, u = splprep([points[:,0], points[:,1], points[:,2]], s=0, k=2)
    u_new = np.linspace(0, 1, num_points)
    x, y, z = splev(u_new, tck)
    return np.vstack((x, y, z)).T

if __name__ == '__main__':
    # ================= 1. 轨迹生成 (与之前相同) =================
    home_pos = np.array([6, 6, 8])
    approach_1 = np.array([8, 0, 2])
    weld1_start = np.array([8, 0, 0])
    weld1_end = np.array([0.5, 0, 0]) 
    corner_via = np.array([0.2, 0.2, 0.2])
    weld2_start = np.array([0, 0.5, 0])
    weld2_end = np.array([0, 8, 0])
    air_via = np.array([3, 3, 6])
    approach_3 = np.array([0, 0, 8])
    weld3_start = np.array([0, 0, 7.5])
    weld3_end = np.array([0, 0, 0.5]) 
    retract_3 = np.array([1, 1, 1.5])

    # 生成各段轨迹并拼接
    path_approach1 = create_smooth_transition(home_pos, (home_pos+approach_1)/2, approach_1, num_points=30)
    path_plunge1 = np.linspace(approach_1, weld1_start, 10)
    path_weld1 = np.linspace(weld1_start, weld1_end, 50)
    path_corner = create_smooth_transition(weld1_end, corner_via, weld2_start, num_points=40)
    path_weld2 = np.linspace(weld2_start, weld2_end, 50)
    path_air_transfer = create_smooth_transition(weld2_end, air_via, approach_3, num_points=60)
    path_plunge3 = np.linspace(approach_3, weld3_start, 10)
    path_weld3 = np.linspace(weld3_start, weld3_end, 50)
    path_return = create_smooth_transition(weld3_end, retract_3, home_pos, num_points=40)

    # 核心：将所有离散的轨迹段按顺序拼成一个完整的连续时间序列数组
    full_path = np.vstack((
        path_approach1, path_plunge1, path_weld1, path_corner, 
        path_weld2, path_air_transfer, path_plunge3, path_weld3, path_return
    ))

    # ================= 2. 动画场景初始化 =================
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection='3d')

    # 绘制三面角的墙壁
    plane_size = 10
    xx, yy = np.meshgrid(np.linspace(0, plane_size, 2), np.linspace(0, plane_size, 2))
    zero_plane = np.zeros_like(xx)
    ax.plot_surface(xx, yy, zero_plane, color='gray', alpha=0.2)
    ax.plot_surface(xx, zero_plane, yy, color='lightblue', alpha=0.2)
    ax.plot_surface(zero_plane, xx, yy, color='lightgreen', alpha=0.2)

    # 绘制一条淡淡的灰色底线作为参考
    ax.plot(full_path[:,0], full_path[:,1], full_path[:,2], color='gray', linestyle=':', alpha=0.5)
    
    # 初始化动画元素：一条不断生长的轨迹线，和一个代表焊枪 TCP 的红点
    trajectory_line, = ax.plot([], [], [], color='red', linewidth=3, label='TCP Trajectory')
    tcp_point, = ax.plot([], [], [], marker='o', color='darkred', markersize=8, label='Welding Gun (TCP)')

    ax.scatter(*home_pos, color='black', s=100, label='Home Position', marker='*')

    # 视角与坐标轴设置
    ax.view_init(elev=25, azim=45)
    ax.set_xlim([0, 10])
    ax.set_ylim([0, 10])
    ax.set_zlim([0, 10])
    ax.set_xlabel('X Axis')
    ax.set_ylabel('Y Axis')
    ax.set_zlabel('Z Axis')
    ax.set_title('Dynamic 3D Welding Trajectory Simulation')
    ax.legend()

    # ================= 3. 动画更新函数 =================
    def update(frame):
        # 截取从 0 到当前帧的数据点
        current_path = full_path[:frame]
        
        # 更新尾迹线
        if len(current_path) > 0:
            trajectory_line.set_data(current_path[:, 0], current_path[:, 1])
            trajectory_line.set_3d_properties(current_path[:, 2])
            
            # 更新 TCP 当前位置点
            current_pos = current_path[-1]
            tcp_point.set_data([current_pos[0]], [current_pos[1]])
            tcp_point.set_3d_properties([current_pos[2]])
            
        return trajectory_line, tcp_point

    # 创建动画：frames 为总数据点数，interval 控制播放速度 (毫秒)
    ani = FuncAnimation(fig, update, frames=len(full_path), interval=30, blit=False, repeat=True)

    plt.show()