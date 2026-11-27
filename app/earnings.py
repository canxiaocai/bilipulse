from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any, Optional

from app.config import get_earnings_owner_uid

BJT_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class EarningsTier:
    index: int
    label: str
    play_start: int
    play_end: int
    cpm: float
    min_avg_likes: int
    max_segment_payout: float


@dataclass(frozen=True)
class EarningsCampaign:
    id: str
    name: str
    short_name: str
    tag: str
    start_date: str
    end_date: str
    start_ts: int
    end_ts: int
    min_video_play: int
    min_qualified_videos: int
    top_likes_count: int
    max_payout: float
    max_billable_play: int
    tiers: tuple[EarningsTier, ...]
    include_viral_bonus: bool = False


def _bjt_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BJT_TZ).timestamp())


PERIOD_1_TIERS: tuple[EarningsTier, ...] = (
    EarningsTier(
        index=1,
        label="第一档",
        play_start=0,
        play_end=200_000,
        cpm=18.0,
        min_avg_likes=1000,
        max_segment_payout=3600.0,
    ),
    EarningsTier(
        index=2,
        label="第二档",
        play_start=200_000,
        play_end=500_000,
        cpm=15.0,
        min_avg_likes=3000,
        max_segment_payout=4500.0,
    ),
    EarningsTier(
        index=3,
        label="第三档",
        play_start=500_000,
        play_end=800_000,
        cpm=10.0,
        min_avg_likes=8000,
        max_segment_payout=3000.0,
    ),
)

EARNINGS_CAMPAIGNS: tuple[EarningsCampaign, ...] = (
    EarningsCampaign(
        id="2026-p1",
        name="第一期 · 崩坏：因缘精灵创作者激励计划",
        short_name="第一期",
        tag="崩坏：因缘精灵创作者激励计划",
        start_date="2026-11-27",
        end_date="2026-12-15",
        start_ts=_bjt_ts(2026, 11, 27, 0, 0, 0),
        end_ts=_bjt_ts(2026, 12, 15, 23, 59, 59),
        min_video_play=3000,
        min_qualified_videos=8,
        top_likes_count=10,
        max_payout=11100.0,
        max_billable_play=800_000,
        tiers=PERIOD_1_TIERS,
        include_viral_bonus=False,
    ),
)

DEFAULT_CAMPAIGN_ID = EARNINGS_CAMPAIGNS[0].id


def is_earnings_owner_uid(uid: Optional[str]) -> bool:
    clean = str(uid or "").strip()
    owner = get_earnings_owner_uid()
    return bool(clean and owner and clean == owner)


def get_campaign(period_id: Optional[str] = None) -> Optional[EarningsCampaign]:
    target = (period_id or "").strip() or DEFAULT_CAMPAIGN_ID
    for campaign in EARNINGS_CAMPAIGNS:
        if campaign.id == target:
            return campaign
    return None


def list_campaign_summaries(now_ts: Optional[int] = None) -> list[dict[str, Any]]:
    ts = int(now_ts if now_ts is not None else time.time())
    items: list[dict[str, Any]] = []
    for c in EARNINGS_CAMPAIGNS:
        if ts < c.start_ts:
            status = "upcoming"
        elif ts <= c.end_ts:
            status = "active"
        else:
            status = "ended"
        items.append(
            {
                "id": c.id,
                "name": c.name,
                "short_name": c.short_name,
                "tag": c.tag,
                "start_date": c.start_date,
                "end_date": c.end_date,
                "status": status,
            }
        )
    return items


def get_active_campaign_window(now_ts: Optional[int] = None) -> Optional[tuple[int, int]]:
    """Return (min_start_ts, max_end_ts) across campaigns that are active or recently ended within 24h."""
    ts = int(now_ts if now_ts is not None else time.time())
    relevant = [c for c in EARNINGS_CAMPAIGNS if c.start_ts <= ts <= c.end_ts + 86400]
    if not relevant:
        return None
    return min(c.start_ts for c in relevant), max(c.end_ts for c in relevant)


