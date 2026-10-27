"""Durable PLY snapshots shared by the artifact tool and authenticated IM routes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[4]
MAX_POINT_CLOUD_BYTES = 100 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024


def snapshot_root() -> Path:
    default = REPO_ROOT / "agent_flow" / "store" / "point_clouds"
    return Path(os.getenv("POINT_CLOUD_ARTIFACT_ROOT", str(default))).expanduser().resolve()


def allowed_roots() -> list[Path]:
    roots = [REPO_ROOT / "upload", REPO_ROOT / "agent_flow" / "temp"]
    roots.extend(Path(value).expanduser() for value in os.getenv("POINT_CLOUD_ALLOWED_ROOTS", "").split(os.pathsep) if value.strip())
    return [root.resolve() for root in roots]


def read_ply_header(stream) -> int:
    """Validate a bounded ASCII header, leaving the binary/ascii body untouched."""
    header = bytearray()
    while len(header) < MAX_HEADER_BYTES:
        line = stream.readline(MAX_HEADER_BYTES - len(header))
        header.extend(line)
        if not line:
            break
        if line.rstrip(b"\r\n") == b"end_header":
            break
    lines = header.decode("ascii", errors="replace").splitlines()
    if not lines or lines[0] != "ply" or lines[-1] != "end_header":
        raise ValueError("文件不是有效的 PLY，或文件头超过 64 KiB")
    if not any(line in {"format ascii 1.0", "format binary_little_endian 1.0", "format binary_big_endian 1.0"} for line in lines):
        raise ValueError("不支持的 PLY 格式")
    vertex_count = 0
    vertex_properties = set()
    current_element = ""
    for line in lines:
        values = line.split()
        if values[:1] == ["element"] and len(values) == 3:
            current_element = values[1]
            if current_element == "vertex":
                vertex_count = int(values[2])
        elif current_element == "vertex" and values[:1] == ["property"] and len(values) == 3:
            vertex_properties.add(values[2])
    if vertex_count <= 0 or not {"x", "y", "z"}.issubset(vertex_properties):
        raise ValueError("PLY 必须包含非空 vertex 点集及 x、y、z 坐标")
    return vertex_count


def create_snapshot(file_path: str, *, run_id: str) -> dict:
    if not run_id:
        raise ValueError("本地点云预览需要当前运行上下文")
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        raise ValueError("point_cloud.file_path 必须是绝对路径")
    path = path.resolve()
    if not any(path.is_relative_to(root) for root in allowed_roots()):
        raise ValueError("点云路径不在允许目录内；请使用 upload、agent_flow/temp 或配置 POINT_CLOUD_ALLOWED_ROOTS")
    if path.suffix.lower() != ".ply" or not path.is_file():
        raise ValueError("点云文件不存在或不是 .ply 文件")

    identity = uuid4().hex
    directory = snapshot_root() / identity
    directory.mkdir(parents=True)
    try:
        with path.open("rb") as source, (directory / "points.ply").open("xb") as output:
            if os.fstat(source.fileno()).st_size > MAX_POINT_CLOUD_BYTES:
                raise ValueError("点云文件不能超过 100 MiB")
            point_count = read_ply_header(source)
            source.seek(0)
            size = 0
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_POINT_CLOUD_BYTES:
                    raise ValueError("点云文件不能超过 100 MiB")
                output.write(chunk)
        record = {"point_cloud_id": identity, "run_id": run_id, "source_label": path.name,
                  "point_count": point_count, "size": size}
        # Metadata is the commit marker: incomplete copies cannot be downloaded.
        (directory / "metadata.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        return record
    except BaseException:
        shutil.rmtree(directory)
        raise


def read_snapshot(identity: str) -> tuple[dict, Path]:
    if re.fullmatch(r"[a-f0-9]{32}", identity) is None:
        raise FileNotFoundError("点云产物不存在")
    root = snapshot_root()
    directory = root / identity
    path = directory / "points.ply"
    metadata_path = directory / "metadata.json"
    if directory.is_symlink() or path.is_symlink() or metadata_path.is_symlink():
        raise FileNotFoundError("点云产物不存在")
    record = json.loads(metadata_path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("point_cloud_id") != identity
            or not record.get("run_id") or not isinstance(record.get("source_label"), str)
            or path.stat().st_size != record.get("size")):
        raise ValueError("点云产物已损坏")
    return record, path
