import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.bilibili.client import BilibiliAPIError, BilibiliClient
from app.database import Database

logger = logging.getLogger(__name__)

VIDEO_SYNC_RULE = "owner_only_v2"
ONLINE_COUNT_RULE = "minus_1_per_cid_v1"

TIERED_VIDEO_THRESHOLD = 80
HOT_NEWEST_LIMIT = 25
HOT_TOP_PLAYS_LIMIT = 40
COLD_POLL_BATCH_SIZE = 30
SHOWCASE_TOP_LIMIT = 50
STATS_REFRESH_QUOTA = 30
STATS_HOT_TTL_SEC = 3600
STATS_COLD_TTL_SEC = 86400
FULL_VIDEO_SYNC_INTERVAL_SEC = 12 * 3600


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


def select_polled_videos(
    videos: list[dict[str, Any]], now_ts: float
) -> tuple[list[dict[str, Any]], set[str], bool]:
    """Select videos to poll for real-time online viewers using Hot + Cold round-robin tiers."""
    if len(videos) <= TIERED_VIDEO_THRESHOLD:
        all_bvids = {v["bvid"] for v in videos}
        return list(videos), all_bvids, False

    active_bvids = {
        v["bvid"]
        for v in videos
        if int(v.get("last_online_count") or 0) > 0 or int(v.get("hot_until_ts") or 0) > now_ts
    }
    newest_bvids = {
        v["bvid"]
        for v in sorted(
            videos,
            key=lambda x: (int(x.get("pubdate") or 0), x["bvid"]),
            reverse=True,
        )[:HOT_NEWEST_LIMIT]
    }
    top_plays_bvids = {
        v["bvid"]
        for v in sorted(
            videos,
            key=lambda x: (
                int(x.get("play_count") or 0),
                int(x.get("pubdate") or 0),
                x["bvid"],
            ),
            reverse=True,
        )[:HOT_TOP_PLAYS_LIMIT]
    }
    hot_bvids = active_bvids | newest_bvids | top_plays_bvids

    hot_videos = sorted(
        (v for v in videos if v["bvid"] in hot_bvids),
        key=lambda x: (
            int(x.get("last_online_count") or 0),
            int(x.get("play_count") or 0),
            int(x.get("pubdate") or 0),
        ),
        reverse=True,
    )
    cold_candidates = sorted(
        (v for v in videos if v["bvid"] not in hot_bvids),
        key=lambda x: (
            0 if _parse_iso_ts(x.get("last_checked_at")) is None else 1,
            _parse_iso_ts(x.get("last_checked_at")) or 0.0,
            -int(x.get("play_count") or 0),
            -int(x.get("pubdate") or 0),
            x["bvid"],
        ),
    )
    polled = hot_videos + cold_candidates[:COLD_POLL_BATCH_SIZE]
    return polled, hot_bvids, True