def parse_video_tags(raw_tags: Any) -> list[str]:
    """Normalize a video's stored tags (JSON string or list) into deduplicated non-empty tag names."""
    if isinstance(raw_tags, list):
        items = raw_tags
    elif isinstance(raw_tags, str):
        text = raw_tags.strip()
        if not text or text == "[]":
            return []
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    else:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("tag_name") or item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def compute_tiered_payout(
    campaign: EarningsCampaign,
    total_play: int,
    *,
    max_play_cap: Optional[int] = None,
) -> tuple[float, list[dict[str, Any]]]:
    """Compute progressive tiered CPM payout and per-tier breakdown for total_play."""
    effective_cap = (
        campaign.max_billable_play
        if max_play_cap is None
        else max(0, min(campaign.max_billable_play, int(max_play_cap)))
    )
    billable_total = min(max(0, int(total_play)), effective_cap)
    total_payout = 0.0
    breakdown: list[dict[str, Any]] = []

    for tier in campaign.tiers:
        seg_capacity = max(0, tier.play_end - tier.play_start)
        seg_plays = max(0, min(billable_total, tier.play_end) - tier.play_start)
        seg_payout = round((seg_plays / 1000.0) * tier.cpm, 2)
        total_payout += seg_payout
        breakdown.append(
            {
                "index": tier.index,
                "label": tier.label,
                "play_start": tier.play_start,
                "play_end": tier.play_end,
                "cpm": tier.cpm,
                "min_avg_likes": tier.min_avg_likes,
                "max_segment_payout": tier.max_segment_payout,
                "counted_play": seg_plays,
                "segment_capacity": seg_capacity,
                "progress_pct": round((seg_plays / seg_capacity) * 100.0, 2)
                if seg_capacity > 0
                else 0.0,
                "payout": seg_payout,
            }
        )

    capped_payout = round(min(campaign.max_payout, total_payout), 2)
    return capped_payout, breakdown


