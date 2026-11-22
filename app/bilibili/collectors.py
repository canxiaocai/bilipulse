import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.bilibili.client import BilibiliAPIError, BilibiliClient
from app.database import Database
from app.events import LiveEventBus

logger = logging.getLogger(__name__)

VIDEO_SYNC_RULE = "owner_only_v2"
ONLINE_COUNT_RULE = "single_cid_minus_1_v2"

# Adaptive 5-tier video scheduling levels (lower = higher priority)
TIER_FAST = 0      # T0: newly published (<=24h), high online (>=3), or surging play velocity
TIER_ACTIVE = 1    # T1: active viewers (1-2), recent (24-72h), or moderate play velocity
TIER_WARM = 2      # T2: warm/observation (3-14d, newest floor, or streak < 2)
TIER_SLEEP = 3     # T3: dormant (zero_online_streak 2..5 or low trickle plays)
TIER_FROZEN = 4    # T4: deep-frozen (>14d, zero_online_streak >= 6, near-zero plays)

FAST_PULSE_INTERVAL_MINUTES = 2
FAST_ONLINE_THRESHOLD = 3
FAST_NEW_PUB_WINDOW_SEC = 24 * 3600
ACTIVE_NEW_PUB_WINDOW_SEC = 72 * 3600
WARM_NEW_PUB_WINDOW_SEC = 14 * 86400
WARM_NEWEST_FLOOR = 5
FAST_PLAY_RATE_THRESHOLD = 80.0
ACTIVE_PLAY_RATE_THRESHOLD = 25.0
WARM_PLAY_RATE_THRESHOLD = 5.0
SLEEP_PLAY_RATE_THRESHOLD = 1.0
SLEEP_ZERO_STREAK = 2
FROZEN_ZERO_STREAK = 6

# Effective online polling TTLs (slightly below nominal intervals to avoid scheduler jitter skips)
ONLINE_TTL_BY_TIER: dict[int, int] = {
    TIER_FAST: 105,      # nominal 2 min
    TIER_ACTIVE: 270,    # nominal 5 min
    TIER_WARM: 1740,     # nominal 30 min
    TIER_SLEEP: 21300,   # nominal 6 hours
    TIER_FROZEN: 86100,  # nominal 24 hours
}

# Stats (/x/web-interface/view) refresh TTLs
STATS_ACTIVE_TTL_SEC = 240
STATS_TTL_BY_TIER: dict[int, int] = {
    TIER_FAST: STATS_ACTIVE_TTL_SEC,  # 4 min
    TIER_ACTIVE: 900,                 # 15 min
    TIER_WARM: 7200,                  # 2 hours (when play_count changed)
    TIER_SLEEP: 21600,                # 6 hours (when play_count changed)
    TIER_FROZEN: 43200,               # 12 hours (when play_count changed)
}
STATS_UNCHANGED_TTL_BY_TIER: dict[int, int] = {
    TIER_WARM: 21600,     # 6 hours when play_count unchanged
    TIER_SLEEP: 259200,   # 72 hours when play_count unchanged
    TIER_FROZEN: 604800,  # 7 days when play_count unchanged
}

STATS_BACKOFF_ERROR_CODES = frozenset({-404, 62002, 62004, 62012, -403})
ONLINE_BACKOFF_ERROR_CODES = frozenset({-404, 62002, 62004, 62012})
STATS_BACKOFF_SECONDS = 24 * 3600

# Budgets & sync intervals
TIERED_VIDEO_THRESHOLD = 80
COLD_POLL_BATCH_SIZE = 30
MAX_ONLINE_POLL_PER_ACCOUNT = 150
SHOWCASE_TOP_LIMIT = 50
STATS_REFRESH_QUOTA = 30
FAST_PULSE_MAX_VIDEOS = 100
FAST_PULSE_STATS_QUOTA = 24
FULL_VIDEO_SYNC_INTERVAL_SEC = 6 * 3600


