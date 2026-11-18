import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.bilibili.collectors import CrawlService
from app.database import Database

logger = logging.getLogger(__name__)

JOB_ID = "bilibili_monitor_crawl"
RETENTION_JOB_ID = "bilibili_monitor_retention"
RETENTION_INTERVAL_HOURS = 6
_UNSET = object()


class SchedulerManager:
    """Manages periodic background crawling and maintenance using APScheduler."""

    def __init__(self, database: Database, crawl_service: CrawlService) -> None:
        self.db = database
        self.crawl_service = crawl_service
        self.scheduler = AsyncIOScheduler()
        self._started = False
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    def spawn_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: Optional[str] = None,
    ) -> asyncio.Task[Any]:
        """Track a fire-and-forget task, remove on completion, and log unexpected errors."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._bg_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Background task %s failed", done_task.get_name())

        task.add_done_callback(_on_done)
        return task

    def start(self) -> None:
        if not self._started:
            self.scheduler.start()
            self._started = True
        self.reschedule()

        if hasattr(self.db, "prune_old_snapshots"):
            self.spawn_background_task(
                self._safe_prune_snapshots(),
                name="startup_snapshot_retention",
            )

        # Run initial background crawl on startup for enabled accounts lacking any snapshot
        enabled_accounts = self.db.get_enabled_accounts()
        missing_uids = [
            acc["uid"]
            for acc in enabled_accounts
            if self.db.get_latest_two_snapshots(acc["uid"])[0] is None
        ]
        if missing_uids:
            self.spawn_background_task(
                self._safe_crawl_missing(missing_uids),
                name="startup_missing_crawl",
            )

    def shutdown(self) -> None:
        for task in list(self._bg_tasks):
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False

    async def _safe_crawl(self, force_sync_videos: bool = False) -> None:
        try:
            await self.crawl_service.run_crawl_all(force_sync_videos=force_sync_videos)
        except Exception:
            logger.exception("Scheduled crawl task failed")

    async def _safe_crawl_missing(self, uids: list[str]) -> None:
        for uid in uids:
            try:
                await self.crawl_service.run_crawl(uid=uid, force_sync_videos=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Startup crawl task failed for UID %s", uid)

    async def _safe_prune_snapshots(self) -> None:
        if not hasattr(self.db, "prune_old_snapshots"):
            return
        try:
            result = await asyncio.to_thread(self.db.prune_old_snapshots)
            deleted = result.get("deleted_snapshots", 0) if isinstance(result, dict) else 0
            if deleted:
                logger.info(
                    "Pruned %d expired snapshot(s) (retention=%s days)",
                    deleted,
                    result.get("retention_days"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled snapshot retention task failed")

    def _ensure_retention_job(self) -> None:
        if not self._started or not hasattr(self.db, "prune_old_snapshots"):
            return
        if self.scheduler.get_job(RETENTION_JOB_ID) is None:
            self.scheduler.add_job(
                self._safe_prune_snapshots,
                trigger=IntervalTrigger(hours=RETENTION_INTERVAL_HOURS),
                id=RETENTION_JOB_ID,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )

    def reschedule(self) -> None:
        if not self._started:
            return
        self._ensure_retention_job()
        pub = self.db.get_public_settings()
        enabled_accounts = self.db.get_enabled_accounts()
        existing = self.scheduler.get_job(JOB_ID)

        if not enabled_accounts:
            if existing:
                self.scheduler.remove_job(JOB_ID)
            logger.info("Crawl scheduler paused (no enabled accounts)")
            return

        interval = max(1, int(pub["interval_minutes"]))
        if existing and isinstance(existing.trigger, IntervalTrigger):
            if int(existing.trigger.interval.total_seconds()) == interval * 60:
                logger.info(
                    "Kept existing %d minute(s) crawl schedule for %d account(s)",
                    interval,
                    len(enabled_accounts),
                )
                return

        if existing:
            self.scheduler.remove_job(JOB_ID)

        self.scheduler.add_job(
            self._safe_crawl,
            trigger=IntervalTrigger(minutes=interval),
            id=JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduled crawl every %d minute(s) for %d account(s)",
            interval,
            len(enabled_accounts),
        )

    def get_status(
        self,
        uid: Optional[str] = None,
        *,
        pub_settings: Optional[dict[str, Any]] = None,
        latest_snapshot: Any = _UNSET,
    ) -> dict[str, Any]:
        pub = pub_settings if pub_settings is not None else self.db.get_public_settings(uid=uid)
        enabled_accounts = self.db.get_enabled_accounts()
        job = self.scheduler.get_job(JOB_ID) if self._started else None
        next_run: Optional[str] = None
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

        if latest_snapshot is _UNSET:
            latest: Optional[dict[str, Any]] = None
            if pub["uid"]:
                latest, _ = self.db.get_latest_two_snapshots(pub["uid"])
        else:
            latest = latest_snapshot

        return {
            "running": bool(self._started and self.scheduler.running),
            "enabled": bool(enabled_accounts),
            "interval_minutes": pub["interval_minutes"],
            "next_run_time": next_run,
            "is_crawling": self.crawl_service.is_crawling,
            "crawling_uid": self.crawl_service.crawling_uid,
            "enabled_accounts_count": len(enabled_accounts),
            "last_crawl_at": latest["collected_at"] if latest else None,
            "last_crawl_status": latest["status"] if latest else None,
        }
