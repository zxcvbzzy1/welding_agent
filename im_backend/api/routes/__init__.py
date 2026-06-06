from __future__ import annotations

from fastapi import APIRouter

from im_backend.api.routes import agents, artifacts, files, conversations, deployments, favorites, messages, rooms, runs, skills, tools


router = APIRouter(prefix="/api/im", tags=["im"])
router.include_router(agents.router)
router.include_router(conversations.router)
router.include_router(rooms.router)
router.include_router(messages.router)
router.include_router(artifacts.router)
router.include_router(favorites.router)
router.include_router(deployments.router)
router.include_router(runs.router)
router.include_router(skills.router)
router.include_router(tools.router)
router.include_router(files.router)
