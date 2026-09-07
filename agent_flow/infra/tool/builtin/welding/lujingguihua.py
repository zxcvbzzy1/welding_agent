import numpy as np
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.interpolate import splprep, splev

def create_smooth_transition(p_start, p_mid, p_end, num_points=50):
    """生成三点之间的平滑过渡曲线 (用于空走或拐角过渡)"""
    points = np.vstack((p_start, p_mid, p_end))
    # s=0 表示必须经过控制点，k=2 表示二次样条
    tck, u = splprep([points[:,0], points[:,1], points[:,2]], s=0, k=2)
    u_new = np.linspace(0, 1, num_points)
    x, y, z = splev(u_new, tck)
    return np.vstack((x, y, z)).T

if __name__ == '__main__':
    # 1. 设定关键点 (Waypoints - 以工件坐标系为基准)
    home_pos = np.array([6, 6, 8])

    # 焊缝 1 (X轴)
    approach_1 = np.array([8, 0, 2])
    weld1_start = np.array([8, 0, 0])
    weld1_end = np.array([0.5, 0, 0]) # 靠近原点提前停止，留出甩枪空间

    # 拐角过渡引导点 (引导样条曲线避开干涉)
    corner_via = np.array([0.2, 0.2, 0.2])

    # 焊缝 2 (Y轴)
    weld2_start = np.array([0, 0.5, 0])
    weld2_end = np.array([0, 8, 0])
    retract_2 = np.array([0, 8, 2])

    # 空中转移 (Air Move) 引导点
    air_via = np.array([3, 3, 6])

    # 焊缝 3 (Z轴)
    approach_3 = np.array([0, 0, 8])
    weld3_start = np.array([0, 0, 7.5])
    weld3_end = np.array([0, 0, 0.5]) # 靠近原点停止
    retract_3 = np.array([1, 1, 1.5])

    # 2. 生成各段轨迹
    # 段1: Home -> 接近点1 (平滑)
    path_approach1 = create_smooth_transition(home_pos, (home_pos+approach_1)/2, approach_1)
    # 段2: 下枪
    path_plunge1 = np.vstack((approach_1, weld1_start))
    # 段3: 焊接 X 轴
    path_weld1 = np.vstack((weld1_start, weld1_end))
    # 段4: X 到 Y 拐角平滑过渡 (连续焊接)
    path_corner = create_smooth_transition(weld1_end, corner_via, weld2_start)
    # 段5: 焊接 Y 轴
    path_weld2 = np.vstack((weld2_start, weld2_end))
    # 段6: 提枪并空中转移到 Z 轴
    path_air_transfer = create_smooth_transition(weld2_end, air_via, approach_3, num_points=100)
    # 段7: 下枪准备焊 Z
    path_plunge3 = np.vstack((approach_3, weld3_start))
    # 段8: 焊接 Z 轴
    path_weld3 = np.vstack((weld3_start, weld3_end))
    # 段9: 提枪回 Home
    path_return = create_smooth_transition(weld3_end, retract_3, home_pos)

    # 3. 绘图部分 (完整复现)
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(111, projection='3d')

    # 绘制三面角 (三个垂直的平面)
    plane_size = 10
    xx, yy = np.meshgrid(np.linspace(0, plane_size, 2), np.linspace(0, plane_size, 2))
    zero_plane = np.zeros_like(xx)

    # XY 平面 (底面)
    ax.plot_surface(xx, yy, zero_plane, color='gray', alpha=0.3)
    # XZ 平面 (侧面1)
    ax.plot_surface(xx, zero_plane, yy, color='lightblue', alpha=0.3)
    # YZ 平面 (侧面2)
    ax.plot_surface(zero_plane, xx, yy, color='lightgreen', alpha=0.3)

    # 绘制焊缝基准线 (黑虚线)
    ax.plot([0, 10], [0, 0], [0, 0], 'k--', linewidth=1)
    ax.plot([0, 0], [0, 10], [0, 0], 'k--', linewidth=1)
    ax.plot([0, 0], [0, 0], [0, 10], 'k--', linewidth=1)

    # 绘制轨迹
    # 空走/过渡轨迹 (蓝色虚线)
    ax.plot(path_approach1[:,0], path_approach1[:,1], path_approach1[:,2], 'b--', linewidth=1.5, label='Air Move (Approach)')
    ax.plot(path_plunge1[:,0], path_plunge1[:,1], path_plunge1[:,2], 'b--', linewidth=1.5)
    ax.plot(path_corner[:,0], path_corner[:,1], path_corner[:,2], 'm-', linewidth=3, label='Corner Smooth Transition')
    ax.plot(path_air_transfer[:,0], path_air_transfer[:,1], path_air_transfer[:,2], 'b--', linewidth=1.5, label='Air Move (Transfer)')
    ax.plot(path_plunge3[:,0], path_plunge3[:,1], path_plunge3[:,2], 'b--', linewidth=1.5)
    ax.plot(path_return[:,0], path_return[:,1], path_return[:,2], 'b--', linewidth=1.5)

    # 实际焊接轨迹 (红色实线)
    ax.plot(path_weld1[:,0], path_weld1[:,1], path_weld1[:,2], 'r-', linewidth=4, label='Welding Path (X & Y & Z)')
    ax.plot(path_weld2[:,0], path_weld2[:,1], path_weld2[:,2], 'r-', linewidth=4)
    ax.plot(path_weld3[:,0], path_weld3[:,1], path_weld3[:,2], 'r-', linewidth=4)

    # 标记起点和关键路点
    ax.scatter(*home_pos, color='black', s=100, label='Home Position', marker='*')
    ax.scatter(*weld1_end, color='orange', s=50, label='Corner Waypoints')
    ax.scatter(*weld2_start, color='orange', s=50)

    # 设置特定视角的视图参数，确保能清晰看到三个面和死角
    ax.view_init(elev=25, azim=45)
    
    # 锁定坐标轴比例，防止模型变形
    ax.set_xlim([0, 10])
    ax.set_ylim([0, 10])
    ax.set_zlim([0, 10])
    
    # 轴标签设置
    ax.set_xlabel('X Axis (Wall 1)')
    ax.set_ylabel('Y Axis (Wall 2)')
    ax.set_zlabel('Z Axis (Floor)')
    ax.set_title('3-Sided Corner Welding Trajectory Planning')
    ax.legend(loc='upper left')

    plt.show()