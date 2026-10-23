"""Authenticated playback snapshots; local source paths are never accepted here."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from im_backend.api.core import get_current_user, get_im_service
from infra.tool.help.robot_simulation import read_simulation

router = APIRouter()


@router.get("/robot-simulations/{simulation_id}")
async def get_robot_simulation(simulation_id: str, user=Depends(get_current_user), service=Depends(get_im_service)):
    try:
        data = await asyncio.to_thread(read_simulation, simulation_id)
    except (FileNotFoundError, ValueError, OSError):
        raise HTTPException(status_code=404, detail="仿真不存在或已不可用") from None
    run = await asyncio.to_thread(service._store.find_one, "runs", {"run_id": data["run_id"]})
    # Identity comes from the persisted run, never caller-supplied tool arguments.
    owner = ((run or {}).get("memory_scope") or {}).get("user_id")
    if not owner or owner != user["user_id"]:
        raise HTTPException(status_code=404, detail="仿真不存在或无权访问")
    return {"item": data}
