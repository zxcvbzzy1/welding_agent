"""Paper sections 3--5: local RANSAC, ordered planes, finite seams and poses."""

from dataclasses import dataclass
import warnings

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation


def normalized(value):
    value = np.asarray(value, dtype=float)
    length = np.linalg.norm(value)
    if not np.isfinite(length) or length < 1e-9:
        raise ValueError("几何向量退化，无法确定方向")
    return value / length


@dataclass
class Plane:
    center: np.ndarray
    normal: np.ndarray
    points: np.ndarray

    @property
    def offset(self):
        return float(self.normal @ self.center)

    def distances(self, points):
        return np.abs((points - self.center) @ self.normal)

    def to_dict(self, index):
        return {
            "id": f"P{index}", "centroid": self.center.tolist(),
            "normal": self.normal.tolist(), "offset_mm": self.offset,
            "point_count": len(self.points),
            "rmse_mm": float(np.sqrt(np.mean(self.distances(self.points) ** 2))),
        }


def _tls(points):
    center = points.mean(axis=0)
    _, singular, vh = np.linalg.svd(points - center, full_matrices=False)
    if len(singular) < 3 or singular[1] < 1e-6:
        raise ValueError("平面点集共线或退化")
    return center, vh[-1]


def _centroid_refit(points, rng, iterations, threshold):
    """Eqs. 5--7, followed by robust total least squares for sensor noise.

    Quadratic fitting in the paper means a second fit, not a curved surface.
    The centroid is a calculation point (it need not be a measured sample).
    """
    center = points.mean(axis=0)
    near = points[np.argsort(np.linalg.norm(points - center, axis=1))[:50]]
    candidates = []
    for _ in range(iterations):
        a, b = near[rng.choice(len(near), 2, replace=False)]
        cross = np.cross(a - center, b - center)
        if np.linalg.norm(cross) > 1e-8:
            normal = normalized(cross)
            residual = np.abs((points - center) @ normal)
            candidates.append((float(np.median(residual)), normal))
    if not candidates:
        raise ValueError("质心邻域退化，无法精拟合平面")
    normal = min(candidates, key=lambda item: item[0])[1]
    residual = np.abs((points - center) @ normal)
    core = points[residual <= max(threshold, np.quantile(residual, 0.7))]
    for _ in range(5):
        center, normal = _tls(core)
        residual = np.abs((points - center) @ normal)
        median = np.median(residual)
        mad = np.median(np.abs(residual - median))
        cutoff = min(threshold, max(0.25, median + 3.5 * 1.4826 * mad))
        core = points[residual <= cutoff]
        if len(core) < 12:
            raise ValueError("精拟合平面内点不足")
    center, normal = _tls(core)
    return Plane(center, normal, core)


def fit_planes(points, count, *, seed=0, iterations=10, threshold=2.0):
    rng = np.random.default_rng(seed)
    remaining = points.copy()
    planes = []
    minimum = max(30, int(len(points) * 0.003))
    for _ in range(count):
        if len(remaining) < minimum:
            raise ValueError(f"平面数量不足，模型需要 {count} 个平面")
        tree = cKDTree(remaining)
        best = None
        for _ in range(iterations):
            first = remaining[rng.integers(len(remaining))]
            neighbors = tree.query_ball_point(first, 10.0, p=np.inf)
            if len(neighbors) < 4:
                continue
            local = remaining[neighbors]
            local = local[np.argsort(np.linalg.norm(local - first, axis=1))[:51]]
            local = local[np.linalg.norm(local - first, axis=1) > 1e-8][:50]
            if len(local) < 2:
                continue
            a, b = local[rng.choice(len(local), 2, replace=False)]
            cross = np.cross(a - first, b - first)
            if np.linalg.norm(cross) < 1e-8:
                continue
            normal = normalized(cross)
            mask = np.abs((remaining - first) @ normal) <= threshold
            score = int(mask.sum())
            if best is None or score > best[0]:
                best = (score, mask)
        if best is None or best[0] < minimum:
            raise ValueError("RANSAC 平面支撑不足；请检查点密度、模型或增加 ransac_iterations")
        plane = _centroid_refit(remaining[best[1]], rng, iterations, threshold)
        planes.append(plane)
        remaining = remaining[~(best[1] | (plane.distances(remaining) <= threshold))]

    # Restore the boundary strips removed by sequential rough segmentation.
    distances = np.column_stack([plane.distances(points) for plane in planes])
    labels = distances.argmin(axis=1)
    for index, plane in enumerate(planes):
        cloud = points[(labels == index) & (distances[:, index] <= threshold)]
        if len(cloud) < minimum:
            raise ValueError("精拟合后的独立平面支撑不足")
        planes[index] = _centroid_refit(cloud, rng, iterations, threshold)
    return planes


