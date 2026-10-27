"""Dependency-free client for both tools; server jobs survive individual polls."""
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler


class WeldServiceError(RuntimeError):
    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def open_request(method, path, *, data=None, headers=None):
    """Shared HTTP errors for JSON requests and streaming file transfers."""
    base = os.getenv("WELD_SERVICE_URL", "http://100.82.31.1:18800").rstrip("/")
    req = Request(base + path, data=data, method=method, headers=headers or {})
    try:
        return build_opener(ProxyHandler({})).open(req, timeout=30)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise WeldServiceError(f"Weld service HTTP {exc.code}: {detail}", status_code=exc.code) from exc
    except (URLError, TimeoutError) as exc:
        raise WeldServiceError(f"Cannot reach weld service at {base}: {exc}") from exc


def request(method, path, payload=None):
    body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    with open_request(method, path, data=body, headers={"Content-Type": "application/json"}) as response:
        return None if response.status == 204 else json.load(response)


def run_job(endpoint, arguments, *, cancel=None):
    from .weld_service_files import prepare_inputs, cleanup_uploads, cache_extraction
    if cancel is not None and cancel.is_set():
        raise WeldServiceError("Tool call cancelled")
    uploads, job = [], None
    completed = False
    cleanup_safe = True
    try:
        arguments = prepare_inputs(endpoint, arguments, uploads, cancel=cancel)
        cleanup_safe = False
        try:
            job = request("POST", endpoint, arguments)
        except WeldServiceError as exc:
            cleanup_safe = exc.status_code is not None and (400 <= exc.status_code < 500 or exc.status_code == 503)
            raise
        path = "/api/v1/jobs/" + job["job_id"]
        deadline = time.monotonic() + float(os.getenv("WELD_SERVICE_TIMEOUT_S", "3600"))
        while True:
            if cancel is not None and cancel.is_set():
                raise WeldServiceError("Tool call cancelled")
            if time.monotonic() >= deadline:
                raise WeldServiceError("Weld service job timed out")
            job = request("GET", path)
            if job["status"] == "succeeded":
                completed = True
                result = job["result"]
                if endpoint in {"/api/v1/extractions/point-cloud", "/api/v1/extractions/stl"}:
                    result = cache_extraction(job["job_id"], result)
                return result
            if job["status"] in {"failed", "cancelled"}:
                completed = True
                error = job.get("error") or {}
                raise WeldServiceError(error.get("message", "Job cancelled"))
            if cancel is not None:
                cancel.wait(.2)
            else:
                time.sleep(.2)
    finally:
        if job is not None and not completed:
            try:
                status = request("POST", path + "/cancel")
                cleanup_safe = status["status"] in {"succeeded", "cancelled"}
            except WeldServiceError:
                pass
        if completed or cleanup_safe:
            cleanup_uploads(uploads)
