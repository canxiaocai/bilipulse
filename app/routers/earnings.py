import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.auth import refresh_admin_session_cookie_if_needed, require_admin
from app.bilibili.client import BilibiliClient
from app.config import get_earnings_owner_uid
from app.earnings import (
    DEFAULT_CAMPAIGN_ID,
    compute_campaign_earnings,
    get_campaign,
    is_earnings_owner_uid,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/earnings", tags=["earnings"])

_ON_DEMAND_TAG_SYNC_CAP = 30


async def _sync_period_videos_and_tags(
    request: Request,
    *,
    uid: str,
    start_ts: int,
    end_ts: int,
    sync_video_list: bool,
    only_missing_tags: bool,
) -> dict[str, Any]:
    """Fetch fresh video list (optional) and tag/stat details for videos in [start_ts, end_ts]."""
    db = request.app.state.db
    cookie = await asyncio.to_thread(db.get_effective_cookie, uid)
    delay_ms = int(await asyncio.to_thread(db.get_setting, "request_delay_ms", "200") or 200)
    delay_sec = max(0.05, delay_ms / 1000.0)

    synced_count = 0
    errors: list[str] = []

    async with BilibiliClient(cookie=cookie) as client:
        if sync_video_list:
            try:
                fetched = await client.get_all_videos(uid, max_pages=1)
                owned = [
                    v
                    for v in fetched
                    if not (v.get("up_mid") and str(v["up_mid"]).strip() != uid)
                ]
                if owned:
                    await asyncio.to_thread(db.upsert_videos, uid, owned)
            except Exception as exc:
                logger.warning("Earnings video list sync failed for %s: %s", uid, exc)
                errors.append(f"同步最新投稿列表失败: {exc}")

        videos = await asyncio.to_thread(db.get_earnings_videos, uid, start_ts, end_ts)
        targets = (
            [v for v in videos if not v.get("tags_synced_at")]
            if only_missing_tags
            else list(videos)
        )[:_ON_DEMAND_TAG_SYNC_CAP]

        stat_updates: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(3)

        async def _fetch_one(video: dict[str, Any]) -> None:
            nonlocal synced_count
            bvid = video["bvid"]
            async with sem:
                try:
                    stat = await client.get_video_stat_with_tags(bvid)
                    stat_updates.append(
                        {
                            "bvid": bvid,
                            "play_count": max(
                                int(video.get("play_count") or 0),
                                int(stat.get("play_count") or 0),
                            ),
                            "like_count": max(0, int(stat.get("like_count") or 0)),
                            "coin_count": max(0, int(stat.get("coin_count") or 0)),
                            "reply_count": max(0, int(stat.get("reply_count") or 0)),
                            "tid": max(0, int(stat.get("tid") or 0)),
                            "tid_v2": max(0, int(stat.get("tid_v2") or 0)),
                            "tags": stat.get("tags") if isinstance(stat.get("tags"), list) else [],
                        }
                    )
                    synced_count += 1
                    await asyncio.sleep(delay_sec)
                except Exception as exc:
                    logger.warning("Earnings tag/stat fetch failed for %s: %s", bvid, exc)
                    errors.append(f"{bvid}: {exc}")

        if targets:
            await asyncio.gather(*(_fetch_one(v) for v in targets))

        if stat_updates:
            await asyncio.to_thread(db.update_video_stats_batch, stat_updates)
            event_bus = getattr(request.app.state, "event_bus", None)
            if event_bus is not None:
                event_bus.publish("earnings_synced", uid=uid)

    return {"synced_count": synced_count, "errors": errors}


def _resolve_owner_and_campaign(
    uid: Optional[str], period: Optional[str]
) -> tuple[str, Any]:
    owner_uid = get_earnings_owner_uid()
    target_uid = (uid or "").strip() or owner_uid
    if not is_earnings_owner_uid(target_uid):
        raise HTTPException(status_code=404, detail="个人收益统计仅对指定主账号开放")
    campaign = get_campaign(period or DEFAULT_CAMPAIGN_ID)
    if campaign is None:
        raise HTTPException(status_code=404, detail="未找到对应的活动期次")
    return target_uid, campaign


@router.get("", dependencies=[Depends(require_admin)])
async def get_personal_earnings(
    request: Request,
    response: Response,
    uid: Optional[str] = Query(default=None, description="Target Bilibili UID (owner only)"),
    period: str = Query(default=DEFAULT_CAMPAIGN_ID, description="Campaign period ID"),
    sync_missing: bool = Query(
        default=True,
        description="Automatically fetch tags for unsynced videos in the active period",
    ),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    await asyncio.to_thread(refresh_admin_session_cookie_if_needed, request, response, db)

    target_uid, campaign = _resolve_owner_and_campaign(uid, period)
    now_ts = int(time.time())

    if now_ts > campaign.end_ts:
        frozen_payload = await asyncio.to_thread(
            db.get_earnings_frozen_snapshot, target_uid, campaign.id
        )
        if frozen_payload is not None:
            return frozen_payload

    sync_info = {"synced_count": 0, "errors": []}
    if sync_missing and now_ts <= campaign.end_ts + 86400:
        videos_initial = await asyncio.to_thread(
            db.get_earnings_videos, target_uid, campaign.start_ts, campaign.end_ts
        )
        if any(not v.get("tags_synced_at") for v in videos_initial):
            sync_info = await _sync_period_videos_and_tags(
                request,
                uid=target_uid,
                start_ts=campaign.start_ts,
                end_ts=campaign.end_ts,
                sync_video_list=False,
                only_missing_tags=True,
            )

    videos = await asyncio.to_thread(
        db.get_earnings_videos, target_uid, campaign.start_ts, campaign.end_ts
    )
    payload = compute_campaign_earnings(
        campaign, videos, uid=target_uid, now_ts=now_ts
    )
    payload["sync_info"] = sync_info

    if now_ts > campaign.end_ts:
        frozen_at = await asyncio.to_thread(
            db.save_earnings_frozen_snapshot, target_uid, campaign.id, payload
        )
        payload["period"]["frozen"] = True
        payload["period"]["frozen_at"] = frozen_at

    return payload


@router.post("/refresh", dependencies=[Depends(require_admin)])
async def refresh_personal_earnings(
    request: Request,
    response: Response,
    uid: Optional[str] = Query(default=None, description="Target Bilibili UID (owner only)"),
    period: str = Query(default=DEFAULT_CAMPAIGN_ID, description="Campaign period ID"),
    force: bool = Query(
        default=False,
        description="Re-sync even if the period has ended and was previously frozen",
    ),
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    await asyncio.to_thread(refresh_admin_session_cookie_if_needed, request, response, db)

    target_uid, campaign = _resolve_owner_and_campaign(uid, period)
    now_ts = int(time.time())

    if now_ts > campaign.end_ts and not force:
        frozen_payload = await asyncio.to_thread(
            db.get_earnings_frozen_snapshot, target_uid, campaign.id
        )
        if frozen_payload is not None:
            frozen_payload["sync_info"] = {"synced_count": 0, "errors": [], "skipped_frozen": True}
            return frozen_payload

    sync_info = await _sync_period_videos_and_tags(
        request,
        uid=target_uid,
        start_ts=campaign.start_ts,
        end_ts=campaign.end_ts,
        sync_video_list=True,
        only_missing_tags=False,
    )
    videos = await asyncio.to_thread(
        db.get_earnings_videos, target_uid, campaign.start_ts, campaign.end_ts
    )
    payload = compute_campaign_earnings(
        campaign, videos, uid=target_uid, now_ts=now_ts
    )
    payload["sync_info"] = sync_info

    if now_ts > campaign.end_ts:
        frozen_at = await asyncio.to_thread(
            db.save_earnings_frozen_snapshot, target_uid, campaign.id, payload
        )
        payload["period"]["frozen"] = True
        payload["period"]["frozen_at"] = frozen_at

    return payload
