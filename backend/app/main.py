from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.agents.model.client import aclose_shared_model_clients
from backend.app.api.admin import router as admin_router
from backend.app.api.routes import delivery_router, router as api_router
from backend.app.db.database import init_database
from backend.app.services import explore
from backend.app.services.scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    explore.cleanup_expired_exploration_briefs()
    explore.purge_expired_deleted_explorations()
    await explore.start_build_queue()
    await start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler()
        await explore.stop_build_queue()
        await aclose_shared_model_clients()


def create_app() -> FastAPI:
    app = FastAPI(title="Morning Dispatch", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(admin_router)
    app.include_router(api_router)
    app.include_router(delivery_router)
    mount_frontend(app)
    return app


def mount_frontend(app: FastAPI) -> None:
    dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    index_file = dist_dir / "index.html"
    if index_file.exists():
        @app.get("/{path:path}", include_in_schema=False)
        def spa_fallback(path: str) -> FileResponse:
            return FileResponse(index_file)


app = create_app()