def _parse_iso_ts(raw: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp string into epoch seconds, or None if invalid/empty."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _is_valid_stat_payload(stat: Any) -> bool:
    """Return True if stat is a dict with numeric like_count and coin_count fields."""
    if not isinstance(stat, dict):
        return False
    like_val = stat.get("like_count")
    coin_val = stat.get("coin_count")
    return (
        isinstance(like_val, (int, float))
        and not isinstance(like_val, bool)
        and isinstance(coin_val, (int, float))
        and not isinstance(coin_val, bool)
    )


def _get_newest_floor_bvids(videos: list[dict[str, Any]]) -> set[str]:
    """Return up to WARM_NEWEST_FLOOR most recently published BVids for an account."""
    if not videos:
        return set()
    return {
        v["bvid"]
        for v in sorted(
            videos,
            key=lambda x: (int(x.get("pubdate") or 0), x["bvid"]),
            reverse=True,
        )[:WARM_NEWEST_FLOOR]
    }


def classify_video_tier(
    video: dict[str, Any],
    now_ts: float,
    newest_floor_bvids: Optional[set[str]] = None,
) -> int:
    """Classify a video into adaptive polling tier T0 (fast) .. T4 (frozen)."""
    online = int(video.get("last_online_count") or 0)
    pubdate = int(video.get("pubdate") or 0)
    pub_age = (now_ts - pubdate) if pubdate > 0 else float("inf")
    fast_until = int(video.get("fast_until_ts") or 0)
    hot_until = int(video.get("hot_until_ts") or 0)
    play_rate = float(video.get("play_rate_per_hour") or 0.0)
    zero_streak = int(video.get("zero_online_streak") or 0)

    if (
        pub_age <= FAST_NEW_PUB_WINDOW_SEC
        or online >= FAST_ONLINE_THRESHOLD
        or play_rate >= FAST_PLAY_RATE_THRESHOLD
        or fast_until > now_ts
    ):
        return TIER_FAST

    if (
        pub_age <= ACTIVE_NEW_PUB_WINDOW_SEC
        or online >= 1
        or hot_until > now_ts
        or play_rate >= ACTIVE_PLAY_RATE_THRESHOLD
    ):
        return TIER_ACTIVE

    if (
        pub_age <= WARM_NEW_PUB_WINDOW_SEC
        or play_rate >= WARM_PLAY_RATE_THRESHOLD
        or (newest_floor_bvids is not None and video["bvid"] in newest_floor_bvids)
        or zero_streak < SLEEP_ZERO_STREAK
    ):
        return TIER_WARM

    if zero_streak < FROZEN_ZERO_STREAK or play_rate >= SLEEP_PLAY_RATE_THRESHOLD:
        return TIER_SLEEP

    return TIER_FROZEN


def is_video_online_due(
    video: dict[str, Any],
    tier: int,
    now_ts: float,
    *,
    base_interval_sec: int = 300,
) -> bool:
    """Return True if a video's online viewer count is due for polling under its tier TTL."""
    checked_ts = _parse_iso_ts(video.get("last_checked_at"))
    if checked_ts is None:
        return True

    age = now_ts - checked_ts
    if tier == TIER_FAST:
        ttl = min(ONLINE_TTL_BY_TIER[TIER_FAST], max(45, int(base_interval_sec) - 15))
    elif tier == TIER_ACTIVE:
        ttl = max(45, int(base_interval_sec) - min(30, max(10, int(base_interval_sec) // 10)))
    else:
        ttl = ONLINE_TTL_BY_TIER.get(tier, ONLINE_TTL_BY_TIER[TIER_FROZEN])
    return age >= ttl


def summarize_video_tiers(
    videos: list[dict[str, Any]], now_ts: float
) -> tuple[dict[str, int], dict[str, int]]:
    """Return (tier_by_bvid, tier_counts_dict) for all videos of an account."""
    newest_floor = _get_newest_floor_bvids(videos)
    tier_by_bvid: dict[str, int] = {}
    counts = {"t0": 0, "t1": 0, "t2": 0, "t3": 0, "t4": 0}
    for v in videos:
        tier = classify_video_tier(v, now_ts, newest_floor)
        tier_by_bvid[v["bvid"]] = tier
        counts[f"t{tier}"] += 1
    return tier_by_bvid, counts


def select_polled_videos(
    videos: list[dict[str, Any]],
    now_ts: float,
    *,
    base_interval_sec: int = 300,
    cold_batch_size: int = COLD_POLL_BATCH_SIZE,
    max_total: int = MAX_ONLINE_POLL_PER_ACCOUNT,
    force_active: bool = False,
) -> tuple[list[dict[str, Any]], set[str], bool]:
    """Select due videos to poll for online viewers using 5-tier adaptive scheduling."""
    if not videos:
        return [], set(), False

    tier_by_bvid, _ = summarize_video_tiers(videos, now_ts)
    priority_bvids = {bvid for bvid, tier in tier_by_bvid.items() if tier <= TIER_ACTIVE}

    active_due: list[dict[str, Any]] = []
    background_due: list[dict[str, Any]] = []

    for v in videos:
        bvid = v["bvid"]
        tier = tier_by_bvid[bvid]
        if tier <= TIER_ACTIVE:
            if force_active or is_video_online_due(
                v, tier, now_ts, base_interval_sec=base_interval_sec
            ):
                active_due.append(v)
        else:
            if is_video_online_due(v, tier, now_ts, base_interval_sec=base_interval_sec):
                background_due.append(v)

    active_due.sort(
        key=lambda x: (
            tier_by_bvid[x["bvid"]],
            -int(x.get("last_online_count") or 0),
            -float(x.get("play_rate_per_hour") or 0.0),
            -int(x.get("pubdate") or 0),
            x["bvid"],
        )
    )
    background_due.sort(
        key=lambda x: (
            0 if _parse_iso_ts(x.get("last_checked_at")) is None else 1,
            tier_by_bvid[x["bvid"]],
            _parse_iso_ts(x.get("last_checked_at")) or 0.0,
            -int(x.get("play_count") or 0),
            -int(x.get("pubdate") or 0),
            x["bvid"],
        )
    )

    limited_bg = background_due[: max(0, int(cold_batch_size))]
    polled = (active_due + limited_bg)[: max(0, int(max_total))]
    is_tiered = len(polled) < len(videos)
    return polled, priority_bvids, is_tiered


def select_fast_pulse_videos(
    videos: list[dict[str, Any]],
    now_ts: float,
    *,
    max_videos: int = FAST_PULSE_MAX_VIDEOS,
) -> list[dict[str, Any]]:
    """Select due T0 (fast-tier) videos for the high-frequency 2-minute pulse."""
    if not videos or max_videos <= 0:
        return []
    tier_by_bvid, _ = summarize_video_tiers(videos, now_ts)
    due_fast = [
        v
        for v in videos
        if tier_by_bvid[v["bvid"]] == TIER_FAST
        and is_video_online_due(v, TIER_FAST, now_ts, base_interval_sec=120)
    ]
    due_fast.sort(
        key=lambda x: (
            -int(x.get("last_online_count") or 0),
            -float(x.get("play_rate_per_hour") or 0.0),
            -int(x.get("pubdate") or 0),
            x["bvid"],
        )
    )
    return due_fast[:max_videos]


def select_stats_refresh_bvids(
    videos: list[dict[str, Any]],
    hot_bvids: set[str],
    now_ts: float,
    quota: int = STATS_REFRESH_QUOTA,
    *,
    tiers_filter: Optional[set[int]] = None,
) -> list[str]:
    """Select up to `quota` BVids to refresh play/like/coin stats via /x/web-interface/view."""
    if quota <= 0 or not videos:
        return []

    tier_by_bvid, _ = summarize_video_tiers(videos, now_ts)

    top_online = {
        v["bvid"]
        for v in sorted(
            videos,
            key=lambda x: (int(x.get("last_online_count") or 0), int(x.get("play_count") or 0)),
            reverse=True,
        )[:SHOWCASE_TOP_LIMIT]
    }
    top_plays = {
        v["bvid"]
        for v in sorted(
            videos,
            key=lambda x: (int(x.get("play_count") or 0), int(x.get("pubdate") or 0)),
            reverse=True,
        )[:SHOWCASE_TOP_LIMIT]
    }
    showcase_bvids = hot_bvids | top_online | top_plays

    unsynced_priority: list[dict[str, Any]] = []
    unsynced_cold: list[dict[str, Any]] = []
    stale_fast: list[tuple[int, int, float, int, float, dict[str, Any]]] = []
    stale_active: list[tuple[int, int, float, int, float, dict[str, Any]]] = []
    stale_warm: list[tuple[int, float, dict[str, Any]]] = []
    stale_dormant: list[tuple[int, int, float, dict[str, Any]]] = []

    for v in videos:
        bvid = v["bvid"]
        tier = tier_by_bvid[bvid]
        if tiers_filter is not None and tier not in tiers_filter:
            continue
        if int(v.get("stats_backoff_until_ts") or 0) > now_ts:
            continue

        in_showcase = bvid in showcase_bvids
        online_val = int(v.get("last_online_count") or 0)
        pubdate_val = int(v.get("pubdate") or 0)
        play_rate = float(v.get("play_rate_per_hour") or 0.0)
        synced_ts = _parse_iso_ts(v.get("stats_synced_at"))

        if synced_ts is None:
            if tier <= TIER_ACTIVE or in_showcase:
                unsynced_priority.append(v)
            else:
                unsynced_cold.append(v)
            continue

        age = now_ts - synced_ts
        if tier == TIER_FAST:
            if age >= STATS_TTL_BY_TIER[TIER_FAST]:
                stale_fast.append(
                    (1 if online_val > 0 else 0, online_val, play_rate, pubdate_val, -synced_ts, v)
                )
        elif tier == TIER_ACTIVE:
            if age >= STATS_TTL_BY_TIER[TIER_ACTIVE]:
                stale_active.append(
                    (1 if online_val > 0 else 0, online_val, play_rate, pubdate_val, -synced_ts, v)
                )
        else:
            stats_play = v.get("stats_play_count")
            cur_play = int(v.get("play_count") or 0)
            play_changed = stats_play is None or cur_play != int(stats_play)
            effective_ttl = (
                STATS_TTL_BY_TIER[tier]
                if play_changed
                else STATS_UNCHANGED_TTL_BY_TIER[tier]
            )
            if age >= effective_ttl:
                if tier == TIER_WARM:
                    stale_warm.append((0 if play_changed else 1, synced_ts, v))
                else:
                    stale_dormant.append((0 if play_changed else 1, tier, synced_ts, v))

    unsynced_priority.sort(
        key=lambda x: (
            -tier_by_bvid[x["bvid"]],
            1 if int(x.get("last_online_count") or 0) > 0 else 0,
            int(x.get("last_online_count") or 0),
            int(x.get("play_count") or 0),
            int(x.get("pubdate") or 0),
        ),
        reverse=True,
    )
    unsynced_cold.sort(
        key=lambda x: (int(x.get("play_count") or 0), int(x.get("pubdate") or 0)),
        reverse=True,
    )
    stale_fast.sort(
        key=lambda item: (item[0], item[1], item[2], item[3], item[4]),
        reverse=True,
    )
    stale_active.sort(
        key=lambda item: (item[0], item[1], item[2], item[3], item[4]),
        reverse=True,
    )
    stale_warm.sort(key=lambda item: (item[0], item[1]))
    stale_dormant.sort(key=lambda item: (item[0], item[1], item[2]))

    ordered = (
        [v["bvid"] for v in unsynced_priority]
        + [v["bvid"] for *_, v in stale_fast]
        + [v["bvid"] for *_, v in stale_active]
        + [v["bvid"] for v in unsynced_cold]
        + [v["bvid"] for _, _, v in stale_warm]
        + [v["bvid"] for _, _, _, v in stale_dormant]
    )
    return ordered[:quota]


def adjust_online_count(raw_count: int) -> int:
    """Subtract 1 baseline viewer reported by Bilibili /x/player/online/total for a video."""
    return max(0, int(raw_count) - 1)


async def _fetch_video_batch(
    *,
    client: BilibiliClient,
    db: Database,
    work_videos: list[dict[str, Any]],
    online_set: set[str],
    stat_set: set[str],
    delay_sec: float,
    abort_412: Optional[asyncio.Event] = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[str],
    list[str],
    list[str],
    bool,
]:
    """Fetch stats (/view) and real-time online counts for a batch of videos with concurrency control."""
    if not work_videos:
        return [], [], [], [], [], False

    semaphore = asyncio.Semaphore(4)
    local_abort = abort_412 if abort_412 is not None else asyncio.Event()
    stat_updates: list[dict[str, Any]] = []
    stats_backoff_bvids: list[str] = []
    online_backoff_bvids: list[str] = []
    errors: list[str] = []

    async def fetch_single_video(video: dict[str, Any]) -> Optional[dict[str, Any]]:
        if local_abort.is_set():
            return None
        async with semaphore:
            if local_abort.is_set():
                return None
            bvid = video["bvid"]

            if bvid in stat_set:
                try:
                    stat = await client.get_video_stat(bvid)
                    if _is_valid_stat_payload(stat):
                        play_val = max(
                            0, int(stat.get("play_count", video.get("play_count", 0)))
                        )
                        like_val = max(0, int(stat["like_count"]))
                        coin_val = max(0, int(stat["coin_count"]))
                        video["play_count"] = max(int(video.get("play_count") or 0), play_val)
                        video["like_count"] = like_val
                        video["coin_count"] = coin_val
                        video["stats_synced_at"] = "synced"
                        stat_updates.append(
                            {
                                "bvid": bvid,
                                "play_count": video["play_count"],
                                "like_count": like_val,
                                "coin_count": coin_val,
                            }
                        )
                        view_cids = stat.get("cids")
                        if not video.get("cids") and isinstance(view_cids, list) and view_cids:
                            video["cids"] = view_cids
                            await asyncio.to_thread(db.update_video_cids, bvid, view_cids)
                        await asyncio.sleep(delay_sec)
                except BilibiliAPIError as exc:
                    errors.append(f"{bvid} 获取点赞投币失败: {exc}")
                    if exc.code == 412:
                        local_abort.set()
                        return None
                    if exc.code in STATS_BACKOFF_ERROR_CODES:
                        stats_backoff_bvids.append(bvid)
                except Exception as exc:
                    errors.append(f"{bvid} 获取点赞投币失败: {exc}")

            if bvid not in online_set:
                return None

            cids = video.get("cids") or []
            if not cids:
                try:
                    cids = await client.get_video_cids(bvid)
                    if cids:
                        await asyncio.to_thread(db.update_video_cids, bvid, cids)
                    await asyncio.sleep(delay_sec)
                except BilibiliAPIError as exc:
                    errors.append(f"{bvid} 获取CID失败: {exc}")
                    if exc.code == 412:
                        local_abort.set()
                    elif exc.code in ONLINE_BACKOFF_ERROR_CODES:
                        online_backoff_bvids.append(bvid)
                    return None
                except Exception as exc:
                    errors.append(f"{bvid} 获取CID失败: {exc}")
                    return None

            if not cids:
                return None

            # Bilibili /x/player/online/total returns whole-video (BV-level) concurrent viewers
            # in `data.total` across all parts, so query the first CID and only fall back to
            # subsequent CIDs if an earlier CID fails.
            video_online: Optional[int] = None
            online_raw = ""
            hit_missing_code = False
            for part in cids:
                if local_abort.is_set():
                    return None
                try:
                    count, raw = await client.get_video_online_total(bvid, int(part["cid"]))
                    video_online = adjust_online_count(count)
                    online_raw = raw
                    await asyncio.sleep(delay_sec)
                    break
                except BilibiliAPIError as exc:
                    errors.append(f"{bvid}(cid={part['cid']}) 在线人数失败: {exc}")
                    if exc.code == 412:
                        local_abort.set()
                        return None
                    if exc.code in ONLINE_BACKOFF_ERROR_CODES:
                        hit_missing_code = True
                    continue
                except Exception as exc:
                    errors.append(f"{bvid}(cid={part['cid']}) 在线人数失败: {exc}")
                    continue

            if video_online is None:
                if hit_missing_code:
                    online_backoff_bvids.append(bvid)
                return None
            raw_like = video.get("like_count")
            has_like_stat = bool(video.get("stats_synced_at")) or (
                raw_like is not None and int(raw_like) > 0
            )
            return {
                "bvid": bvid,
                "online_count": video_online,
                "online_raw": online_raw,
                "play_count": int(video.get("play_count", 0)),
                "like_count": int(raw_like or 0) if has_like_stat else None,
            }

    results = await asyncio.gather(*(fetch_single_video(v) for v in work_videos))
    succeeded_items = [item for item in results if item is not None]
    return (
        succeeded_items,
        stat_updates,
        stats_backoff_bvids,
        online_backoff_bvids,
        errors,
        local_abort.is_set(),
    )


@dataclass
class CrawlContext:
    uid: str
    client: BilibiliClient
    db: Database
    settings: dict[str, str]
    force_sync_videos: bool = False
    track_deletion: bool = False
    aborted_deleted: bool = False
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    online_viewers_total: Optional[int] = None
    video_count: int = 0
    videos_polled: int = 0
    videos_succeeded: int = 0
    online_poll_planned: bool = False
    video_online_items: list[dict[str, Any]] = field(default_factory=list)
    extra_metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    async def check_deleted(self) -> bool:
        """Return True if this monitored account was deleted while the crawl is in flight."""
        if not self.track_deletion:
            return False
        if self.aborted_deleted:
            return True
        acc = await asyncio.to_thread(self.db.get_account, self.uid)
        if acc is None:
            self.aborted_deleted = True
            return True
        return False


class BaseCollector(ABC):
    """Extensible collector interface for adding new Bilibili metrics."""

    name: str = "base"

    @abstractmethod
    async def collect(self, ctx: CrawlContext) -> None:
        """Populate metrics on CrawlContext."""


class AccountFollowerCollector(BaseCollector):
    """Collects follower count, following count, and profile metadata."""

    name = "account_follower"

    async def collect(self, ctx: CrawlContext) -> None:
        stat = await ctx.client.get_relation_stat(ctx.uid)
        ctx.follower_count = stat["follower"]
        ctx.following_count = stat["following"]

        try:
            card = await ctx.client.get_account_card(ctx.uid)
            if await ctx.check_deleted():
                return
            if card.get("name") or card.get("face"):
                await asyncio.to_thread(
                    ctx.db.update_account_profile,
                    ctx.uid,
                    account_name=card.get("name") or None,
                    account_face=card.get("face") or None,
                )
            ctx.extra_metrics["archive_count_reported"] = card.get("archive_count", 0)
        except Exception as exc:
            logger.warning("Failed to fetch account card for %s: %s", ctx.uid, exc)


class AllVideosOnlineCollector(BaseCollector):
    """Syncs user's uploaded videos and polls real-time concurrent viewers via 5-tier adaptive scheduling."""

    name = "all_videos_online"

    async def collect(self, ctx: CrawlContext) -> None:
        delay_sec = max(0.05, int(ctx.settings.get("request_delay_ms", "200") or 200) / 1000.0)
        base_interval_sec = max(
            60, int(ctx.settings.get("interval_minutes", "5") or 5) * 60
        )
        sync_interval_sec = (
            max(5, int(ctx.settings.get("video_sync_interval_minutes", "30") or 30)) * 60
        )

        cached_videos = await asyncio.to_thread(ctx.db.get_videos, ctx.uid)
        now_ts = time.time()
        last_sync_ts = int(ctx.settings.get(f"last_video_sync_ts_{ctx.uid}", "0") or 0)
        rule_matches = ctx.settings.get(f"video_sync_rule_{ctx.uid}", "") == VIDEO_SYNC_RULE
        reported_count = int(ctx.extra_metrics.get("archive_count_reported", 0) or 0)
        last_reported_count = int(ctx.settings.get(f"last_archive_count_{ctx.uid}", "0") or 0)

        needs_sync = (
            ctx.force_sync_videos
            or not cached_videos
            or not rule_matches
            or (now_ts - last_sync_ts) >= sync_interval_sec
            or (
                reported_count > 0
                and last_reported_count > 0
                and reported_count != last_reported_count
            )
        )

        if needs_sync:
            raw_full_ts = ctx.settings.get(f"last_full_video_sync_ts_{ctx.uid}")
            last_full_sync_ts = int(
                raw_full_ts if raw_full_ts is not None else (last_sync_ts if rule_matches else 0)
            )
            count_decreased = (
                reported_count > 0
                and last_reported_count > 0
                and reported_count < last_reported_count
            )
            needs_full_sync = (
                ctx.force_sync_videos
                or not cached_videos
                or not rule_matches
                or len(cached_videos) <= TIERED_VIDEO_THRESHOLD
                or count_decreased
                or (now_ts - last_full_sync_ts) >= FULL_VIDEO_SYNC_INTERVAL_SEC
            )
            try:
                if needs_full_sync:
                    fetched_videos = await ctx.client.get_all_videos(ctx.uid)
                else:
                    try:
                        fetched_videos = await ctx.client.get_all_videos(ctx.uid, max_pages=1)
                    except TypeError:
                        fetched_videos = await ctx.client.get_all_videos(ctx.uid)
                        needs_full_sync = True

                target_uid = str(ctx.uid).strip()
                owned_videos = [
                    v
                    for v in fetched_videos
                    if not (v.get("up_mid") and str(v["up_mid"]).strip() != target_uid)
                ]
                if await ctx.check_deleted():
                    return
                if owned_videos:
                    await asyncio.to_thread(ctx.db.upsert_videos, ctx.uid, owned_videos)
                if needs_full_sync:
                    active_bvids = {v["bvid"] for v in owned_videos}
                    await asyncio.to_thread(
                        ctx.db.delete_stale_videos, ctx.uid, active_bvids, allow_empty=True
                    )
                sync_now_str = str(int(time.time()))
                sync_updates: dict[str, Any] = {
                    f"last_video_sync_ts_{ctx.uid}": sync_now_str,
                    f"video_sync_rule_{ctx.uid}": VIDEO_SYNC_RULE,
                }
                if needs_full_sync:
                    sync_updates[f"last_full_video_sync_ts_{ctx.uid}"] = sync_now_str
                if (
                    "archive_count_reported" in ctx.extra_metrics
                    or not ctx.settings.get(f"last_archive_count_{ctx.uid}")
                ):
                    sync_updates[f"last_archive_count_{ctx.uid}"] = str(reported_count)
                await asyncio.to_thread(ctx.db.update_settings, sync_updates)
                ctx.settings.update({k: str(v) for k, v in sync_updates.items()})
                cached_videos = await asyncio.to_thread(ctx.db.get_videos, ctx.uid)
            except Exception as exc:
                msg = f"同步视频列表失败: {exc}"
                logger.warning(msg)
                ctx.errors.append(msg)
                if not cached_videos:
                    raise

        ctx.video_count = len(cached_videos)
        ctx.online_poll_planned = True
        if not cached_videos:
            ctx.online_viewers_total = 0
            ctx.videos_polled = 0
            ctx.videos_succeeded = 0
            ctx.extra_metrics["total_video_plays"] = 0
            return

        poll_now_ts = time.time()
        _, tier_counts = summarize_video_tiers(cached_videos, poll_now_ts)
        polled_videos, priority_bvids, is_tiered = select_polled_videos(
            cached_videos,
            poll_now_ts,
            base_interval_sec=base_interval_sec,
            force_active=ctx.force_sync_videos,
        )
        stats_target_order = select_stats_refresh_bvids(
            cached_videos, priority_bvids, poll_now_ts, quota=STATS_REFRESH_QUOTA
        )

        online_set = {v["bvid"] for v in polled_videos}
        stat_set = set(stats_target_order)
        by_bvid = {v["bvid"]: v for v in cached_videos}

        work_videos: list[dict[str, Any]] = list(polled_videos)
        for bvid in stats_target_order:
            if bvid not in online_set and bvid in by_bvid:
                work_videos.append(by_bvid[bvid])

        ctx.videos_polled = len(polled_videos)
        ctx.extra_metrics["videos_polled"] = len(polled_videos)
        ctx.extra_metrics["tiered_polling"] = is_tiered
        ctx.extra_metrics["tier_counts"] = tier_counts
        ctx.extra_metrics["total_video_plays"] = sum(
            int(v.get("play_count", 0)) for v in cached_videos
        )

        (
            succeeded_items,
            stat_updates,
            stats_backoff_bvids,
            online_backoff_bvids,
            batch_errors,
            _,
        ) = await _fetch_video_batch(
            client=ctx.client,
            db=ctx.db,
            work_videos=work_videos,
            online_set=online_set,
            stat_set=stat_set,
            delay_sec=delay_sec,
        )
        ctx.errors.extend(batch_errors)

        if await ctx.check_deleted():
            return
        if stat_updates:
            await asyncio.to_thread(ctx.db.update_video_stats_batch, stat_updates)
            ctx.extra_metrics["stats_refreshed"] = len(stat_updates)
            ctx.extra_metrics["total_video_plays"] = sum(
                int(v.get("play_count", 0)) for v in cached_videos
            )
        if stats_backoff_bvids:
            backoff_until = int(time.time()) + STATS_BACKOFF_SECONDS
            await asyncio.to_thread(
                ctx.db.set_video_stats_backoff_batch, stats_backoff_bvids, backoff_until
            )
        if online_backoff_bvids:
            await asyncio.to_thread(ctx.db.set_video_online_backoff_batch, online_backoff_bvids)

        unpolled_carryover = sum(
            max(0, int(v.get("last_online_count") or 0))
            for v in cached_videos
            if v["bvid"] not in online_set
        )
        ctx.video_online_items = succeeded_items
        ctx.videos_succeeded = len(succeeded_items)
        ctx.online_viewers_total = (
            sum(item["online_count"] for item in succeeded_items) + unpolled_carryover
        )


class CrawlService:
    """Coordinates modular collectors, fast-tier pulses, and persists snapshots safely."""

    def __init__(
        self,
        database: Database,
        collectors: Optional[list[BaseCollector]] = None,
        event_bus: Optional[LiveEventBus] = None,
    ) -> None:
        self.db = database
        self.collectors: list[BaseCollector] = collectors or [
            AccountFollowerCollector(),
            AllVideosOnlineCollector(),
        ]
        self.event_bus = event_bus
        self._lock = asyncio.Lock()
        self.crawling_uid: Optional[str] = None

    def _emit(self, kind: str, *, uid: Optional[str] = None, **extra: Any) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(kind, uid=uid, **extra)

    @property
    def is_crawling(self) -> bool:
        return self._lock.locked()

    def register_collector(self, collector: BaseCollector) -> None:
        """Register an additional collector for future metric extensions."""
        self.collectors.append(collector)

    async def _is_uid_still_enabled(self, uid: str) -> bool:
        """Verify that a scheduled UID has not been deleted or disabled while a batch crawl is running."""
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return False
        enabled_accounts = await asyncio.to_thread(self.db.get_enabled_accounts)
        return any(acc["uid"] == clean_uid for acc in enabled_accounts)

    async def _crawl_single_uid(
        self, uid: str, force_sync_videos: bool = False
    ) -> dict[str, Any]:
        target_uid = uid.strip()
        if not target_uid:
            return {"skipped": True, "reason": "尚未配置 B站 UID"}

        settings = await asyncio.to_thread(self.db.get_all_settings)
        cookie = await asyncio.to_thread(self.db.get_effective_cookie, target_uid)
        was_monitored = (await asyncio.to_thread(self.db.get_account, target_uid)) is not None

        started = time.perf_counter()
        self.crawling_uid = target_uid
        self._emit("crawl_started", uid=target_uid)
        snapshot_emitted = False
        try:
            async with BilibiliClient(cookie=cookie) as client:
                ctx = CrawlContext(
                    uid=target_uid,
                    client=client,
                    db=self.db,
                    settings=settings,
                    force_sync_videos=force_sync_videos,
                    track_deletion=was_monitored,
                )
                for collector in self.collectors:
                    if await ctx.check_deleted():
                        break
                    try:
                        await collector.collect(ctx)
                    except BilibiliAPIError as exc:
                        err = f"[{collector.name}] {exc}"
                        logger.error(err)
                        ctx.errors.append(err)
                    except Exception as exc:
                        err = f"[{collector.name}] 未预期异常: {exc}"
                        logger.exception(err)
                        ctx.errors.append(err)

            if await ctx.check_deleted():
                logger.info("Skipping snapshot persistence for deleted UID %s", target_uid)
                return {"skipped": True, "uid": target_uid, "reason": "账号已被删除"}

            duration_ms = int((time.perf_counter() - started) * 1000)

            expected_online = (
                ctx.videos_polled if ctx.online_poll_planned else ctx.video_count
            )
            if ctx.follower_count is None and ctx.online_viewers_total is None:
                status = "error"
            elif ctx.errors or (expected_online > 0 and ctx.videos_succeeded < expected_online):
                status = "partial"
            else:
                status = "success"

            error_message = " | ".join(ctx.errors[:5]) if ctx.errors else None
            if ctx.errors and len(ctx.errors) > 5:
                error_message = f"{error_message} (共 {len(ctx.errors)} 条警告)"

            snapshot_id = await asyncio.to_thread(
                self.db.insert_snapshot,
                uid=target_uid,
                follower_count=ctx.follower_count,
                following_count=ctx.following_count,
                online_viewers_total=ctx.online_viewers_total,
                video_count=ctx.video_count,
                videos_succeeded=ctx.videos_succeeded,
                extra_metrics=ctx.extra_metrics,
                status=status,
                error_message=error_message,
                duration_ms=duration_ms,
                video_online_items=ctx.video_online_items,
            )

            self.crawling_uid = None
            snapshot_emitted = True
            self._emit(
                "snapshot",
                uid=target_uid,
                snapshot_id=snapshot_id,
                status=status,
            )

            return {
                "skipped": False,
                "snapshot_id": snapshot_id,
                "uid": target_uid,
                "status": status,
                "follower_count": ctx.follower_count,
                "online_viewers_total": ctx.online_viewers_total,
                "video_count": ctx.video_count,
                "videos_succeeded": ctx.videos_succeeded,
                "duration_ms": duration_ms,
                "error_message": error_message,
            }
        finally:
            self.crawling_uid = None
            if not snapshot_emitted:
                self._emit("crawl_finished", uid=target_uid)

    async def fetch_account_profile(self, uid: str, cookie: Optional[str] = None) -> None:
        """Best-effort fetch of account name and avatar without blocking on the crawl lock."""
        target_uid = uid.strip()
        if not target_uid:
            return
        clean_cookie = (cookie or "").strip()
        if not clean_cookie:
            clean_cookie = await asyncio.to_thread(self.db.get_effective_cookie, target_uid)
        try:
            async with BilibiliClient(cookie=clean_cookie) as client:
                card = await client.get_account_card(target_uid)
            if card.get("name") or card.get("face"):
                await asyncio.to_thread(
                    self.db.update_account_profile,
                    target_uid,
                    account_name=card.get("name") or None,
                    account_face=card.get("face") or None,
                )
                self._emit("account_profile", uid=target_uid)
        except Exception as exc:
            logger.warning("Failed to prefetch account profile for %s: %s", target_uid, exc)

    async def run_crawl(
        self,
        force_sync_videos: bool = False,
        uid: Optional[str] = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        if not wait and self._lock.locked():
            return {"skipped": True, "reason": "已有采集任务正在进行中"}

        async with self._lock:
            target_uid = (
                uid.strip()
                if uid is not None
                else (await asyncio.to_thread(self.db.get_setting, "uid", "")).strip()
            )
            if not target_uid:
                return {"skipped": True, "reason": "尚未配置 B站 UID"}
            if wait and uid is not None:
                acc = await asyncio.to_thread(self.db.get_account, target_uid)
                if acc is None or not acc.get("enabled"):
                    return {"skipped": True, "reason": "账号已删除或已暂停"}
            return await self._crawl_single_uid(target_uid, force_sync_videos=force_sync_videos)

    async def run_crawl_all(self, force_sync_videos: bool = False) -> dict[str, Any]:
        if self._lock.locked():
            return {"skipped": True, "reason": "已有采集任务正在进行中"}

        async with self._lock:
            accounts = await asyncio.to_thread(self.db.get_enabled_accounts)
            if not accounts:
                return {"skipped": True, "reason": "没有启用的监控账号"}

            results: list[dict[str, Any]] = []
            for idx, acc in enumerate(accounts):
                if not await self._is_uid_still_enabled(acc["uid"]):
                    logger.info(
                        "Skipping UID %s during batch crawl (deleted or paused)",
                        acc["uid"],
                    )
                    continue
                res = await self._crawl_single_uid(
                    acc["uid"], force_sync_videos=force_sync_videos
                )
                if not res.get("skipped"):
                    results.append(res)
                if idx < len(accounts) - 1:
                    await asyncio.sleep(0.5)

            return {
                "skipped": False,
                "total": len(results),
                "results": results,
            }

    async def run_fast_pulse(
        self,
        *,
        max_videos: int = FAST_PULSE_MAX_VIDEOS,
        stats_quota: int = FAST_PULSE_STATS_QUOTA,
    ) -> dict[str, Any]:
        """Run a lightweight 2-minute high-frequency pulse for due T0 videos across enabled accounts."""
        if self._lock.locked():
            return {"skipped": True, "reason": "已有采集任务正在进行中"}

        async with self._lock:
            accounts = await asyncio.to_thread(self.db.get_enabled_accounts)
            if not accounts:
                return {"skipped": True, "reason": "没有启用的监控账号"}

            settings = await asyncio.to_thread(self.db.get_all_settings)
            delay_sec = max(
                0.05, int(settings.get("request_delay_ms", "200") or 200) / 1000.0
            )
            started = time.perf_counter()
            now_ts = time.time()

            remaining_online = max(0, int(max_videos))
            remaining_stats = max(0, int(stats_quota))
            accounts_touched = 0
            total_polled = 0
            total_succeeded = 0
            total_stats = 0
            aborted_412 = False
            global_abort = asyncio.Event()

            try:
                for acc in accounts:
                    if remaining_online <= 0 or global_abort.is_set():
                        break

                    uid = acc["uid"]
                    if not await self._is_uid_still_enabled(uid):
                        continue
                    was_monitored = (await asyncio.to_thread(self.db.get_account, uid)) is not None
                    cached_videos = await asyncio.to_thread(self.db.get_videos, uid)
                    if not cached_videos:
                        continue

                    polled_videos = select_fast_pulse_videos(
                        cached_videos, now_ts, max_videos=remaining_online
                    )
                    if not polled_videos:
                        continue

                    fast_bvids = {v["bvid"] for v in polled_videos}
                    stats_bvids = (
                        select_stats_refresh_bvids(
                            cached_videos,
                            fast_bvids,
                            now_ts,
                            quota=remaining_stats,
                            tiers_filter={TIER_FAST},
                        )
                        if remaining_stats > 0
                        else []
                    )
                    stat_set = set(stats_bvids)
                    by_bvid = {v["bvid"]: v for v in cached_videos}
                    work_videos: list[dict[str, Any]] = list(polled_videos)
                    for bvid in stats_bvids:
                        if bvid not in fast_bvids and bvid in by_bvid:
                            work_videos.append(by_bvid[bvid])

                    cookie = await asyncio.to_thread(self.db.get_effective_cookie, uid)
                    self.crawling_uid = uid
                    self._emit("crawl_started", uid=uid, pulse=True)
                    async with BilibiliClient(cookie=cookie) as client:
                        (
                            succeeded_items,
                            stat_updates,
                            stats_backoff_bvids,
                            online_backoff_bvids,
                            batch_errors,
                            hit_412,
                        ) = await _fetch_video_batch(
                            client=client,
                            db=self.db,
                            work_videos=work_videos,
                            online_set=fast_bvids,
                            stat_set=stat_set,
                            delay_sec=delay_sec,
                            abort_412=global_abort,
                        )

                    if was_monitored and await asyncio.to_thread(self.db.get_account, uid) is None:
                        logger.info("Skipping fast pulse persistence for deleted UID %s", uid)
                        self.crawling_uid = None
                        self._emit("crawl_finished", uid=uid, pulse=True)
                        continue

                    if stat_updates:
                        await asyncio.to_thread(self.db.update_video_stats_batch, stat_updates)
                        total_stats += len(stat_updates)
                        remaining_stats = max(0, remaining_stats - len(stat_updates))
                    if stats_backoff_bvids:
                        backoff_until = int(time.time()) + STATS_BACKOFF_SECONDS
                        await asyncio.to_thread(
                            self.db.set_video_stats_backoff_batch,
                            stats_backoff_bvids,
                            backoff_until,
                        )
                    if online_backoff_bvids:
                        await asyncio.to_thread(
                            self.db.set_video_online_backoff_batch, online_backoff_bvids
                        )
                    if succeeded_items:
                        await asyncio.to_thread(
                            self.db.insert_video_pulse, uid, succeeded_items
                        )

                    if batch_errors:
                        logger.warning(
                            "Fast pulse for UID %s had %d error(s): %s",
                            uid,
                            len(batch_errors),
                            batch_errors[0],
                        )

                    self.crawling_uid = None
                    if succeeded_items or stat_updates:
                        self._emit(
                            "fast_pulse",
                            uid=uid,
                            videos_succeeded=len(succeeded_items),
                            stats_refreshed=len(stat_updates),
                        )
                    else:
                        self._emit("crawl_finished", uid=uid, pulse=True)

                    accounts_touched += 1
                    total_polled += len(polled_videos)
                    total_succeeded += len(succeeded_items)
                    remaining_online = max(0, remaining_online - len(polled_videos))
                    if hit_412:
                        aborted_412 = True
                        break
            finally:
                if self.crawling_uid is not None:
                    finished_uid = self.crawling_uid
                    self.crawling_uid = None
                    self._emit("crawl_finished", uid=finished_uid, pulse=True)

            duration_ms = int((time.perf_counter() - started) * 1000)
            if total_polled > 0:
                logger.info(
                    "Fast pulse finished: accounts=%d videos=%d/%d stats=%d duration=%dms aborted_412=%s",
                    accounts_touched,
                    total_succeeded,
                    total_polled,
                    total_stats,
                    duration_ms,
                    aborted_412,
                )
            return {
                "skipped": False,
                "accounts_touched": accounts_touched,
                "videos_polled": total_polled,
                "videos_succeeded": total_succeeded,
                "stats_refreshed": total_stats,
                "aborted_412": aborted_412,
                "duration_ms": duration_ms,
            }
