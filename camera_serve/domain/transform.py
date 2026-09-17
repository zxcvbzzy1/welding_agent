from __future__ import annotations

import numpy as np

from .models import Point3D


def transform_points(
    points: list[Point3D], transformation: list[list[float]]
) -> list[Point3D]:
    """Apply target_from_source homogeneous transformation to XYZ points."""
    matrix = np.asarray(transformation, dtype=np.float64)
    if matrix.shape != (4, 4):
        raise ValueError("transformation must be a 4x4 matrix")
    if not np.allclose(matrix[3], [0.0, 0.0, 0.0, 1.0]):
        raise ValueError("the last matrix row must be [0, 0, 0, 1]")
    if not points:
        return []

    xyz = np.asarray([[point.x, point.y, point.z] for point in points])
    homogeneous = np.column_stack((xyz, np.ones(len(points))))
    transformed = (matrix @ homogeneous.T).T[:, :3]
    return [Point3D(*row.tolist()) for row in transformed]