def select_stats_refresh_bvids(
    videos: list[dict[str, Any]],
    hot_bvids: set[str],
    now_ts: float,
    quota: int = STATS_REFRESH_QUOTA,
) -> list[str]:
    """Select up to `quota` BVids to refresh play/like/coin stats via /x/web-interface/view."""
    if quota <= 0 or not videos:
        return []

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

    unsynced_showcase: list[dict[str, Any]] = []
    unsynced_cold: list[dict[str, Any]] = []
    stale_showcase: list[tuple[float, dict[str, Any]]] = []
    stale_cold: list[tuple[float, dict[str, Any]]] = []

    for v in videos:
        bvid = v["bvid"]
        in_showcase = bvid in showcase_bvids
        synced_ts = _parse_iso_ts(v.get("stats_synced_at"))
        if synced_ts is None:
            if in_showcase:
                unsynced_showcase.append(v)
            else:
                unsynced_cold.append(v)
        else:
            age = now_ts - synced_ts
            if in_showcase and age >= STATS_HOT_TTL_SEC:
                stale_showcase.append((synced_ts, v))
            elif not in_showcase and age >= STATS_COLD_TTL_SEC:
                stale_cold.append((synced_ts, v))

    unsynced_showcase.sort(
        key=lambda x: (
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
    stale_showcase.sort(key=lambda pair: pair[0])
    stale_cold.sort(key=lambda pair: pair[0])

    ordered = (
        [v["bvid"] for v in unsynced_showcase]
        + [v["bvid"] for v in unsynced_cold]
        + [v["bvid"] for _, v in stale_showcase]
        + [v["bvid"] for _, v in stale_cold]
    )
    return ordered[:quota]


def adjust_online_count(raw_count: int) -> int:
    """Subtract 1 baseline viewer reported by Bilibili /x/player/online/total per CID."""
    return max(0, int(raw_count) - 1)


@dataclass
class CrawlContext:
    uid: str
    client: BilibiliClient
    db: Database
    settings: dict[str, str]
    force_sync_videos: bool = False
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    online_viewers_total: Optional[int] = None
    video_count: int = 0
    videos_polled: int = 0
    videos_succeeded: int = 0
    video_online_items: list[dict[str, Any]] = field(default_factory=list)
    extra_metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


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
    """Syncs user's uploaded videos and sums real-time concurrent viewers across all videos."""

    name = "all_videos_online"

    async def collect(self, ctx: CrawlContext) -> None:
        delay_sec = max(0.05, int(ctx.settings.get("request_delay_ms", "200") or 200) / 1000.0)
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
        if not cached_videos:
            ctx.online_viewers_total = 0
            ctx.videos_polled = 0
            ctx.videos_succeeded = 0
            ctx.extra_metrics["total_video_plays"] = 0
            return

        poll_now_ts = time.time()
        polled_videos, hot_bvids, is_tiered = select_polled_videos(cached_videos, poll_now_ts)
        stats_target_order = select_stats_refresh_bvids(
            cached_videos, hot_bvids, poll_now_ts, quota=STATS_REFRESH_QUOTA
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
        ctx.extra_metrics["total_video_plays"] = sum(
            int(v.get("play_count", 0)) for v in cached_videos
        )

        semaphore = asyncio.Semaphore(4)
        abort_412 = asyncio.Event()
        stat_updates: list[dict[str, Any]] = []

        async def fetch_single_video(video: dict[str, Any]) -> Optional[dict[str, Any]]:
            if abort_412.is_set():
                return None
            async with semaphore:
                if abort_412.is_set():
                    return None
                bvid = video["bvid"]

                if bvid in stat_set:
                    try:
                        stat = await ctx.client.get_video_stat(bvid)
                        if _is_valid_stat_payload(stat):
                            play_val = max(
                                0, int(stat.get("play_count", video.get("play_count", 0)))
                            )
                            like_val = max(0, int(stat["like_count"]))
                            coin_val = max(0, int(stat["coin_count"]))
                            video["play_count"] = play_val
                            video["like_count"] = like_val
                            video["coin_count"] = coin_val
                            stat_updates.append(
                                {
                                    "bvid": bvid,
                                    "play_count": play_val,
                                    "like_count": like_val,
                                    "coin_count": coin_val,
                                }
                            )
                            view_cids = stat.get("cids")
                            if not video.get("cids") and isinstance(view_cids, list) and view_cids:
                                video["cids"] = view_cids
                                await asyncio.to_thread(ctx.db.update_video_cids, bvid, view_cids)
                            await asyncio.sleep(delay_sec)
                    except BilibiliAPIError as exc:
                        ctx.errors.append(f"{bvid} 获取点赞投币失败: {exc}")
                        if exc.code == 412:
                            abort_412.set()
                            return None
                    except Exception as exc:
                        ctx.errors.append(f"{bvid} 获取点赞投币失败: {exc}")

                if bvid not in online_set:
                    return None

                cids = video.get("cids") or []
                if not cids:
                    try:
                        cids = await ctx.client.get_video_cids(bvid)
                        if cids:
                            await asyncio.to_thread(ctx.db.update_video_cids, bvid, cids)
                        await asyncio.sleep(delay_sec)
                    except BilibiliAPIError as exc:
                        ctx.errors.append(f"{bvid} 获取CID失败: {exc}")
                        if exc.code == 412:
                            abort_412.set()
                        return None
                    except Exception as exc:
                        ctx.errors.append(f"{bvid} 获取CID失败: {exc}")
                        return None

                if not cids:
                    return None

                video_online = 0
                raw_parts: list[str] = []
                succeeded_parts = 0
                for part in cids:
                    if abort_412.is_set():
                        return None
                    try:
                        count, raw = await ctx.client.get_video_online_total(bvid, int(part["cid"]))
                        video_online += adjust_online_count(count)
                        raw_parts.append(raw)
                        succeeded_parts += 1
                        await asyncio.sleep(delay_sec)
                    except BilibiliAPIError as exc:
                        ctx.errors.append(f"{bvid}(cid={part['cid']}) 在线人数失败: {exc}")
                        if exc.code == 412:
                            abort_412.set()
                            return None
                        continue
                    except Exception as exc:
                        ctx.errors.append(f"{bvid}(cid={part['cid']}) 在线人数失败: {exc}")
                        continue

                if succeeded_parts == 0:
                    return None

                online_raw = raw_parts[0] if len(raw_parts) == 1 else "+".join(raw_parts)
                return {
                    "bvid": bvid,
                    "online_count": video_online,
                    "online_raw": online_raw,
                    "play_count": int(video.get("play_count", 0)),
                }

        results = await asyncio.gather(*(fetch_single_video(v) for v in work_videos))
        if stat_updates:
            await asyncio.to_thread(ctx.db.update_video_stats_batch, stat_updates)
            ctx.extra_metrics["stats_refreshed"] = len(stat_updates)
            ctx.extra_metrics["total_video_plays"] = sum(
                int(v.get("play_count", 0)) for v in cached_videos
            )
        succeeded_items = [item for item in results if item is not None]
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
    """Coordinates modular collectors and persists snapshots safely."""

    def __init__(self, database: Database, collectors: Optional[list[BaseCollector]] = None) -> None:
        self.db = database
        self.collectors: list[BaseCollector] = collectors or [
            AccountFollowerCollector(),
            AllVideosOnlineCollector(),
        ]
        self._lock = asyncio.Lock()
        self.crawling_uid: Optional[str] = None

    @property
    def is_crawling(self) -> bool:
        return self._lock.locked()

    def register_collector(self, collector: BaseCollector) -> None:
        """Register an additional collector for future metric extensions."""
        self.collectors.append(collector)

    async def _crawl_single_uid(
        self, uid: str, force_sync_videos: bool = False
    ) -> dict[str, Any]:
        target_uid = uid.strip()
        if not target_uid:
            return {"skipped": True, "reason": "尚未配置 B站 UID"}

        settings = await asyncio.to_thread(self.db.get_all_settings)
        cookie = await asyncio.to_thread(self.db.get_effective_cookie, target_uid)

        started = time.perf_counter()
        self.crawling_uid = target_uid
        try:
            async with BilibiliClient(cookie=cookie) as client:
                ctx = CrawlContext(
                    uid=target_uid,
                    client=client,
                    db=self.db,
                    settings=settings,
                    force_sync_videos=force_sync_videos,
                )
                for collector in self.collectors:
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

            duration_ms = int((time.perf_counter() - started) * 1000)

            expected_online = ctx.videos_polled if ctx.videos_polled > 0 else ctx.video_count
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
                res = await self._crawl_single_uid(
                    acc["uid"], force_sync_videos=force_sync_videos
                )
                results.append(res)
                if idx < len(accounts) - 1:
                    await asyncio.sleep(0.5)

            return {
                "skipped": False,
                "total": len(accounts),
                "results": results,
            }
