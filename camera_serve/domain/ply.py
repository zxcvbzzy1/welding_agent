from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True, slots=True)
class PlyProcessingResult:
    original_points: int
    saved_points: int


def process_ascii_ply(
    source: Path,
    destination: Path,
    *,
    max_points: int = 0,
    transformation: tuple[tuple[float, ...], ...] | None = None,
) -> PlyProcessingResult:
    """Uniformly sample and/or transform an SDK-generated ASCII PLY file."""
    header, vertex_count, coordinate_indexes = _read_header(source)
    saved_count = min(vertex_count, max_points) if max_points > 0 else vertex_count
    if source == destination and saved_count == vertex_count and transformation is None:
        return PlyProcessingResult(vertex_count, saved_count)

    matrix = None
    if transformation is not None:
        matrix = np.asarray(transformation, dtype=np.float64)
        if matrix.shape != (4, 4) or not np.allclose(
            matrix[3], [0.0, 0.0, 0.0, 1.0]
        ):
            raise ValueError("transformation must be a homogeneous 4x4 matrix")

    output = destination
    temporary = destination.with_name(f".{destination.name}.processing.tmp")
    if source == destination:
        output = temporary

    try:
        with source.open("r", encoding="ascii", newline="") as reader, output.open(
            "w", encoding="ascii", newline=""
        ) as writer:
            for line in _header_with_vertex_count(header, saved_count):
                writer.write(line)

            # Skip the source header already parsed by _read_header.
            for _ in header:
                reader.readline()

            next_sample = 0
            for index in range(vertex_count):
                line = reader.readline()
                if not line:
                    raise ValueError("PLY ended before all vertex rows were read")
                if saved_count < vertex_count:
                    wanted_index = (
                        0
                        if saved_count == 1
                        else (next_sample * (vertex_count - 1)) // (saved_count - 1)
                    )
                    if index != wanted_index:
                        continue
                    next_sample += 1
                if matrix is not None:
                    line = _transform_vertex(line, coordinate_indexes, matrix)
                writer.write(line)

            # Preserve any faces or other elements after the vertex block.
            for line in reader:
                writer.write(line)

        if source == destination:
            temporary.replace(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

    return PlyProcessingResult(vertex_count, saved_count)


def _read_header(source: Path) -> tuple[list[str], int, tuple[int, int, int]]:
    header: list[str] = []
    vertex_count: int | None = None
    vertex_properties: list[str] = []
    current_element = ""

    with source.open("r", encoding="ascii", newline="") as stream:
        while True:
            line = stream.readline()
            if not line:
                raise ValueError("Invalid PLY: missing end_header")
            header.append(line)
            stripped = line.strip()
            if stripped == "format ascii 1.0":
                pass
            elif stripped.startswith("format "):
                raise ValueError("Only ASCII PLY files are supported")
            elif stripped.startswith("element "):
                parts = stripped.split()
                current_element = parts[1]
                if current_element == "vertex":
                    vertex_count = int(parts[2])
            elif stripped.startswith("property ") and current_element == "vertex":
                vertex_properties.append(stripped.split()[-1])
            elif stripped == "end_header":
                break

    if vertex_count is None:
        raise ValueError("Invalid PLY: missing vertex element")
    try:
        coordinate_indexes = tuple(vertex_properties.index(axis) for axis in ("x", "y", "z"))
    except ValueError as exc:
        raise ValueError("Invalid PLY: vertex properties must include x, y and z") from exc
    return header, vertex_count, coordinate_indexes  # type: ignore[return-value]


def _header_with_vertex_count(header: list[str], count: int) -> list[str]:
    result: list[str] = []
    replaced = False
    for line in header:
        if not replaced and line.strip().startswith("element vertex "):
            newline = "\r\n" if line.endswith("\r\n") else "\n"
            result.append(f"element vertex {count}{newline}")
            replaced = True
        else:
            result.append(line)
    return result


def _transform_vertex(
    line: str, coordinate_indexes: tuple[int, int, int], matrix: np.ndarray
) -> str:
    values = line.split()
    x_index, y_index, z_index = coordinate_indexes
    point = np.array(
        [float(values[x_index]), float(values[y_index]), float(values[z_index]), 1.0]
    )
    transformed = matrix @ point
    values[x_index] = f"{transformed[0]:.9g}"
    values[y_index] = f"{transformed[1]:.9g}"
    values[z_index] = f"{transformed[2]:.9g}"
    return " ".join(values) + "\n"
