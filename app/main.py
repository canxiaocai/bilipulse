import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.auth import AdminVerifyRateLimiter
from app.bilibili.collectors import CrawlService
from app.config import STATIC_DIR
from app.database import Database, db as default_db
from app.routers.accounts import router as accounts_router
from app.routers.admin import router as admin_router
from app.routers.dashboard import router as dashboard_router
from app.routers.settings import router as settings_router
from app.scheduler import SchedulerManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def create_app(
    database: Optional[Database] = None,
    enable_scheduler: bool = True,
) -> FastAPI:
    active_db = database or default_db
    crawl_service = CrawlService(active_db)
    scheduler_mgr = SchedulerManager(active_db, crawl_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if enable_scheduler:
            scheduler_mgr.start()
        try:
            yield
        finally:
            scheduler_mgr.shutdown()

    application = FastAPI(
        title="Bilibili Monitor",
        description="每 5 分钟定时监控 B站账号粉丝量与全视频实时在线观看人数",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.state.db = active_db
    application.state.crawl_service = crawl_service
    application.state.scheduler = scheduler_mgr
    application.state.enable_scheduler = enable_scheduler
    application.state.admin_rate_limiter = AdminVerifyRateLimiter()

    application.include_router(admin_router)
    application.include_router(accounts_router)
    application.include_router(dashboard_router)
    application.include_router(settings_router)

    if STATIC_DIR.exists():
        application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @application.get("/", include_in_schema=False)
        async def serve_index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

    return application


app = create_app()
