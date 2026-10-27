"""Compatibility entry point: computation belongs to the FastAPI service."""


def extract_weld_seams(**arguments):
    from ..weld_service_client import run_job
    cancel = arguments.pop("cancel", None)
    return run_job("/api/v1/extractions/point-cloud", arguments, cancel=cancel)
