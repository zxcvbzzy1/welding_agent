"""Authenticated downloads of immutable PLY snapshots; no request accepts a disk path."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from im_backend.api.core import get_current_user, get_im_service
from infra.tool.help.point_cloud_artifacts import read_snapshot


router = APIRouter()


@router.get("/point-clouds/{point_cloud_id}")
def download_point_cloud(point_cloud_id: str, user=Depends(get_current_user), service=Depends(get_im_service)):
    try:
        record, path = read_snapshot(point_cloud_id)
    except (OSError, ValueError, TypeError):
        raise HTTPException(status_code=404, detail="点云产物不存在或已不可用") from None
    run = service._store.find_one("runs", {"run_id": record["run_id"]})
    owner = ((run or {}).get("memory_scope") or {}).get("user_id")
    if not owner or owner != user["user_id"]:
        raise HTTPException(status_code=404, detail="点云产物不存在或无权访问")
    return FileResponse(path, filename=record["source_label"], media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