def order_planes(planes, model_type, viewpoint):
    horizontal = [p for p in planes if abs(p.normal[2]) >= np.cos(np.deg2rad(15))]
    vertical = [p for p in planes if abs(p.normal[2]) <= np.sin(np.deg2rad(15))]
    expected_horizontal = {"three_plane": 1, "h_section": 2, "square_tube": 3}[model_type]
    if len(horizontal) != expected_horizontal or len(vertical) != 2:
        raise ValueError("平面结构不符合所选构件；要求水平放置、两个相交立面及对应数量的水平面")
    horizontal.sort(key=lambda p: p.center[2])
    if any(b.center[2] - a.center[2] < 4 for a, b in zip(horizontal, horizontal[1:])):
        raise ValueError("水平面高度过近或重复，无法区分构件平面")
    for plane in planes:
        facing = float(plane.normal @ (viewpoint - plane.center))
        if abs(facing) < 1e-6:
            raise ValueError("相机位置位于拟合平面上，无法确定可见面方向")
        if facing < 0:
            plane.normal = -plane.normal
    base = horizontal[0]
    # Lowest face normal is upward; ordering uses a scalar triple product,
    # avoiding the division by a normal component printed in Eq. 11.
    if base.normal[2] < 0:
        raise ValueError("相机必须位于底面上方")
    if np.linalg.norm(np.cross(vertical[0].normal, vertical[1].normal)) < 0.2:
        raise ValueError("两个立面平行或近似平行，无法提取交线")
    if np.dot(np.cross(vertical[0].normal, vertical[1].normal), base.normal) < 0:
        vertical.reverse()
    return [base, *vertical, *horizontal[1:]]


@dataclass
class Line:
    center: np.ndarray
    direction: np.ndarray
    low: float
    high: float
    support_count: int
    rmse: float

    @property
    def endpoints(self):
        return self.center + self.low * self.direction, self.center + self.high * self.direction


def intersection_line(a, b, planes, points, band=2.0):
    direction = normalized(np.cross(a.normal, b.normal))
    normals = np.array([a.normal, b.normal])
    distances = np.column_stack([p.distances(points) for p in planes])
    labels = distances.argmin(axis=1)
    groups, extent_groups = [], []
    for plane, other in ((a, b), (b, a)):
        index = next(i for i, p in enumerate(planes) if p is plane)
        surface_mask = (labels == index) & (plane.distances(points) <= band)
        group = points[surface_mask & (other.distances(points) <= band)]
        if len(group) < 8:
            raise ValueError("平面交线缺少两侧有效支撑点")
        groups.append(group)
        # Use a wider strip solely for longitudinal bounds. A narrow strip has
        # too few samples at a sparse terminal edge; plane labels still prevent
        # the infinite continuation of a shorter face becoming a false seam.
        extent_groups.append(points[surface_mask & (other.distances(points) <= max(8.0, band))])
    support = np.concatenate(groups)
    mean = support.mean(axis=0)
    _, singular, vh = np.linalg.svd(support - mean, full_matrices=False)
    if singular[0] < max(3 * singular[1], 1e-6):
        raise ValueError("交线支撑点不具有稳定的直线方向")
    if vh[0] @ direction < 0:
        direction = -direction
    # Project SVD's location/direction onto the two fitted planes. This removes
    # the one-sided sampling bias of thick edge bands, preserving their extent.
    origin = mean + normals.T @ np.linalg.solve(normals @ normals.T, [a.offset, b.offset] - normals @ mean)
    bounds = [np.quantile((group - origin) @ direction, [0.005, 0.995]) for group in extent_groups]
    low = max(bound[0] for bound in bounds)
    high = min(bound[1] for bound in bounds)
    if high - low < 3:
        raise ValueError("相交平面的有限支撑范围不重叠或交线过短")
    residual = np.linalg.norm(np.cross(support - origin, direction), axis=1)
    return Line(origin, direction, float(low), float(high), len(support), float(np.sqrt(np.mean(residual ** 2))))


def line_plane(line, plane):
    denominator = float(plane.normal @ line.direction)
    if abs(denominator) < 1e-6:
        raise ValueError("直线与平面平行，无法确定焊缝端点")
    t = (plane.offset - plane.normal @ line.center) / denominator
    # A small edge band is allowed because rough segmentation removes corners.
    if t < line.low - 3 or t > line.high + 3:
        raise ValueError("焊缝交点位于点云有限支撑范围之外")
    return line.center + t * line.direction


