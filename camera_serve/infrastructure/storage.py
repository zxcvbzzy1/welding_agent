from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from camera_serve.domain.exceptions import CaptureFileNotFoundError


class LocalCaptureStorage:
    def __init__(self, service_root: Path) -> None:
        self._service_root = service_root.resolve()
        self._root = (self._service_root / "captures").resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def create_capture_directory(self) -> tuple[str, Path]:
        now = datetime.now(timezone.utc)
        capture_id = f"{now.strftime('%H%M%S_%f')}_{uuid4().hex[:8]}"
        directory = self._root / now.strftime("%Y%m%d") / capture_id
        directory.mkdir(parents=True, exist_ok=False)
        return capture_id, directory

    def relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self._service_root).as_posix()

    def resolve_file(self, relative_path: str) -> Path:
        candidate = (self._service_root / relative_path).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise CaptureFileNotFoundError("Capture path is outside the storage root")
        if not candidate.is_file():
            raise CaptureFileNotFoundError(f"Capture file not found: {relative_path}")
        return candidate