def compute_campaign_earnings(
    campaign: EarningsCampaign,
    videos: list[dict[str, Any]],
    *,
    uid: str,
    now_ts: Optional[int] = None,
    frozen: bool = False,
    frozen_at: Optional[str] = None,
) -> dict[str, Any]:
    """Evaluate real-time earnings, tier unlock status, and per-video breakdown for a campaign."""
    ts = int(now_ts if now_ts is not None else time.time())
    if ts < campaign.start_ts:
        period_status = "upcoming"
        remaining_seconds = max(0, campaign.end_ts - campaign.start_ts)
    elif ts <= campaign.end_ts:
        period_status = "active"
        remaining_seconds = max(0, campaign.end_ts - ts)
    else:
        period_status = "ended"
        remaining_seconds = 0

    period_videos: list[dict[str, Any]] = []
    tags_unsynced_count = 0
    matched_videos: list[dict[str, Any]] = []
    target_tag = campaign.tag.strip()

    for raw in videos:
        pubdate = int(raw.get("pubdate") or 0)
        if pubdate <= 0 or pubdate < campaign.start_ts or pubdate > campaign.end_ts:
            continue
        period_videos.append(raw)
        if not raw.get("tags_synced_at"):
            tags_unsynced_count += 1
        tags = parse_video_tags(raw.get("tags") if "tags" in raw else raw.get("tags_json"))
        if target_tag in tags:
            matched_videos.append({**raw, "_parsed_tags": tags})

    qualified_raw: list[dict[str, Any]] = []
    pending_raw: list[dict[str, Any]] = []
    for item in matched_videos:
        play_val = max(0, int(item.get("play_count") or 0))
        if play_val >= campaign.min_video_play:
            qualified_raw.append(item)
        else:
            pending_raw.append(item)

    qualified_count = len(qualified_raw)
    pending_count = len(pending_raw)
    matched_count = len(matched_videos)

    total_play = sum(max(0, int(v.get("play_count") or 0)) for v in qualified_raw)
    total_like = sum(max(0, int(v.get("like_count") or 0)) for v in qualified_raw)
    total_reply = sum(max(0, int(v.get("reply_count") or 0)) for v in qualified_raw)
    all_matched_play = sum(max(0, int(v.get("play_count") or 0)) for v in matched_videos)

    top_liked_sorted = sorted(
        qualified_raw,
        key=lambda v: (
            max(0, int(v.get("like_count") or 0)),
            max(0, int(v.get("play_count") or 0)),
            str(v.get("bvid") or ""),
        ),
        reverse=True,
    )[: campaign.top_likes_count]
    top_liked_bvids = {str(v.get("bvid") or "") for v in top_liked_sorted}
    top_likes_sample_size = len(top_liked_sorted)
    avg_top10_likes = (
        round(
            sum(max(0, int(v.get("like_count") or 0)) for v in top_liked_sorted)
            / top_likes_sample_size,
            1,
        )
        if top_likes_sample_size > 0
        else 0.0
    )

    engagement_rate = (
        round(((total_like + total_reply) / total_play) * 100.0, 2)
        if total_play > 0
        else 0.0
    )
    reply_to_like_rate = (
        round((total_reply / total_like) * 100.0, 2) if total_like > 0 else 0.0
    )

    estimated_payout, tier_breakdown = compute_tiered_payout(
        campaign, total_play, max_play_cap=campaign.max_billable_play
    )

    unlocked_tier_index = 0
    unlocked_play_cap = 0
    unlocked_payout_cap = 0.0
    next_likes_target: Optional[int] = None

    for tier in campaign.tiers:
        if avg_top10_likes >= tier.min_avg_likes:
            unlocked_tier_index = tier.index
            unlocked_play_cap = tier.play_end
            unlocked_payout_cap = round(unlocked_payout_cap + tier.max_segment_payout, 2)
        elif next_likes_target is None:
            next_likes_target = tier.min_avg_likes

    for tier_item in tier_breakdown:
        tier_item["likes_unlocked"] = avg_top10_likes >= tier_item["min_avg_likes"]
        tier_item["fully_unlocked"] = (
            qualified_count >= campaign.min_qualified_videos
            and tier_item["likes_unlocked"]
        )

    meets_video_count = qualified_count >= campaign.min_qualified_videos
    meets_min_avg_likes = unlocked_play_cap > 0

    if meets_video_count and meets_min_avg_likes:
        settled_payout, _ = compute_tiered_payout(
            campaign, total_play, max_play_cap=unlocked_play_cap
        )
    else:
        settled_payout = 0.0

    current_cpm = 0.0
    for tier in campaign.tiers:
        if total_play < tier.play_end:
            current_cpm = tier.cpm
            break

    blockers: list[str] = []
    videos_needed = max(0, campaign.min_qualified_videos - qualified_count)
    if videos_needed > 0:
        blockers.append(f"还差 {videos_needed} 条有效稿件（需 ≥{campaign.min_video_play:,} 播放）")
    if not meets_min_avg_likes:
        need_likes = max(0.0, round(campaign.tiers[0].min_avg_likes - avg_top10_likes, 1))
        blockers.append(f"TOP10 均赞需达 {campaign.tiers[0].min_avg_likes:,}（当前差 {need_likes:g}）")
    elif total_play > unlocked_play_cap and next_likes_target is not None:
        need_likes = max(0.0, round(next_likes_target - avg_top10_likes, 1))
        blockers.append(
            f"当前播放已超第 {unlocked_tier_index} 档上限（{unlocked_play_cap:,}），均赞达 {next_likes_target:,} 可解锁下一档（差 {need_likes:g}）"
        )

    sorted_matched = sorted(
        matched_videos,
        key=lambda v: (
            1 if int(v.get("play_count") or 0) >= campaign.min_video_play else 0,
            int(v.get("play_count") or 0),
            int(v.get("pubdate") or 0),
            str(v.get("bvid") or ""),
        ),
        reverse=True,
    )

    video_rows: list[dict[str, Any]] = []
    for item in sorted_matched:
        bvid = str(item.get("bvid") or "")
        play_val = max(0, int(item.get("play_count") or 0))
        like_val = max(0, int(item.get("like_count") or 0))
        reply_val = max(0, int(item.get("reply_count") or 0))
        coin_val = max(0, int(item.get("coin_count") or 0))
        is_qualified = play_val >= campaign.min_video_play
        plays_to_qualify = max(0, campaign.min_video_play - play_val)
        v_engagement = (
            round(((like_val + reply_val) / play_val) * 100.0, 2) if play_val > 0 else 0.0
        )
        allocated_est = (
            round(estimated_payout * (play_val / total_play), 2)
            if (is_qualified and total_play > 0)
            else 0.0
        )
        video_rows.append(
            {
                "bvid": bvid,
                "title": str(item.get("title") or ""),
                "pic": str(item.get("pic") or ""),
                "pubdate": int(item.get("pubdate") or 0),
                "duration": int(item.get("duration") or 0),
                "play_count": play_val,
                "like_count": like_val,
                "reply_count": reply_val,
                "coin_count": coin_val,
                "qualified": is_qualified,
                "plays_to_qualify": plays_to_qualify,
                "in_top10_likes": bvid in top_liked_bvids,
                "engagement_rate": v_engagement,
                "allocated_estimated_payout": allocated_est,
                "stats_synced_at": item.get("stats_synced_at"),
                "tags_synced_at": item.get("tags_synced_at"),
            }
        )

    return {
        "uid": uid,
        "period": {
            "id": campaign.id,
            "name": campaign.name,
            "short_name": campaign.short_name,
            "tag": campaign.tag,
            "start_date": campaign.start_date,
            "end_date": campaign.end_date,
            "start_ts": campaign.start_ts,
            "end_ts": campaign.end_ts,
            "status": period_status,
            "remaining_seconds": remaining_seconds,
            "frozen": frozen,
            "frozen_at": frozen_at,
        },
        "periods": list_campaign_summaries(now_ts=ts),
        "rules": {
            "min_video_play": campaign.min_video_play,
            "min_qualified_videos": campaign.min_qualified_videos,
            "top_likes_count": campaign.top_likes_count,
            "max_payout": campaign.max_payout,
            "max_billable_play": campaign.max_billable_play,
            "include_viral_bonus": campaign.include_viral_bonus,
        },
        "summary": {
            "period_total_videos": len(period_videos),
            "tags_unsynced_count": tags_unsynced_count,
            "matched_video_count": matched_count,
            "qualified_video_count": qualified_count,
            "pending_video_count": pending_count,
            "videos_needed": videos_needed,
            "meets_video_count": meets_video_count,
            "total_play": total_play,
            "billable_play": min(total_play, campaign.max_billable_play),
            "all_matched_play": all_matched_play,
            "total_like": total_like,
            "total_reply": total_reply,
            "avg_top10_likes": avg_top10_likes,
            "top_likes_sample_size": top_likes_sample_size,
            "engagement_rate": engagement_rate,
            "reply_to_like_rate": reply_to_like_rate,
            "current_cpm": current_cpm,
            "estimated_payout": estimated_payout,
            "settled_payout": settled_payout,
            "unlocked_tier_index": unlocked_tier_index,
            "unlocked_play_cap": unlocked_play_cap,
            "unlocked_payout_cap": unlocked_payout_cap,
            "next_likes_target": next_likes_target,
            "meets_min_avg_likes": meets_min_avg_likes,
            "is_settled_ready": meets_video_count and meets_min_avg_likes,
            "payout_progress_pct": round((estimated_payout / campaign.max_payout) * 100.0, 2)
            if campaign.max_payout > 0
            else 0.0,
            "blockers": blockers,
        },
        "tiers": tier_breakdown,
        "videos": video_rows,
        "computed_at": datetime.fromtimestamp(ts, tz=BJT_TZ).isoformat(),
    }
