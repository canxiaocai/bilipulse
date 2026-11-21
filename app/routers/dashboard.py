import asyncio
import json
from typing import Any, AsyncIterator, Optional
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.auth import get_admin_status_payload
from app.models import AdminAuthStatus, ChartSnapshotOut, DashboardSummary, FollowerDailyChange

router = APIRouter(prefix="/api", tags=["dashboard"])

SSE_HEARTBEAT_SECONDS = 20.0


def _apply_no_store_headers(request: Request, response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        response.headers["X-Data-Revision"] = str(event_bus.revision)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/events")
async def stream_events(request: Request) -> StreamingResponse:
    """Server-Sent Events stream notifying browsers immediately when new data is persisted."""
    event_bus = request.app.state.event_bus
    crawl_service = request.app.state.crawl_service

    async def _event_stream() -> AsyncIterator[str]:
        async with event_bus.subscribe() as queue:
            ready_payload = {
                "rev": event_bus.revision,
                "is_crawling": crawl_service.is_crawling,
                "crawling_uid": crawl_service.crawling_uid or "",
            }
            yield (
                "retry: 3000\n"
                "event: ready\n"
                f"data: {json.dumps(ready_payload, ensure_ascii=False)}\n\n"
            )
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        break
                    yield (
                        f"id: {event['rev']}\n"
                        "event: update\n"
                        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    )
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/dashboard", response_model=DashboardSummary)
async def get_dashboard(
    request: Request,
    response: Response,
    hours: int = Query(default=24, ge=0, le=8760, description="Time window in hours; 0 for all"),
    uid: Optional[str] = Query(default=None, description="Optional account UID to inspect"),
    max_points: Optional[int] = Query(
        default=None,
        ge=16,
        le=5000,
        description="Optional chart point cap; returns lightweight downsampled snapshots when set",
    ),
    video_limit: Optional[int] = Query(
        default=None,
        ge=1,
        le=5000,
        description="Optional per-leaderboard cap (returns union of top online and top play videos)",
    ),
    include_accounts: bool = Query(
        default=True,
        description="Whether to include the full accounts summary list in the response",
    ),
) -> DashboardSummary:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service
    target_uid = uid.strip() if uid is not None else None

    _apply_no_store_headers(request, response)

    def _assemble_dashboard() -> DashboardSummary:
        with db.read_session():
            pub_settings = db.get_public_settings(uid=target_uid)
            active_uid = pub_settings["uid"]
            accounts = (
                db.get_accounts_summary(crawling_uid=crawl_service.crawling_uid)
                if include_accounts
                else []
            )
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
            if max_points is not None:
                chart_snaps, snapshots_downsampled, _ = db.get_chart_snapshots(
                    active_uid,
                    since_hours=hours if hours > 0 else None,
                    max_points=max_points,
                )
                snapshots: list[Any] = [ChartSnapshotOut(**s) for s in chart_snaps]
            else:
                snapshots = db.get_snapshots(
                    active_uid, since_hours=hours if hours > 0 else None, limit=2000
                )
                snapshots_downsampled = len(snapshots) >= 2000

            videos, total_videos = db.get_dashboard_videos(
                active_uid, top_limit=video_limit
            )
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
                total_videos=total_videos,
                snapshots=snapshots,
                snapshots_downsampled=snapshots_downsampled,
                top_videos=videos,
                accounts=accounts,
            )

    return await asyncio.to_thread(_assemble_dashboard)


@router.get("/snapshots")
async def get_chart_snapshots(
    request: Request,
    response: Response,
    hours: int = Query(default=24, ge=0, le=8760, description="Time window in hours; 0 for all"),
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
    max_points: Optional[int] = Query(
        default=480, ge=16, le=5000, description="Maximum chart points returned"
    ),
) -> dict[str, Any]:
    """Lightweight overview chart telemetry endpoint for fast time-window switching."""
    _apply_no_store_headers(request, response)
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        target_uid = (uid if uid is not None else db.get_setting("uid", "")).strip()
        if not target_uid:
            return {
                "uid": "",
                "hours": hours,
                "snapshots": [],
                "downsampled": False,
                "total_in_window": 0,
            }
        snapshots, downsampled, total_in_window = db.get_chart_snapshots(
            target_uid,
            since_hours=hours if hours > 0 else None,
            max_points=max_points,
        )
        return {
            "uid": target_uid,
            "hours": hours,
            "snapshots": snapshots,
            "downsampled": downsampled,
            "total_in_window": total_in_window,
        }

    return await asyncio.to_thread(_load)


@router.get("/videos")
async def get_videos(
    request: Request,
    response: Response,
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
    lightweight: bool = Query(
        default=False,
        description="Return dashboard video fields only without parsing full cids arrays",
    ),
) -> dict[str, Any]:
    _apply_no_store_headers(request, response)
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        with db.read_session():
            target_uid = (uid if uid is not None else db.get_setting("uid", "")).strip()
            if not target_uid:
                return {"uid": "", "videos": []}
            if lightweight:
                videos, _ = db.get_dashboard_videos(target_uid, top_limit=None)
                return {"uid": target_uid, "videos": videos}
            return {"uid": target_uid, "videos": db.get_videos(target_uid)}

    return await asyncio.to_thread(_load)


@router.get("/videos/{bvid}/history")
async def get_video_history(
    request: Request,
    response: Response,
    bvid: str,
    hours: Optional[int] = Query(
        default=None, ge=0, le=8760, description="Time window in hours; 0 for all"
    ),
    limit: Optional[int] = Query(default=None, ge=1, le=20000),
    max_points: Optional[int] = Query(default=None, ge=8, le=5000),
) -> dict[str, Any]:
    _apply_no_store_headers(request, response)
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
    response: Response,
    limit: int = Query(default=30, ge=1, le=200),
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
) -> dict[str, Any]:
    _apply_no_store_headers(request, response)
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        target_uid = (uid if uid is not None else db.get_setting("uid", "")).strip()
        if not target_uid:
            return {"uid": "", "logs": []}
        return {"uid": target_uid, "logs": db.get_recent_snapshots(target_uid, limit=limit)}

    return await asyncio.to_thread(_load)
