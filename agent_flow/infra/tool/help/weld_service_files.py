"""File transport for remote tools. No event binding or framework dependencies."""
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4
from .weld_service_client import WeldServiceError, open_request, request

CHUNK_SIZE = 1024 * 1024


def upload_local(path, *, cancel=None):
    source = Path(path).expanduser()
    boundary = "weld-" + uuid4().hex
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input{source.suffix}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def chunks():
        yield head
        with source.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                if cancel is not None and cancel.is_set():
                    raise WeldServiceError("Tool call cancelled during upload")
                yield chunk
        yield tail

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "Content-Length": str(len(head) + source.stat().st_size + len(tail))}
    with open_request("POST", "/api/v1/files", data=chunks(), headers=headers) as response:
        return json.load(response)


def prepare_inputs(endpoint, arguments, uploads, *, cancel=None):
    """Local paths are uploaded; a server-produced path can pass through unchanged."""
    prepared = copy.deepcopy(arguments)
    key = {"/api/v1/extractions/point-cloud": "point_cloud_path", "/api/v1/extractions/stl": "stl_path",
           "/api/v1/simulations": "trajectory_path"}.get(endpoint)
    targets = [(prepared, key)] if key else []
    if endpoint == "/api/v1/simulations" and prepared.get("workpiece"):
        targets.append((prepared["workpiece"], "stl_path"))
    for container, field in targets:
        value = container.get(field)
        if value and Path(value).expanduser().is_file():
            metadata = upload_local(value, cancel=cancel)
            uploads.append(metadata["file_id"])
            container[field] = metadata["file_path"]
    return prepared


def cleanup_uploads(identities):
    for identity in identities:
        try:
            request("DELETE", f"/api/v1/files/{identity}")
        except WeldServiceError:
            # Cleanup must never replace a successful result or the original error.
            pass


def cache_extraction(job_id, result):
    """Keep the original tool's local JSON/CSV/image return contract."""
    default = Path(__file__).resolve().parents[3] / "temp" / "point_cloud_welding"
    root = Path(os.getenv("WELD_CLIENT_ARTIFACT_ROOT", str(default))).expanduser().resolve()
    destination = root / uuid4().hex
    destination.mkdir(parents=True)
    cached = copy.deepcopy(result)
    try:
        for kind, filename in {"trajectory": "trajectory.json", "csv": "trajectory.csv", "image": "seams.png"}.items():
            target = destination / filename
            with open_request("GET", f"/api/v1/jobs/{job_id}/files/{kind}") as response, target.open("wb") as stream:
                shutil.copyfileobj(response, stream, CHUNK_SIZE)
            cached[kind + "_path"] = str(target)
        cached["inline_artifact_arguments"]["image"]["file_path"] = cached["image_path"]
        return cached
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def cache_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