def closest_intersection(first, second):
    matrix = np.column_stack((first.direction, -second.direction))
    if np.linalg.matrix_rank(matrix) < 2:
        raise ValueError("焊缝交线平行，不能确定公共端点")
    t, u = np.linalg.lstsq(matrix, second.center - first.center, rcond=None)[0]
    p = first.center + t * first.direction
    q = second.center + u * second.direction
    if np.linalg.norm(p - q) > 3:
        raise ValueError("焊缝交线不相交，构件结构不匹配")
    return (p + q) / 2


def extract_segments(planes, points, model_type, h_width_mm=None, tube_width_mm=None):
    p1, p2, p3 = planes[:3]
    lines = {}

    def line(i, j):
        key = tuple(sorted((i, j)))
        if key not in lines:
            lines[key] = intersection_line(planes[i - 1], planes[j - 1], planes, points)
        return lines[key]

    def far_end(segment, junction):
        return max(segment.endpoints, key=lambda point: np.linalg.norm(point - junction))

    if model_type == "three_plane":
        l23, l12, l13 = line(2, 3), line(1, 2), line(1, 3)
        # Eq. 14 / Fig. 19: average the three nearest finite endpoints.
        junction_guess = np.linalg.solve(np.array([p.normal for p in planes]), [p.offset for p in planes])
        near = [min(l.endpoints, key=lambda x: np.linalg.norm(x - junction_guess)) for l in (l23, l12, l13)]
        if max(np.linalg.norm(x - junction_guess) for x in near) > 4:
            raise ValueError("三条焊缝没有共同端点")
        junction = np.mean(near, axis=0)
        definitions = [(far_end(l23, junction), junction, (2, 3)),
                       (junction, far_end(l12, junction), (1, 2)),
                       (junction, far_end(l13, junction), (1, 3))]
    elif model_type == "h_section":
        axis = line(2, 3)
        a, b = line_plane(axis, planes[3]), line_plane(axis, p1)
        d, f = a + h_width_mm / 2 * p2.normal, b + h_width_mm / 2 * p2.normal
        c, e = d + h_width_mm / 2 * p3.normal, d - h_width_mm / 2 * p3.normal
        g = f + h_width_mm / 2 * p3.normal
        m, n = a + (b - a) / 6, b + (a - b) / 6
        definitions = [(m, n, (2, 3)), (e, c, (4, 4)), (b, f, (1, 3)), (f, g, (1, 1))]
    else:
        l23, l24, l12, l13 = line(2, 3), line(2, 4), line(1, 2), line(1, 3)
        a = closest_intersection(l23, l24)
        b = np.mean([closest_intersection(l23, l12), closest_intersection(l23, l13),
                     closest_intersection(l12, l13)], axis=0)
        h = a - tube_width_mm * p3.normal
        definitions = [(a, b, (2, 3)), (b, far_end(l12, b), (1, 2)),
                       (b, far_end(l13, b), (1, 3)), (a, h, (2, 4))]
    result = []
    for i, (start, end, pair) in enumerate(definitions, 1):
        length = float(np.linalg.norm(end - start))
        if length < 3 or not np.isfinite(length):
            raise ValueError("焊缝长度退化")
        if model_type != "three_plane":
            for plane_index in set(pair):
                plane = planes[plane_index - 1]
                surface = points[plane.distances(points) <= 1.0]
                if len(surface) < 8:
                    raise ValueError("构件尺寸与点云支撑范围不符，或焊缝存在较大缺口")
                tree = cKDTree(surface)
                if tree.query(np.asarray([start, end]))[0].max() > 3.0:
                    raise ValueError("构件尺寸与点云支撑范围不符，或焊缝存在较大缺口")
                probes = np.linspace(start, end, min(5000, max(3, int(np.ceil(length / 2)) + 1)))
                if tree.query(probes)[0].max() > 3.0:
                    raise ValueError("构件尺寸与点云支撑范围不符，或焊缝存在较大缺口")
        result.append({"id": f"L{i}", "start": start.tolist(), "end": end.tolist(),
                       "length_mm": length, "travel_direction": ((end - start) / length).tolist(),
                       "support_planes": [f"P{j}" for j in pair]})
    return result, {f"P{i}-P{j}": {"point_count": l.support_count, "band_rmse_mm": l.rmse}
                    for (i, j), l in lines.items()}


