import asyncio
from typing import Any, Optional
from fastapi import APIRouter, Query, Request

from app.auth import get_admin_status_payload
from app.models import AdminAuthStatus, DashboardSummary, FollowerDailyChange

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/dashboard", response_model=DashboardSummary)
async def get_dashboard(
    request: Request,
    hours: int = Query(default=24, ge=0, le=8760, description="Time window in hours; 0 for all"),
    uid: Optional[str] = Query(default=None, description="Optional account UID to inspect"),
) -> DashboardSummary:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service
    target_uid = uid.strip() if uid is not None else None

    def _assemble_dashboard() -> DashboardSummary:
        pub_settings = db.get_public_settings(uid=target_uid)
        active_uid = pub_settings["uid"]
        accounts = db.get_accounts_summary(crawling_uid=crawl_service.crawling_uid)
        admin_auth = AdminAuthStatus(**get_admin_status_payload(request))
        pub_settings["admin_auth"] = admin_auth

        if not active_uid:
            sched_status = scheduler_mgr.get_status(
                uid=target_uid,
                pub_settings=pub_settings,
                latest_snapshot=None,
            )
            return DashboardSummary(
                settings=pub_settings,
                scheduler=sched_status,
                admin_auth=admin_auth,
                follower_daily=FollowerDailyChange(**db.get_follower_daily_changes("")),
                accounts=accounts,
            )

        latest, prev = db.get_latest_two_snapshots(active_uid)
        sched_status = scheduler_mgr.get_status(
            uid=target_uid,
            pub_settings=pub_settings,
            latest_snapshot=latest,
        )

        follower_delta: Optional[int] = None
        online_delta: Optional[int] = None
        if latest and prev:
            if (
                latest.get("follower_count") is not None
                and prev.get("follower_count") is not None
            ):
                follower_delta = latest["follower_count"] - prev["follower_count"]
            if (
                latest.get("online_viewers_total") is not None
                and prev.get("online_viewers_total") is not None
            ):
                online_delta = latest["online_viewers_total"] - prev["online_viewers_total"]

        follower_daily = FollowerDailyChange(**db.get_follower_daily_changes(active_uid))
        snapshots = db.get_snapshots(
            active_uid, since_hours=hours if hours > 0 else None, limit=2000
        )
        videos = db.get_videos(active_uid)
        total_snapshots = db.count_snapshots(active_uid)

        return DashboardSummary(
            settings=pub_settings,
            scheduler=sched_status,
            admin_auth=admin_auth,
            latest=latest,
            previous=prev,
            follower_delta=follower_delta,
            online_delta=online_delta,
            follower_daily=follower_daily,
            total_snapshots=total_snapshots,
            total_videos=len(videos),
            snapshots=snapshots,
            top_videos=videos,
            accounts=accounts,
        )

    return await asyncio.to_thread(_assemble_dashboard)


@router.get("/videos")
async def get_videos(
    request: Request,
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
) -> dict[str, Any]:
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        target_uid = (uid if uid is not None else db.get_setting("uid", "")).strip()
        if not target_uid:
            return {"uid": "", "videos": []}
        return {"uid": target_uid, "videos": db.get_videos(target_uid)}

    return await asyncio.to_thread(_load)


@router.get("/videos/{bvid}/history")
async def get_video_history(
    request: Request,
    bvid: str,
    hours: Optional[int] = Query(
        default=None, ge=0, le=8760, description="Time window in hours; 0 for all"
    ),
    limit: Optional[int] = Query(default=None, ge=1, le=20000),
    max_points: Optional[int] = Query(default=None, ge=8, le=5000),
) -> dict[str, Any]:
    db = request.app.state.db

    def _load() -> list[dict[str, Any]]:
        if hours is not None:
            effective_limit = limit if limit is not None else 20000
            effective_max_points = (
                max_points
                if max_points is not None
                else (320 if limit is None else None)
            )
            return db.get_video_history(
                bvid,
                limit=effective_limit,
                since_hours=hours if hours > 0 else None,
                max_points=effective_max_points,
            )
        effective_limit = limit if limit is not None else 100
        return db.get_video_history(
            bvid, limit=effective_limit, max_points=max_points
        )

    history = await asyncio.to_thread(_load)
    return {"bvid": bvid, "hours": hours, "history": history}


@router.get("/crawl-logs")
async def get_crawl_logs(
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
) -> dict[str, Any]:
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        target_uid = (uid if uid is not None else db.get_setting("uid", "")).strip()
        if not target_uid:
            return {"uid": "", "logs": []}
        return {"uid": target_uid, "logs": db.get_recent_snapshots(target_uid, limit=limit)}

    return await asyncio.to_thread(_load)