def _edge_line(points, start, end, tangent, radial, width, step):
    relative = points - start
    along = relative @ tangent
    radial_dist = relative @ radial
    # Reject unrelated portions of the plane and require measured edge support.
    lateral = relative - along[:, None] * tangent
    mask = ((along >= -step) & (along <= np.linalg.norm(end - start) + step)
            & (radial_dist > 0.25) & (radial_dist <= width)
            & (np.linalg.norm(lateral, axis=1) <= width * 1.15))
    local, along, radial_dist = points[mask], along[mask], radial_dist[mask]
    if len(local) < 12:
        raise ValueError("姿态邻域缺少有效边缘支撑点")
    probes = np.linspace(0, np.linalg.norm(end - start),
                         max(3, int(np.ceil(np.linalg.norm(end - start) / step)) + 1))
    coverage = cKDTree(along[:, None]).query(probes[:, None])[0]
    if coverage.max() > max(2 * step, 2.0):
        raise ValueError("姿态边缘支撑未覆盖整条焊缝")
    bins = np.floor(along / max(step * 2, 2.0)).astype(int)
    edge = []
    for key in np.unique(bins):
        indices = np.flatnonzero(bins == key)
        if len(indices) < 2:
            continue
        chosen = indices[radial_dist[indices] >= np.quantile(radial_dist[indices], 0.75)]
        edge.extend(local[chosen])
    if len(edge) < 6 or len(np.unique(bins)) < 3:
        raise ValueError("姿态边线的纵向支撑不足")
    edge = np.asarray(edge)
    center = edge.mean(axis=0)
    _, singular, vh = np.linalg.svd(edge - center, full_matrices=False)
    if singular[0] < 1e-6 or abs(vh[0] @ tangent) < 0.85:
        raise ValueError("姿态边线不平行于焊缝")
    # Eq. 24 needs parallel edges at equal longitudinal interpolation positions.
    # Constrain the SVD fit to the already estimated seam tangent.
    return center, len(local)


def plan_seam_pose(seam, planes, points, viewpoint, *, sample_step_mm=2.0,
                   posture_width_mm=5.0, approach_distance_mm=10.0,
                   retract_distance_mm=10.0, tcp_rotation_offset_deg=(0, 0, 0)):
    start, end = np.asarray(seam["start"]), np.asarray(seam["end"])
    tangent = normalized(end - start)
    ratio = seam["length_mm"] / sample_step_mm
    if not np.isfinite(ratio) or ratio > 49997:
        raise ValueError("单条轨迹超过 50000 点，请增大 sample_step_mm")
    samples = np.linspace(start, end, max(2, int(np.ceil(ratio)) + 1))
    pair = [planes[int(name[1:]) - 1] for name in seam["support_planes"]]
    distances = np.column_stack([p.distances(points) for p in planes])
    labels = distances.argmin(axis=1)
    radial = normalized(np.cross(tangent, pair[0].normal))
    if radial @ (viewpoint - (start + end) / 2) < 0:
        radial = -radial
    directions = [radial]
    if pair[0] is pair[1]:
        directions.append(-radial)
    else:
        radial = normalized(np.cross(tangent, pair[1].normal))
        if radial @ (viewpoint - (start + end) / 2) < 0:
            radial = -radial
        directions.append(radial)
    edges, support = [], []
    for plane, radial in zip(pair, directions):
        index = next(i for i, candidate in enumerate(planes) if candidate is plane)
        cloud = points[(labels == index) & (distances[:, index] <= 2.0)]
        edge, count = _edge_line(cloud, start, end, tangent, radial, posture_width_mm, sample_step_mm)
        edges.append(edge)
        support.append(count)
    if pair[0] is pair[1]:
        zs = pair[0].normal.copy()
        method = "coplanar_surface_normal"
    else:
        vectors = [edge - start - np.dot(edge - start, tangent) * tangent for edge in edges]
        zs = normalized(sum(normalized(v) for v in vectors))
        method = "dihedral_edge_bisector"
    zg = -normalized(zs - (zs @ tangent) * tangent)
    yg = normalized(np.cross(zg, tangent))  # Correct right-handed version of Eq. 25.
    rotation = np.column_stack((tangent, yg, zg))
    correction = Rotation.from_euler("xyz", tcp_rotation_offset_deg, degrees=True).as_matrix()
    rotation = rotation @ correction
    if (not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-7)
            or not np.isclose(np.linalg.det(rotation), 1, atol=1e-7)):
        raise ValueError("焊枪姿态旋转矩阵无效")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # Deterministic equivalent RPY at gimbal lock.
        angles = Rotation.from_matrix(rotation).as_euler("xyz", degrees=True)
    poses = np.column_stack((samples, np.tile(angles, (len(samples), 1))))
    approach = np.r_[start - zg * approach_distance_mm, angles].tolist()
    retract = np.r_[end - zg * retract_distance_mm, angles].tolist()
    waypoints = [{"stage": "APPROACH", "pose": approach}]
    waypoints.extend({"stage": "WELDING", "pose": p.tolist()} for p in poses)
    waypoints.append({"stage": "RETRACT", "pose": retract})
    return {"executable": True, "poses": poses.tolist(), "approach_pose": approach,
            "retract_pose": retract, "waypoints": waypoints,
            "quality": {"orientation_method": method, "edge_support_points": support,
                        "rotation_determinant": float(np.linalg.det(rotation))}}
