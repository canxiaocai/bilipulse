import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.auth import hash_password
from app.bilibili.client import parse_online_count
from app.bilibili.zones import resolve_video_zone
from app.config import DB_PATH, DEFAULT_SETTINGS, get_default_admin_password

ONLINE_COUNT_RULE = "single_cid_minus_1_v2"
LEGACY_ONLINE_COUNT_RULE_V1 = "minus_1_per_cid_v1"
HOT_STICKY_SECONDS = 900
FAST_STICKY_SECONDS = 900
FAST_ONLINE_THRESHOLD = 3
FAST_PLAY_RATE_THRESHOLD = 80.0
ACTIVE_PLAY_RATE_THRESHOLD = 25.0
PLAY_RATE_MIN_INTERVAL_SEC = 180
PLAY_RATE_SETTLE_INTERVAL_SEC = 600
SEVEN_DAY_WINDOW_SECONDS = 7 * 86400
SEVEN_DAY_BRACKET_TOLERANCE_SECONDS = 12 * 3600
SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS = 6 * 3600
BJT_TZ = timezone(timedelta(hours=8))
_SQLITE_CHUNK_SIZE = 500


def _settle_play_count_7d(
    *,
    target_ts: int,
    pre_ts: Optional[int],
    pre_play: Optional[int],
    post_ts: Optional[int],
    post_play: Optional[int],
) -> tuple[Optional[int], int, int]:
    """Resolve and lock the 7-day post-publication play count from bounding observations."""
    has_pre = pre_ts is not None and pre_play is not None and pre_ts <= target_ts
    has_post = post_ts is not None and post_play is not None and post_ts >= target_ts

    if has_pre and has_post:
        assert pre_ts is not None and pre_play is not None
        assert post_ts is not None and post_play is not None
        gap_before = target_ts - pre_ts
        gap_after = post_ts - target_ts
        p_before = max(0, int(pre_play))
        p_after = max(p_before, int(post_play))
        if pre_ts == post_ts:
            return (p_after, target_ts, 1)
        if (
            gap_before <= SEVEN_DAY_BRACKET_TOLERANCE_SECONDS
            and gap_after <= SEVEN_DAY_BRACKET_TOLERANCE_SECONDS
        ):
            ratio = gap_before / (post_ts - pre_ts)
            interp = int(round(p_before + (p_after - p_before) * ratio))
            return (interp, target_ts, 1)
        if gap_before <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS:
            return (p_before, pre_ts, 1)
        if gap_after <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS:
            return (p_after, post_ts, 1)
        return (None, 0, 1)

    if has_pre:
        assert pre_ts is not None and pre_play is not None
        gap_before = target_ts - pre_ts
        if gap_before <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS:
            return (max(0, int(pre_play)), pre_ts, 1)
        return (None, 0, 1)

    if has_post:
        assert post_ts is not None and post_play is not None
        gap_after = post_ts - target_ts
        if gap_after <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS:
            return (max(0, int(post_play)), post_ts, 1)
        return (None, 0, 1)

    return (None, 0, 1)


def _compute_play_count_7d_update(
    *,
    pubdate: int,
    obs_play: int,
    obs_ts: int,
    old_play_7d: Optional[int],
    old_play_7d_ts: int,
    old_locked: int,
) -> tuple[Optional[int], int, int]:
    """Compute updated (play_count_7d, play_count_7d_ts, play_count_7d_locked) for a fresh play observation."""
    if old_locked:
        return (
            int(old_play_7d) if old_play_7d is not None else None,
            int(old_play_7d_ts or 0),
            1,
        )

    pub_ts = int(pubdate or 0)
    now_ts = int(obs_ts or 0)
    play_val = max(0, int(obs_play))
    if pub_ts <= 0 or now_ts <= 0:
        return (
            int(old_play_7d) if old_play_7d is not None else None,
            int(old_play_7d_ts or 0),
            0,
        )

    target_ts = pub_ts + SEVEN_DAY_WINDOW_SECONDS
    prev_ts = int(old_play_7d_ts or 0)
    prev_play = int(old_play_7d) if old_play_7d is not None else None

    if now_ts < target_ts:
        next_play = max(prev_play, play_val) if prev_play is not None else play_val
        next_ts = max(prev_ts, now_ts)
        return (next_play, next_ts, 0)

    valid_pre = prev_play is not None and 0 < prev_ts <= target_ts
    return _settle_play_count_7d(
        target_ts=target_ts,
        pre_ts=prev_ts if valid_pre else None,
        pre_play=prev_play if valid_pre else None,
        post_ts=now_ts,
        post_play=play_val,
    )


def _resolve_video_play_count_7d_view(
    *,
    pubdate: int,
    play_count: int,
    stored_play_7d: Optional[int],
    stored_play_7d_ts: int,
    stored_locked: int,
    now_ts: int,
) -> tuple[Optional[int], str]:
    """Return (display_play_count_7d, status) where status is 'tracking' | 'locked' | 'unavailable'."""
    if stored_locked:
        if stored_play_7d is not None:
            return (max(0, int(stored_play_7d)), "locked")
        return (None, "unavailable")

    pub_ts = int(pubdate or 0)
    if pub_ts <= 0:
        return (None, "unavailable")

    target_ts = pub_ts + SEVEN_DAY_WINDOW_SECONDS
    if now_ts < target_ts:
        cur_play = max(0, int(play_count or 0))
        if stored_play_7d is not None:
            cur_play = max(cur_play, max(0, int(stored_play_7d)))
        return (cur_play, "tracking")

    prev_ts = int(stored_play_7d_ts or 0)
    if (
        stored_play_7d is not None
        and 0 < prev_ts <= target_ts
        and (target_ts - prev_ts) <= SEVEN_DAY_BRACKET_TOLERANCE_SECONDS
    ):
        return (max(0, int(stored_play_7d)), "locked")
    return (None, "unavailable")


def _compute_play_rate_update(
    *,
    old_play: int,
    new_play: int,
    old_play_ts: int,
    now_ts: int,
    old_rate: float,
    old_fast_until: int,
    old_hot_until: int,
    old_zero_streak: int,
) -> tuple[int, float, int, int, int, int]:
    """Compute monotonic play_count, smoothed play_rate_per_hour, and surge wake-up state."""
    effective_play = max(max(0, int(old_play)), max(0, int(new_play)))
    if old_play_ts <= 0:
        return (
            effective_play,
            max(0.0, float(old_rate or 0.0)),
            int(now_ts),
            int(old_fast_until or 0),
            int(old_hot_until or 0),
            max(0, int(old_zero_streak or 0)),
        )

    dt = int(now_ts) - int(old_play_ts)
    delta = max(0, effective_play - max(0, int(old_play)))
    if dt < PLAY_RATE_MIN_INTERVAL_SEC or (delta < 15 and dt < PLAY_RATE_SETTLE_INTERVAL_SEC):
        return (
            effective_play,
            max(0.0, float(old_rate or 0.0)),
            int(old_play_ts),
            int(old_fast_until or 0),
            int(old_hot_until or 0),
            max(0, int(old_zero_streak or 0)),
        )

    instant_rate = round(delta * 3600.0 / dt, 2)
    prev_rate = max(0.0, float(old_rate or 0.0))
    if instant_rate >= prev_rate or dt >= 900:
        new_rate = instant_rate
    else:
        new_rate = round(prev_rate * 0.5 + instant_rate * 0.5, 2)

    new_fast = int(old_fast_until or 0)
    new_hot = int(old_hot_until or 0)
    new_streak = max(0, int(old_zero_streak or 0))
    if delta >= 20 and instant_rate >= FAST_PLAY_RATE_THRESHOLD:
        new_fast = max(new_fast, int(now_ts) + FAST_STICKY_SECONDS)
        new_hot = max(new_hot, int(now_ts) + HOT_STICKY_SECONDS)
        new_streak = 0
    elif delta >= 15 and instant_rate >= ACTIVE_PLAY_RATE_THRESHOLD:
        new_hot = max(new_hot, int(now_ts) + HOT_STICKY_SECONDS)
        new_streak = 0

    return (effective_play, new_rate, int(now_ts), new_fast, new_hot, new_streak)


def _split_legacy_online_raw(raw: str) -> list[str]:
    """Split legacy '+'-joined per-CID online_raw strings while preserving trailing '+' suffixes (e.g. '1000+')."""
    text = (raw or "").strip()
    if not text:
        return []
    parts: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("+", i)
        if j == -1:
            tok = text[i:].strip()
            if tok:
                parts.append(tok)
            break
        if j + 1 == n or text[j + 1] == "+":
            tok = text[i : j + 1].strip()
            if tok:
                parts.append(tok)
            i = j + 2
        else:
            tok = text[i:j].strip()
            if tok:
                parts.append(tok)
            i = j + 1
    return parts


def _adjusted_online_from_raw(
    raw: str, fallback_count: int, *, already_minus_one: bool = False
) -> tuple[int, str]:
    """Compute single-video adjusted online count (-1 baseline, floored at 0) and normalized raw text."""
    parts = _split_legacy_online_raw(raw)
    if not parts:
        count = max(0, int(fallback_count)) if already_minus_one else max(0, int(fallback_count) - 1)
        return count, (raw or "").strip()
    first_raw = parts[0]
    return max(0, parse_online_count(first_raw) - 1), first_raw


def _mask_cookie(cookie: str) -> str:
    if not cookie:
        return ""
    if len(cookie) <= 16:
        return "*" * len(cookie)
    return f"{cookie[:8]}...{cookie[-6:]}"


def _parse_retention_days(raw_val: Optional[str], default: int = 90) -> int:
    text = (raw_val or "").strip()
    if not text:
        return default
    try:
        return max(0, int(text))
    except ValueError:
        return default


class Database:
    """SQLite persistence layer for settings, video cache, and crawl snapshots."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path or DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_version = 0
        self._write_version_lock = threading.Lock()
        self.init_schema()

    @property
    def write_version(self) -> int:
        return self._write_version

    def _bump_write_version(self) -> None:
        with self._write_version_lock:
            self._write_version += 1

    def _open_read_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA cache_size=-4000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA query_only=ON;")
        return conn

    @contextmanager
    def read_session(self) -> Iterator[sqlite3.Connection]:
        """Share a single read-only SQLite connection across multiple read queries on the current thread."""
        existing: Optional[sqlite3.Connection] = getattr(self._local, "read_conn", None)
        if existing is not None:
            yield existing
            return
        conn = self._open_read_connection()
        self._local.read_conn = conn
        try:
            yield conn
        finally:
            self._local.read_conn = None
            conn.close()

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Reuse the active thread-local read session if present, else open a lightweight read-only connection."""
        existing: Optional[sqlite3.Connection] = getattr(self._local, "read_conn", None)
        if existing is not None:
            yield existing
            return
        conn = self._open_read_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        conn.execute("PRAGMA cache_size=-4000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        try:
            yield conn
            conn.commit()
            self._bump_write_version()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    collected_at_ts INTEGER NOT NULL,
                    follower_count INTEGER,
                    following_count INTEGER,
                    online_viewers_total INTEGER,
                    video_count INTEGER NOT NULL DEFAULT 0,
                    videos_succeeded INTEGER NOT NULL DEFAULT 0,
                    extra_metrics TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'success',
                    error_message TEXT,
                    duration_ms INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_uid_ts_cover
                    ON account_snapshots (
                        uid, collected_at_ts DESC, id DESC,
                        follower_count, online_viewers_total, collected_at, status, video_count
                    );

                CREATE TABLE IF NOT EXISTS videos (
                    bvid TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    aid INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    pic TEXT NOT NULL DEFAULT '',
                    pubdate INTEGER NOT NULL DEFAULT 0,
                    duration INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    play_count_7d INTEGER,
                    play_count_7d_ts INTEGER NOT NULL DEFAULT 0,
                    play_count_7d_locked INTEGER NOT NULL DEFAULT 0,
                    like_count INTEGER NOT NULL DEFAULT 0,
                    coin_count INTEGER NOT NULL DEFAULT 0,
                    tid INTEGER NOT NULL DEFAULT 0,
                    tid_v2 INTEGER NOT NULL DEFAULT 0,
                    zone_synced INTEGER NOT NULL DEFAULT 1,
                    stats_synced_at TEXT,
                    stats_play_count INTEGER,
                    stats_backoff_until_ts INTEGER NOT NULL DEFAULT 0,
                    online_backoff_until_ts INTEGER NOT NULL DEFAULT 0,
                    cids_json TEXT NOT NULL DEFAULT '[]',
                    last_online_count INTEGER NOT NULL DEFAULT 0,
                    last_online_raw TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT,
                    hot_until_ts INTEGER NOT NULL DEFAULT 0,
                    fast_until_ts INTEGER NOT NULL DEFAULT 0,
                    zero_online_streak INTEGER NOT NULL DEFAULT 0,
                    play_rate_per_hour REAL NOT NULL DEFAULT 0.0,
                    last_play_ts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_videos_uid_pubdate
                    ON videos (uid, pubdate DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_uid_online_pubdate
                    ON videos (uid, last_online_count DESC, pubdate DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_uid_online_play
                    ON videos (uid, last_online_count DESC, play_count DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_uid_play_online
                    ON videos (uid, play_count DESC, last_online_count DESC);

                CREATE TABLE IF NOT EXISTS video_online_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
                    bvid TEXT NOT NULL,
                    collected_at_ts INTEGER,
                    online_count INTEGER NOT NULL,
                    online_raw TEXT NOT NULL DEFAULT '',
                    play_count INTEGER,
                    like_count INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_video_snapshots_snapshot
                    ON video_online_snapshots (snapshot_id);

                CREATE TABLE IF NOT EXISTS video_pulse_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL,
                    bvid TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    collected_at_ts INTEGER NOT NULL,
                    online_count INTEGER NOT NULL,
                    online_raw TEXT NOT NULL DEFAULT '',
                    play_count INTEGER,
                    like_count INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_video_pulse_uid_ts
                    ON video_pulse_snapshots (uid, collected_at_ts DESC);

                CREATE TABLE IF NOT EXISTS monitored_accounts (
                    uid TEXT PRIMARY KEY,
                    account_name TEXT NOT NULL DEFAULT '',
                    account_face TEXT NOT NULL DEFAULT '',
                    cookie TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Drop redundant prefix indexes superseded by idx_snapshots_uid_ts_cover
            conn.execute("DROP INDEX IF EXISTS idx_snapshots_uid_ts;")
            conn.execute("DROP INDEX IF EXISTS idx_snapshots_uid_ts_id;")

            video_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(videos)").fetchall()
            }
            if "like_count" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN like_count INTEGER NOT NULL DEFAULT 0")
            if "coin_count" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN coin_count INTEGER NOT NULL DEFAULT 0")
            if "stats_synced_at" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN stats_synced_at TEXT")
            if "stats_play_count" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN stats_play_count INTEGER")
                conn.execute(
                    "UPDATE videos SET stats_play_count = play_count WHERE stats_synced_at IS NOT NULL"
                )
            if "stats_backoff_until_ts" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN stats_backoff_until_ts INTEGER NOT NULL DEFAULT 0"
                )
            if "online_backoff_until_ts" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN online_backoff_until_ts INTEGER NOT NULL DEFAULT 0"
                )
            if "hot_until_ts" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN hot_until_ts INTEGER NOT NULL DEFAULT 0")
            if "fast_until_ts" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN fast_until_ts INTEGER NOT NULL DEFAULT 0")
            if "zero_online_streak" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN zero_online_streak INTEGER NOT NULL DEFAULT 0"
                )
                dormant_cutoff = int(time.time()) - 14 * 86400
                conn.execute(
                    """
                    UPDATE videos
                    SET zero_online_streak = 2
                    WHERE last_checked_at IS NOT NULL
                      AND last_online_count = 0
                      AND hot_until_ts = 0
                      AND pubdate > 0
                      AND pubdate < ?
                    """,
                    (dormant_cutoff,),
                )
            if "play_rate_per_hour" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN play_rate_per_hour REAL NOT NULL DEFAULT 0.0"
                )
            if "last_play_ts" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN last_play_ts INTEGER NOT NULL DEFAULT 0")
                now_epoch = int(time.time())
                conn.execute(
                    "UPDATE videos SET last_play_ts = ? WHERE last_play_ts = 0 AND play_count > 0",
                    (now_epoch,),
                )
            if "play_count_7d" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN play_count_7d INTEGER")
            if "play_count_7d_ts" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN play_count_7d_ts INTEGER NOT NULL DEFAULT 0"
                )
            if "play_count_7d_locked" not in video_cols:
                conn.execute(
                    "ALTER TABLE videos ADD COLUMN play_count_7d_locked INTEGER NOT NULL DEFAULT 0"
                )
            if "tid" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN tid INTEGER NOT NULL DEFAULT 0")
            if "tid_v2" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN tid_v2 INTEGER NOT NULL DEFAULT 0")
            if "zone_synced" not in video_cols:
                conn.execute("ALTER TABLE videos ADD COLUMN zone_synced INTEGER NOT NULL DEFAULT 0")
            video_snap_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(video_online_snapshots)").fetchall()
            }
            if "play_count" not in video_snap_cols:
                conn.execute("ALTER TABLE video_online_snapshots ADD COLUMN play_count INTEGER")
            if "like_count" not in video_snap_cols:
                conn.execute("ALTER TABLE video_online_snapshots ADD COLUMN like_count INTEGER")
            if "collected_at_ts" not in video_snap_cols:
                conn.execute(
                    "ALTER TABLE video_online_snapshots ADD COLUMN collected_at_ts INTEGER"
                )
                conn.execute(
                    """
                    UPDATE video_online_snapshots
                    SET collected_at_ts = (
                        SELECT s.collected_at_ts
                        FROM account_snapshots s
                        WHERE s.id = video_online_snapshots.snapshot_id
                    )
                    WHERE collected_at_ts IS NULL
                    """
                )
            # Covering indexes for zero-table-lookup video history queries (supersede legacy non-covering bvid indexes)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_snapshots_bvid_ts_cover
                    ON video_online_snapshots (
                        bvid, collected_at_ts DESC, snapshot_id DESC,
                        online_count, online_raw, play_count, like_count
                    )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_pulse_bvid_ts_cover
                    ON video_pulse_snapshots (
                        bvid, collected_at_ts DESC, id DESC,
                        online_count, online_raw, play_count, like_count
                    )
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_video_snapshots_bvid;")
            conn.execute("DROP INDEX IF EXISTS idx_video_pulse_bvid_ts;")
            now = datetime.now(timezone.utc).astimezone().isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                [(key, str(default_val), now) for key, default_val in DEFAULT_SETTINGS.items()],
            )
            self._migrate_online_count_rule(conn, now)
            self._migrate_initial_account(conn, now)
            self._migrate_global_cookie(conn, now)
            self._migrate_play_count_7d(conn, now)
            self._seed_default_admin_password(conn, now)

    @classmethod
    def _migrate_play_count_7d(cls, conn: sqlite3.Connection, now: str) -> None:
        """One-time backfill of 7-day post-publication play counts from historical snapshots."""
        flag_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'play_count_7d_backfilled_v1'"
        ).fetchone()
        if flag_row and flag_row["value"] == "1":
            return

        video_rows = conn.execute(
            """
            SELECT bvid, pubdate, play_count, last_play_ts,
                   play_count_7d, play_count_7d_ts, play_count_7d_locked
            FROM videos
            WHERE pubdate > 0
            """
        ).fetchall()
        if video_rows:
            now_epoch = int(time.time())
            acc_bounds = conn.execute(
                "SELECT MIN(collected_at_ts) AS min_ts, MAX(collected_at_ts) AS max_ts FROM account_snapshots"
            ).fetchone()
            pulse_bounds = conn.execute(
                "SELECT MIN(collected_at_ts) AS min_ts, MAX(collected_at_ts) AS max_ts FROM video_pulse_snapshots"
            ).fetchone()
            min_candidates = [
                int(r["min_ts"])
                for r in (acc_bounds, pulse_bounds)
                if r is not None and r["min_ts"] is not None
            ]
            min_snap_ts = min(min_candidates) if min_candidates else None

            updates: list[tuple[Optional[int], int, int, str]] = []
            for row in video_rows:
                if int(row["play_count_7d_locked"] or 0):
                    continue
                bvid = row["bvid"]
                pubdate = int(row["pubdate"] or 0)
                cur_play = max(0, int(row["play_count"] or 0))
                last_play_ts = int(row["last_play_ts"] or 0)
                target_ts = pubdate + SEVEN_DAY_WINDOW_SECONDS

                if now_epoch < target_ts:
                    checkpoint_ts = (
                        min(last_play_ts, target_ts - 1)
                        if last_play_ts > 0
                        else min(now_epoch, target_ts - 1)
                    )
                    updates.append((cur_play, checkpoint_ts, 0, bvid))
                    continue

                # Video is already >= 7 days old. Skip snapshot scan if 7d mark predates all snapshots.
                if (
                    min_snap_ts is None
                    or target_ts < min_snap_ts - SEVEN_DAY_BRACKET_TOLERANCE_SECONDS
                ):
                    if (
                        last_play_ts >= target_ts
                        and (last_play_ts - target_ts) <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS
                    ):
                        updates.append((cur_play, last_play_ts, 1, bvid))
                    elif now_epoch - target_ts <= SEVEN_DAY_SINGLE_SIDE_TOLERANCE_SECONDS:
                        updates.append((None, 0, 0, bvid))
                    else:
                        updates.append((None, 0, 1, bvid))
                    continue

                pre_row = conn.execute(
                    """
                    SELECT collected_at_ts, play_count
                    FROM (
                        SELECT s.collected_at_ts AS collected_at_ts, v.play_count AS play_count
                        FROM video_online_snapshots v
                        JOIN account_snapshots s ON s.id = v.snapshot_id
                        WHERE v.bvid = ? AND v.play_count IS NOT NULL AND s.collected_at_ts <= ?
                        UNION ALL
                        SELECT p.collected_at_ts AS collected_at_ts, p.play_count AS play_count
                        FROM video_pulse_snapshots p
                        WHERE p.bvid = ? AND p.play_count IS NOT NULL AND p.collected_at_ts <= ?
                    )
                    ORDER BY collected_at_ts DESC, play_count DESC
                    LIMIT 1
                    """,
                    (bvid, target_ts, bvid, target_ts),
                ).fetchone()

                post_row = conn.execute(
                    """
                    SELECT collected_at_ts, play_count
                    FROM (
                        SELECT s.collected_at_ts AS collected_at_ts, v.play_count AS play_count
                        FROM video_online_snapshots v
                        JOIN account_snapshots s ON s.id = v.snapshot_id
                        WHERE v.bvid = ? AND v.play_count IS NOT NULL AND s.collected_at_ts >= ?
                        UNION ALL
                        SELECT p.collected_at_ts AS collected_at_ts, p.play_count AS play_count
                        FROM video_pulse_snapshots p
                        WHERE p.bvid = ? AND p.play_count IS NOT NULL AND p.collected_at_ts >= ?
                    )
                    ORDER BY collected_at_ts ASC, play_count ASC
                    LIMIT 1
                    """,
                    (bvid, target_ts, bvid, target_ts),
                ).fetchone()

                pre_ts = int(pre_row["collected_at_ts"]) if pre_row is not None else None
                pre_play = int(pre_row["play_count"]) if pre_row is not None else None
                post_ts = int(post_row["collected_at_ts"]) if post_row is not None else None
                post_play = int(post_row["play_count"]) if post_row is not None else None

                # If no post-7d sample exists yet and we're still within the bracket window,
                # keep unlocked so the next live crawl can bracket and interpolate accurately.
                if (
                    post_ts is None
                    and (now_epoch - target_ts) <= SEVEN_DAY_BRACKET_TOLERANCE_SECONDS
                ):
                    if pre_ts is not None and pre_play is not None:
                        updates.append((max(0, pre_play), pre_ts, 0, bvid))
                    else:
                        updates.append((None, 0, 0, bvid))
                    continue

                settled_play, settled_ts, settled_locked = _settle_play_count_7d(
                    target_ts=target_ts,
                    pre_ts=pre_ts,
                    pre_play=pre_play,
                    post_ts=post_ts,
                    post_play=post_play,
                )
                updates.append((settled_play, settled_ts, settled_locked, bvid))

            if updates:
                conn.executemany(
                    """
                    UPDATE videos
                    SET play_count_7d = ?,
                        play_count_7d_ts = ?,
                        play_count_7d_locked = ?
                    WHERE bvid = ?
                    """,
                    updates,
                )

        cls._upsert_setting(conn, "play_count_7d_backfilled_v1", "1", now)

    @staticmethod
    def _seed_default_admin_password(conn: sqlite3.Connection, now: str) -> None:
        """Seed initial admin password hash once when admin_password_hash is absent."""
        existing = conn.execute(
            "SELECT 1 FROM settings WHERE key = 'admin_password_hash'"
        ).fetchone()
        if existing is not None:
            return
        default_pw = get_default_admin_password()
        if not default_pw:
            return
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('admin_password_hash', ?, ?)",
            (hash_password(default_pw), now),
        )

    @staticmethod
    def _migrate_online_count_rule(conn: sqlite3.Connection, now: str) -> None:
        """One-time migration normalizing online viewer counts to whole-video single-CID minus-1 rule."""
        rule_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'online_count_rule'"
        ).fetchone()
        current_rule = rule_row["value"] if rule_row else ""
        if current_rule == ONLINE_COUNT_RULE:
            return

        already_minus_one = current_rule == LEGACY_ONLINE_COUNT_RULE_V1

        if already_minus_one:
            vos_rows = conn.execute(
                """
                SELECT v.id, v.snapshot_id, v.bvid, v.online_count, v.online_raw,
                       s.uid, s.collected_at_ts
                FROM video_online_snapshots v
                JOIN account_snapshots s ON s.id = v.snapshot_id
                WHERE v.online_raw LIKE '%+%'
                """
            ).fetchall()
        else:
            vos_rows = conn.execute(
                """
                SELECT v.id, v.snapshot_id, v.bvid, v.online_count, v.online_raw,
                       s.uid, s.collected_at_ts
                FROM video_online_snapshots v
                JOIN account_snapshots s ON s.id = v.snapshot_id
                """
            ).fetchall()

        vos_updates: list[tuple[int, str, int]] = []
        affected_bvids_by_uid: dict[str, set[str]] = {}
        snap_polled_deltas: dict[int, dict[str, int]] = {}
        snap_polled_delta_totals: dict[int, int] = {}
        for r in vos_rows:
            raw_text = r["online_raw"] or ""
            if already_minus_one and len(_split_legacy_online_raw(raw_text)) <= 1:
                continue
            new_count, new_raw = _adjusted_online_from_raw(
                raw_text, r["online_count"], already_minus_one=already_minus_one
            )
            old_count = int(r["online_count"])
            if new_count != old_count or new_raw != raw_text:
                vos_updates.append((new_count, new_raw, int(r["id"])))
            delta = old_count - new_count
            if delta != 0:
                affected_bvids_by_uid.setdefault(r["uid"], set()).add(r["bvid"])
            sid = int(r["snapshot_id"])
            snap_polled_deltas.setdefault(sid, {})[r["bvid"]] = delta
            snap_polled_delta_totals[sid] = snap_polled_delta_totals.get(sid, 0) + delta

        if already_minus_one:
            vps_rows = conn.execute(
                """
                SELECT id, uid, bvid, collected_at_ts, online_count, online_raw
                FROM video_pulse_snapshots
                WHERE online_raw LIKE '%+%'
                """
            ).fetchall()
        else:
            vps_rows = conn.execute(
                """
                SELECT id, uid, bvid, collected_at_ts, online_count, online_raw
                FROM video_pulse_snapshots
                """
            ).fetchall()

        vps_updates: list[tuple[int, str, int]] = []
        pulse_events_by_uid: dict[str, list[tuple[int, int, str, int]]] = {}
        for r in vps_rows:
            raw_text = r["online_raw"] or ""
            if already_minus_one and len(_split_legacy_online_raw(raw_text)) <= 1:
                continue
            new_count, new_raw = _adjusted_online_from_raw(
                raw_text, r["online_count"], already_minus_one=already_minus_one
            )
            old_count = int(r["online_count"])
            if new_count != old_count or new_raw != raw_text:
                vps_updates.append((new_count, new_raw, int(r["id"])))
            delta = old_count - new_count
            if delta != 0:
                affected_bvids_by_uid.setdefault(r["uid"], set()).add(r["bvid"])
            pulse_events_by_uid.setdefault(r["uid"], []).append(
                (int(r["collected_at_ts"]), int(r["id"]), r["bvid"], delta)
            )

        if already_minus_one:
            video_rows = conn.execute(
                """
                SELECT bvid, last_online_count, last_online_raw, last_checked_at
                FROM videos
                WHERE last_online_raw LIKE '%+%'
                """
            ).fetchall()
        else:
            video_rows = conn.execute(
                """
                SELECT bvid, last_online_count, last_online_raw, last_checked_at
                FROM videos
                """
            ).fetchall()

        vid_updates: list[tuple[int, str, str]] = []
        for r in video_rows:
            raw_text = r["last_online_raw"] or ""
            old_count = int(r["last_online_count"] or 0)
            if r["last_checked_at"] is None and not raw_text and old_count == 0:
                continue
            if already_minus_one and len(_split_legacy_online_raw(raw_text)) <= 1:
                continue
            new_count, new_raw = _adjusted_online_from_raw(
                raw_text, old_count, already_minus_one=already_minus_one
            )
            if new_count != old_count or new_raw != raw_text:
                vid_updates.append((new_count, new_raw, r["bvid"]))

        # Recompute account_snapshots.online_viewers_total before mutating video_online_snapshots
        # so historical tiered carryover (old_total - old_sum_polled) is adjusted accurately.
        acc_updates: list[tuple[int, int]] = []
        if affected_bvids_by_uid:
            existing_bvids = {
                r["bvid"] for r in conn.execute("SELECT bvid FROM videos").fetchall()
            }
            for uid, aff_bvids in affected_bvids_by_uid.items():
                snaps = conn.execute(
                    """
                    SELECT s.id, s.collected_at_ts, s.online_viewers_total,
                           (SELECT COALESCE(SUM(v.online_count), 0)
                            FROM video_online_snapshots v
                            WHERE v.snapshot_id = s.id) AS old_sum
                    FROM account_snapshots s
                    WHERE s.uid = ? AND s.online_viewers_total IS NOT NULL
                    ORDER BY s.collected_at_ts ASC, s.id ASC
                    """,
                    (uid,),
                ).fetchall()
                if not snaps:
                    continue

                aff_list = sorted(aff_bvids)
                pulses = list(pulse_events_by_uid.get(uid, []))
                max_seen_ts: dict[str, int] = {}
                for ts_val, _pid, pbvid, _pdelta in pulses:
                    if ts_val > max_seen_ts.get(pbvid, 0):
                        max_seen_ts[pbvid] = ts_val

                for i in range(0, len(aff_list), _SQLITE_CHUNK_SIZE):
                    chunk = aff_list[i : i + _SQLITE_CHUNK_SIZE]
                    placeholders = ",".join("?" for _ in chunk)
                    for r in conn.execute(
                        f"""
                        SELECT v.snapshot_id, v.bvid, s.collected_at_ts
                        FROM video_online_snapshots v
                        JOIN account_snapshots s ON s.id = v.snapshot_id
                        WHERE s.uid = ? AND v.bvid IN ({placeholders})
                        """,
                        [uid, *chunk],
                    ).fetchall():
                        snap_polled_deltas.setdefault(int(r["snapshot_id"]), {}).setdefault(
                            r["bvid"], 0
                        )
                        sts_val = int(r["collected_at_ts"])
                        if sts_val > max_seen_ts.get(r["bvid"], 0):
                            max_seen_ts[r["bvid"]] = sts_val

                    if already_minus_one:
                        for r in conn.execute(
                            f"""
                            SELECT id, bvid, collected_at_ts, online_raw
                            FROM video_pulse_snapshots
                            WHERE uid = ? AND bvid IN ({placeholders})
                            """,
                            [uid, *chunk],
                        ).fetchall():
                            if len(_split_legacy_online_raw(r["online_raw"] or "")) <= 1:
                                pts_val = int(r["collected_at_ts"])
                                pulses.append((pts_val, int(r["id"]), r["bvid"], 0))
                                if pts_val > max_seen_ts.get(r["bvid"], 0):
                                    max_seen_ts[r["bvid"]] = pts_val

                pulses.sort(key=lambda item: (item[0], item[1]))
                latest_delta: dict[str, int] = {}
                p_idx = 0
                for s in snaps:
                    sid = int(s["id"])
                    sts = int(s["collected_at_ts"])
                    while p_idx < len(pulses) and pulses[p_idx][0] <= sts:
                        _, _, pbvid, pdelta = pulses[p_idx]
                        latest_delta[pbvid] = pdelta
                        p_idx += 1

                    old_total = int(s["online_viewers_total"])
                    old_sum = int(s["old_sum"])
                    polled_map = snap_polled_deltas.get(sid, {})
                    polled_delta = snap_polled_delta_totals.get(sid, 0)
                    old_carryover = max(0, old_total - old_sum)
                    unpolled_delta = 0
                    if old_carryover > 0:
                        raw_unpolled = sum(
                            d
                            for b, d in latest_delta.items()
                            if b not in polled_map
                            and d > 0
                            and (b in existing_bvids or sts <= max_seen_ts.get(b, 0))
                        )
                        unpolled_delta = min(old_carryover, raw_unpolled)
                    new_total = max(0, old_total - polled_delta - unpolled_delta)
                    for b, d in polled_map.items():
                        latest_delta[b] = d
                    if new_total != old_total:
                        acc_updates.append((new_total, sid))

        if vos_updates:
            conn.executemany(
                "UPDATE video_online_snapshots SET online_count = ?, online_raw = ? WHERE id = ?",
                vos_updates,
            )
        if vps_updates:
            conn.executemany(
                "UPDATE video_pulse_snapshots SET online_count = ?, online_raw = ? WHERE id = ?",
                vps_updates,
            )
        if vid_updates:
            conn.executemany(
                "UPDATE videos SET last_online_count = ?, last_online_raw = ? WHERE bvid = ?",
                vid_updates,
            )
        if acc_updates:
            conn.executemany(
                "UPDATE account_snapshots SET online_viewers_total = ? WHERE id = ?",
                acc_updates,
            )

        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES ('online_count_rule', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (ONLINE_COUNT_RULE, now),
        )

    @staticmethod
    def _migrate_initial_account(conn: sqlite3.Connection, now: str) -> None:
        """Seed monitored_accounts from legacy single-account settings if table is empty."""
        cnt = conn.execute("SELECT COUNT(*) AS cnt FROM monitored_accounts").fetchone()["cnt"]
        if cnt > 0:
            return
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN ('uid', 'account_name', 'account_face', 'cookie', 'enabled')"
        ).fetchall()
        vals = {r["key"]: r["value"] for r in rows}
        uid = vals.get("uid", "").strip()
        if not uid:
            return
        enabled_int = 1 if vals.get("enabled", "true").lower() == "true" else 0
        conn.execute(
            """
            INSERT OR IGNORE INTO monitored_accounts (
                uid, account_name, account_face, cookie, enabled, sort_order, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                uid,
                vals.get("account_name", "").strip(),
                vals.get("account_face", "").strip(),
                vals.get("cookie", "").strip(),
                enabled_int,
                now,
                now,
            ),
        )

    @classmethod
    def _migrate_global_cookie(cls, conn: sqlite3.Connection, now: str) -> None:
        """One-time migration promoting any legacy per-account cookie to global settings.cookie."""
        flag_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'global_cookie_migrated_v1'"
        ).fetchone()
        if flag_row and flag_row["value"] == "1":
            return

        cookie_row = conn.execute("SELECT value FROM settings WHERE key = 'cookie'").fetchone()
        global_cookie = cookie_row["value"].strip() if cookie_row else ""
        if not global_cookie:
            active_row = conn.execute("SELECT value FROM settings WHERE key = 'uid'").fetchone()
            active_uid = active_row["value"].strip() if active_row else ""
            acc_row = conn.execute(
                """
                SELECT cookie FROM monitored_accounts
                WHERE TRIM(cookie) != ''
                ORDER BY CASE WHEN uid = ? THEN 0 ELSE 1 END, sort_order ASC, created_at ASC
                LIMIT 1
                """,
                (active_uid,),
            ).fetchone()
            if acc_row and acc_row["cookie"].strip():
                cls._upsert_setting(conn, "cookie", acc_row["cookie"].strip(), now)

        conn.execute("UPDATE monitored_accounts SET cookie = '' WHERE TRIM(cookie) != ''")
        cls._upsert_setting(conn, "global_cookie_migrated_v1", "1", now)

    def get_all_settings(self) -> dict[str, str]:
        with self.read_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            result[row["key"]] = row["value"]
        return result

    def resolve_dashboard_uid(self, uid: Optional[str] = None) -> str:
        """Resolve account UID for read/dashboard endpoints.

        If ``uid`` is provided and exists in ``monitored_accounts``, return it.
        Otherwise fall back to the first monitored account in display sort order
        (``sort_order ASC, created_at ASC``), or ``""`` when no accounts exist.
        """
        requested = (uid or "").strip()
        with self.read_connection() as conn:
            if requested:
                row = conn.execute(
                    "SELECT uid FROM monitored_accounts WHERE uid = ?",
                    (requested,),
                ).fetchone()
                if row is not None:
                    return str(row["uid"]).strip()

            first = conn.execute(
                "SELECT uid FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC LIMIT 1"
            ).fetchone()
            if first is not None:
                return str(first["uid"]).strip()

            if uid is None:
                legacy = conn.execute(
                    "SELECT value FROM settings WHERE key = 'uid'"
                ).fetchone()
                if legacy is not None:
                    return str(legacy["value"]).strip()
            return ""

    def get_public_settings(self, uid: Optional[str] = None) -> dict[str, Any]:
        with self.read_connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            raw = dict(DEFAULT_SETTINGS)
            for row in rows:
                raw[row["key"]] = row["value"]

            target_uid = (uid if uid is not None else raw.get("uid", "")).strip()
            acc_row = (
                conn.execute(
                    "SELECT * FROM monitored_accounts WHERE uid = ?", (target_uid,)
                ).fetchone()
                if target_uid
                else None
            )
            account = dict(acc_row) if acc_row is not None else None

        global_cookie = raw.get("cookie", "").strip()
        effective_cookie = (
            (account.get("cookie") or "").strip()
            if account and (account.get("cookie") or "").strip()
            else global_cookie
        )

        if account is not None:
            enabled = bool(account["enabled"])
            account_name = account["account_name"]
            account_face = account["account_face"]
        else:
            enabled = raw.get("enabled", "true").lower() == "true"
            account_name = raw.get("account_name", "") if target_uid == raw.get("uid", "").strip() else ""
            account_face = raw.get("account_face", "") if target_uid == raw.get("uid", "").strip() else ""

        return {
            "uid": target_uid,
            "has_cookie": bool(effective_cookie),
            "cookie_preview": _mask_cookie(effective_cookie),
            "interval_minutes": max(1, int(raw.get("interval_minutes", "5") or 5)),
            "enabled": enabled,
            "request_delay_ms": max(50, int(raw.get("request_delay_ms", "200") or 200)),
            "video_sync_interval_minutes": max(
                5, int(raw.get("video_sync_interval_minutes", "30") or 30)
            ),
            "snapshot_retention_days": _parse_retention_days(raw.get("snapshot_retention_days")),
            "account_name": account_name,
            "account_face": account_face,
        }

    @staticmethod
    def _upsert_setting(conn: sqlite3.Connection, key: str, str_val: str, now: str) -> None:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str_val, now),
        )

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            current_uid_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'uid'"
            ).fetchone()
            old_uid = current_uid_row["value"].strip() if current_uid_row else ""

            for key, value in updates.items():
                if value is None:
                    continue
                if isinstance(value, bool):
                    str_val = "true" if value else "false"
                else:
                    str_val = str(value).strip()
                self._upsert_setting(conn, key, str_val, now)

            target_uid = (
                str(updates["uid"]).strip() if updates.get("uid") is not None else old_uid
            )
            if target_uid:
                existing = conn.execute(
                    "SELECT * FROM monitored_accounts WHERE uid = ?", (target_uid,)
                ).fetchone()
                if existing is None:
                    max_row = conn.execute(
                        "SELECT COALESCE(MAX(sort_order), -1) AS max_ord FROM monitored_accounts"
                    ).fetchone()
                    next_ord = int(max_row["max_ord"]) + 1 if max_row else 0
                    enabled_val = (
                        1
                        if (
                            str(updates.get("enabled", "true")).lower() == "true"
                            if updates.get("enabled") is not None
                            else True
                        )
                        else 0
                    )
                    conn.execute(
                        """
                        INSERT INTO monitored_accounts (
                            uid, account_name, account_face, cookie, enabled, sort_order, created_at, updated_at
                        ) VALUES (?, ?, ?, '', ?, ?, ?, ?)
                        """,
                        (
                            target_uid,
                            str(updates.get("account_name") or "").strip(),
                            str(updates.get("account_face") or "").strip(),
                            enabled_val,
                            next_ord,
                            now,
                            now,
                        ),
                    )
                else:
                    acc_fields: list[str] = []
                    acc_params: list[Any] = []
                    if updates.get("enabled") is not None:
                        en = 1 if str(updates["enabled"]).lower() == "true" else 0
                        acc_fields.append("enabled = ?")
                        acc_params.append(en)
                    elif target_uid != old_uid:
                        self._upsert_setting(
                            conn, "enabled", "true" if existing["enabled"] else "false", now
                        )

                    if updates.get("account_name") is not None:
                        acc_fields.append("account_name = ?")
                        acc_params.append(str(updates["account_name"]).strip())
                    elif target_uid != old_uid:
                        self._upsert_setting(conn, "account_name", existing["account_name"], now)

                    if updates.get("account_face") is not None:
                        acc_fields.append("account_face = ?")
                        acc_params.append(str(updates["account_face"]).strip())
                    elif target_uid != old_uid:
                        self._upsert_setting(conn, "account_face", existing["account_face"], now)

                    if acc_fields:
                        acc_fields.append("updated_at = ?")
                        acc_params.append(now)
                        acc_params.append(target_uid)
                        conn.execute(
                            f"UPDATE monitored_accounts SET {', '.join(acc_fields)} WHERE uid = ?",
                            acc_params,
                        )
        return self.get_public_settings()

    def get_setting(self, key: str, default: str = "") -> str:
        with self.read_connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def get_effective_cookie(self, uid: Optional[str] = None) -> str:
        """Return account-specific cookie override if set, otherwise the global cookie."""
        clean_uid = str(uid or "").strip()
        with self.read_connection() as conn:
            if clean_uid:
                acc_row = conn.execute(
                    "SELECT cookie FROM monitored_accounts WHERE uid = ?", (clean_uid,)
                ).fetchone()
                if acc_row is not None:
                    acc_cookie = (acc_row["cookie"] or "").strip()
                    if acc_cookie:
                        return acc_cookie
            row = conn.execute("SELECT value FROM settings WHERE key = 'cookie'").fetchone()
        return row["value"].strip() if row else ""

    def get_account(self, uid: str) -> Optional[dict[str, Any]]:
        clean_uid = str(uid or "").strip()
        if not clean_uid:
            return None
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitored_accounts WHERE uid = ?", (clean_uid,)
            ).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_enabled_accounts(self) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monitored_accounts WHERE enabled = 1 ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
            # Fallback if monitored_accounts is empty (e.g. unmigrated edge case)
            any_acc = conn.execute("SELECT 1 FROM monitored_accounts LIMIT 1").fetchone()
            if any_acc is not None:
                return []
            setting_rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('uid', 'enabled', 'account_name', 'account_face', 'cookie')"
            ).fetchall()
        vals = {r["key"]: r["value"] for r in setting_rows}
        uid = vals.get("uid", "").strip()
        enabled = vals.get("enabled", "true").lower() == "true"
        if uid and enabled:
            return [
                {
                    "uid": uid,
                    "account_name": vals.get("account_name", ""),
                    "account_face": vals.get("account_face", ""),
                    "cookie": vals.get("cookie", ""),
                    "enabled": 1,
                    "sort_order": 0,
                }
            ]
        return []

    def get_accounts_summary(self, crawling_uid: Optional[str] = None) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            acc_rows = conn.execute(
                "SELECT * FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
            if not acc_rows:
                return []

            cookie_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'cookie'"
            ).fetchone()
            global_cookie = cookie_row["value"].strip() if cookie_row is not None else ""

            vid_counts = {
                r["uid"]: int(r["cnt"])
                for r in conn.execute(
                    "SELECT uid, COUNT(*) AS cnt FROM videos GROUP BY uid"
                ).fetchall()
            }

            snaps_by_uid: dict[str, list[dict[str, Any]]] = {}
            for acc_row in acc_rows:
                acc_uid = acc_row["uid"]
                snap_rows = conn.execute(
                    """
                    SELECT id, uid, collected_at, collected_at_ts, follower_count,
                           online_viewers_total, video_count, status
                    FROM account_snapshots
                    WHERE uid = ?
                    ORDER BY collected_at_ts DESC, id DESC
                    LIMIT 2
                    """,
                    (acc_uid,),
                ).fetchall()
                if snap_rows:
                    snaps_by_uid[acc_uid] = [dict(r) for r in snap_rows]

        summaries: list[dict[str, Any]] = []
        for acc_row in acc_rows:
            acc = dict(acc_row)
            uid = acc["uid"]
            snaps = snaps_by_uid.get(uid, [])
            latest = snaps[0] if len(snaps) >= 1 else None
            prev = snaps[1] if len(snaps) >= 2 else None

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
                    online_delta = (
                        latest["online_viewers_total"] - prev["online_viewers_total"]
                    )

            vid_cnt = vid_counts.get(uid, 0)
            if vid_cnt == 0 and latest:
                vid_cnt = int(latest.get("video_count") or 0)

            cookie_str = (acc.get("cookie") or "").strip() or global_cookie
            summaries.append(
                {
                    "uid": uid,
                    "account_name": acc.get("account_name") or "",
                    "account_face": acc.get("account_face") or "",
                    "has_cookie": bool(cookie_str),
                    "cookie_preview": _mask_cookie(cookie_str),
                    "enabled": bool(acc.get("enabled")),
                    "sort_order": int(acc.get("sort_order") or 0),
                    "latest_follower_count": latest.get("follower_count") if latest else None,
                    "latest_online_viewers_total": (
                        latest.get("online_viewers_total") if latest else None
                    ),
                    "follower_delta": follower_delta,
                    "online_delta": online_delta,
                    "video_count": vid_cnt,
                    "last_crawl_at": latest.get("collected_at") if latest else None,
                    "last_crawl_status": latest.get("status") if latest else None,
                    "is_crawling": bool(crawling_uid and crawling_uid == uid),
                }
            )
        return summaries

    def create_account(
        self,
        uid: str,
        cookie: str = "",
        enabled: bool = True,
        account_name: str = "",
        account_face: str = "",
        set_active: bool = True,
    ) -> dict[str, Any]:
        clean_uid = str(uid).strip()
        clean_cookie = str(cookie or "").strip()
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) AS max_ord FROM monitored_accounts"
            ).fetchone()
            next_ord = int(max_row["max_ord"]) + 1 if max_row else 0
            conn.execute(
                """
                INSERT INTO monitored_accounts (
                    uid, account_name, account_face, cookie, enabled, sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_uid,
                    account_name.strip(),
                    account_face.strip(),
                    clean_cookie,
                    1 if enabled else 0,
                    next_ord,
                    now,
                    now,
                ),
            )
            current_uid_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'uid'"
            ).fetchone()
            current_uid = current_uid_row["value"].strip() if current_uid_row else ""
            if set_active or not current_uid:
                self._upsert_setting(conn, "uid", clean_uid, now)
                self._upsert_setting(conn, "account_name", account_name.strip(), now)
                self._upsert_setting(conn, "account_face", account_face.strip(), now)
                self._upsert_setting(conn, "enabled", "true" if enabled else "false", now)

        return next(
            (a for a in self.get_accounts_summary() if a["uid"] == clean_uid),
            {"uid": clean_uid},
        )

    def update_account(
        self,
        uid: str,
        cookie: Optional[str] = None,
        enabled: Optional[bool] = None,
        account_name: Optional[str] = None,
        account_face: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        clean_uid = str(uid).strip()
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT * FROM monitored_accounts WHERE uid = ?", (clean_uid,)
            ).fetchone()
            if existing is None:
                return None

            fields: list[str] = []
            params: list[Any] = []
            if cookie is not None:
                fields.append("cookie = ?")
                params.append(cookie.strip())
            if enabled is not None:
                fields.append("enabled = ?")
                params.append(1 if enabled else 0)
            if account_name is not None:
                fields.append("account_name = ?")
                params.append(account_name.strip())
            if account_face is not None:
                fields.append("account_face = ?")
                params.append(account_face.strip())

            if fields:
                fields.append("updated_at = ?")
                params.append(now)
                params.append(clean_uid)
                conn.execute(
                    f"UPDATE monitored_accounts SET {', '.join(fields)} WHERE uid = ?",
                    params,
                )

            active_row = conn.execute("SELECT value FROM settings WHERE key = 'uid'").fetchone()
            active_uid = active_row["value"].strip() if active_row else ""
            if active_uid == clean_uid:
                if enabled is not None:
                    self._upsert_setting(conn, "enabled", "true" if enabled else "false", now)
                if account_name is not None:
                    self._upsert_setting(conn, "account_name", account_name.strip(), now)
                if account_face is not None:
                    self._upsert_setting(conn, "account_face", account_face.strip(), now)

        return next((a for a in self.get_accounts_summary() if a["uid"] == clean_uid), None)

    def update_account_profile(
        self,
        uid: str,
        account_name: Optional[str] = None,
        account_face: Optional[str] = None,
    ) -> None:
        clean_uid = str(uid).strip()
        if not clean_uid or (account_name is None and account_face is None):
            return
        clean_name = account_name.strip() if account_name is not None else None
        clean_face = account_face.strip() if account_face is not None else None

        # Avoid opening a SQLite write transaction on every crawl when profile is unchanged.
        with self.read_connection() as rconn:
            acc_row = rconn.execute(
                "SELECT account_name, account_face FROM monitored_accounts WHERE uid = ?",
                (clean_uid,),
            ).fetchone()
            active_row = rconn.execute(
                "SELECT value FROM settings WHERE key = 'uid'"
            ).fetchone()
            active_uid = active_row["value"].strip() if active_row else ""
            acc_unchanged = (
                acc_row is not None
                and (clean_name is None or acc_row["account_name"] == clean_name)
                and (clean_face is None or acc_row["account_face"] == clean_face)
            )
            if acc_unchanged:
                if active_uid != clean_uid:
                    return
                s_rows = rconn.execute(
                    "SELECT key, value FROM settings WHERE key IN ('account_name', 'account_face')"
                ).fetchall()
                s_vals = {r["key"]: r["value"] for r in s_rows}
                if (
                    (clean_name is None or s_vals.get("account_name", "") == clean_name)
                    and (clean_face is None or s_vals.get("account_face", "") == clean_face)
                ):
                    return

        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            fields: list[str] = []
            params: list[Any] = []
            if clean_name is not None:
                fields.append("account_name = ?")
                params.append(clean_name)
            if clean_face is not None:
                fields.append("account_face = ?")
                params.append(clean_face)
            fields.append("updated_at = ?")
            params.append(now)
            params.append(clean_uid)
            conn.execute(
                f"UPDATE monitored_accounts SET {', '.join(fields)} WHERE uid = ?",
                params,
            )

            active_row = conn.execute("SELECT value FROM settings WHERE key = 'uid'").fetchone()
            active_uid = active_row["value"].strip() if active_row else ""
            if active_uid == clean_uid:
                if clean_name is not None:
                    self._upsert_setting(conn, "account_name", clean_name, now)
                if clean_face is not None:
                    self._upsert_setting(conn, "account_face", clean_face, now)

    def set_active_account(
        self,
        uid: str,
        *,
        include_summary: bool = True,
        crawling_uid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        clean_uid = str(uid).strip()
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitored_accounts WHERE uid = ?", (clean_uid,)
            ).fetchone()
            if row is None:
                return None
            self._upsert_setting(conn, "uid", clean_uid, now)
            self._upsert_setting(conn, "account_name", row["account_name"], now)
            self._upsert_setting(conn, "account_face", row["account_face"], now)
            self._upsert_setting(
                conn, "enabled", "true" if row["enabled"] else "false", now
            )
            basic_account = {
                "uid": clean_uid,
                "account_name": row["account_name"] or "",
                "account_face": row["account_face"] or "",
                "enabled": bool(row["enabled"]),
                "sort_order": int(row["sort_order"] or 0),
            }
        if not include_summary:
            return basic_account
        return next(
            (a for a in self.get_accounts_summary(crawling_uid=crawling_uid) if a["uid"] == clean_uid),
            basic_account,
        )

    @staticmethod
    def _delete_video_snapshots_by_bvids(conn: sqlite3.Connection, bvids: list[str]) -> None:
        for i in range(0, len(bvids), _SQLITE_CHUNK_SIZE):
            chunk = bvids[i : i + _SQLITE_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM video_online_snapshots WHERE bvid IN ({placeholders})",
                chunk,
            )
            conn.execute(
                f"DELETE FROM video_pulse_snapshots WHERE bvid IN ({placeholders})",
                chunk,
            )

    def reorder_accounts(
        self, ordered_uids: list[str], crawling_uid: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Persist custom display/crawl sort_order for monitored accounts."""
        now = datetime.now(timezone.utc).astimezone().isoformat()
        seen: set[str] = set()
        requested: list[str] = []
        for raw in ordered_uids:
            clean = str(raw or "").strip()
            if clean and clean not in seen:
                seen.add(clean)
                requested.append(clean)

        with self.connection() as conn:
            rows = conn.execute(
                "SELECT uid FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
            existing_order = [r["uid"] for r in rows]
            existing_set = set(existing_order)

            final_order = [u for u in requested if u in existing_set]
            final_set = set(final_order)
            for u in existing_order:
                if u not in final_set:
                    final_order.append(u)

            if final_order:
                conn.executemany(
                    "UPDATE monitored_accounts SET sort_order = ?, updated_at = ? WHERE uid = ?",
                    [(idx, now, uid) for idx, uid in enumerate(final_order)],
                )

        return self.get_accounts_summary(crawling_uid=crawling_uid)

    def delete_account(self, uid: str, delete_data: bool = False) -> Optional[dict[str, Any]]:
        clean_uid = str(uid).strip()
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            existing = conn.execute(
                "SELECT uid FROM monitored_accounts WHERE uid = ?", (clean_uid,)
            ).fetchone()
            if existing is None:
                return None

            conn.execute("DELETE FROM monitored_accounts WHERE uid = ?", (clean_uid,))
            if delete_data:
                video_rows = conn.execute(
                    "SELECT bvid FROM videos WHERE uid = ?", (clean_uid,)
                ).fetchall()
                bvids = [r["bvid"] for r in video_rows]
                conn.execute("DELETE FROM account_snapshots WHERE uid = ?", (clean_uid,))
                conn.execute("DELETE FROM video_pulse_snapshots WHERE uid = ?", (clean_uid,))
                if bvids:
                    self._delete_video_snapshots_by_bvids(conn, bvids)
                conn.execute("DELETE FROM videos WHERE uid = ?", (clean_uid,))
                conn.execute(
                    "DELETE FROM settings WHERE key IN (?, ?, ?, ?)",
                    (
                        f"last_video_sync_ts_{clean_uid}",
                        f"last_full_video_sync_ts_{clean_uid}",
                        f"video_sync_rule_{clean_uid}",
                        f"last_archive_count_{clean_uid}",
                    ),
                )

            active_row = conn.execute("SELECT value FROM settings WHERE key = 'uid'").fetchone()
            active_uid = active_row["value"].strip() if active_row else ""
            if active_uid == clean_uid:
                next_acc = conn.execute(
                    "SELECT * FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC LIMIT 1"
                ).fetchone()
                if next_acc is not None:
                    new_active = next_acc["uid"]
                    self._upsert_setting(conn, "uid", new_active, now)
                    self._upsert_setting(conn, "account_name", next_acc["account_name"], now)
                    self._upsert_setting(conn, "account_face", next_acc["account_face"], now)
                    self._upsert_setting(
                        conn, "enabled", "true" if next_acc["enabled"] else "false", now
                    )
                else:
                    new_active = ""
                    self._upsert_setting(conn, "uid", "", now)
                    self._upsert_setting(conn, "account_name", "", now)
                    self._upsert_setting(conn, "account_face", "", now)
            else:
                new_active = active_uid

        return {"deleted": True, "uid": clean_uid, "active_uid": new_active}

    def upsert_videos(
        self, uid: str, videos: list[dict[str, Any]], now_ts: Optional[int] = None
    ) -> None:
        if not videos:
            return
        now_dt = (
            datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone()
            if now_ts is not None
            else datetime.now(timezone.utc).astimezone()
        )
        now = now_dt.isoformat()
        effective_now_ts = int(now_ts if now_ts is not None else now_dt.timestamp())

        bvids = list(dict.fromkeys(v["bvid"] for v in videos))
        existing_map: dict[str, sqlite3.Row] = {}
        with self.connection() as conn:
            for i in range(0, len(bvids), _SQLITE_CHUNK_SIZE):
                chunk = bvids[i : i + _SQLITE_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT bvid, pubdate, play_count, last_play_ts, play_rate_per_hour,
                           fast_until_ts, hot_until_ts, zero_online_streak,
                           play_count_7d, play_count_7d_ts, play_count_7d_locked
                    FROM videos WHERE bvid IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    existing_map[row["bvid"]] = row

            params = []
            for v in videos:
                has_stats = (
                    ("like_count" in v and v["like_count"] is not None)
                    or ("coin_count" in v and v["coin_count"] is not None)
                )
                like_val = max(0, int(v.get("like_count") or 0)) if has_stats else 0
                coin_val = max(0, int(v.get("coin_count") or 0)) if has_stats else 0
                synced_val = v.get("stats_synced_at") or (now if has_stats else None)
                incoming_play = max(0, int(v.get("play_count", 0)))
                incoming_pubdate = int(v.get("pubdate", 0))

                existing = existing_map.get(v["bvid"])
                if existing is not None:
                    (
                        eff_play,
                        play_rate,
                        play_ts,
                        fast_until,
                        hot_until,
                        zero_streak,
                    ) = _compute_play_rate_update(
                        old_play=int(existing["play_count"] or 0),
                        new_play=incoming_play,
                        old_play_ts=int(existing["last_play_ts"] or 0),
                        now_ts=effective_now_ts,
                        old_rate=float(existing["play_rate_per_hour"] or 0.0),
                        old_fast_until=int(existing["fast_until_ts"] or 0),
                        old_hot_until=int(existing["hot_until_ts"] or 0),
                        old_zero_streak=int(existing["zero_online_streak"] or 0),
                    )
                    effective_pubdate = (
                        incoming_pubdate if incoming_pubdate > 0 else int(existing["pubdate"] or 0)
                    )
                    play_7d, play_7d_ts, play_7d_locked = _compute_play_count_7d_update(
                        pubdate=effective_pubdate,
                        obs_play=eff_play,
                        obs_ts=effective_now_ts,
                        old_play_7d=existing["play_count_7d"],
                        old_play_7d_ts=int(existing["play_count_7d_ts"] or 0),
                        old_locked=int(existing["play_count_7d_locked"] or 0),
                    )
                else:
                    eff_play = incoming_play
                    play_rate = max(0.0, float(v.get("play_rate_per_hour") or 0.0))
                    play_ts = int(v.get("last_play_ts") or effective_now_ts)
                    fast_until = int(v.get("fast_until_ts") or 0)
                    hot_until = int(v.get("hot_until_ts") or 0)
                    zero_streak = max(0, int(v.get("zero_online_streak") or 0))
                    effective_pubdate = incoming_pubdate
                    play_7d, play_7d_ts, play_7d_locked = _compute_play_count_7d_update(
                        pubdate=effective_pubdate,
                        obs_play=eff_play,
                        obs_ts=effective_now_ts,
                        old_play_7d=v.get("play_count_7d"),
                        old_play_7d_ts=int(v.get("play_count_7d_ts") or 0),
                        old_locked=int(v.get("play_count_7d_locked") or 0),
                    )

                stats_play_val = eff_play if synced_val is not None else None
                tid_val = max(0, int(v.get("tid") or 0))
                tid_v2_val = max(0, int(v.get("tid_v2") or 0))
                params.append(
                    (
                        v["bvid"],
                        uid,
                        int(v["aid"]),
                        v["title"],
                        v.get("pic", ""),
                        effective_pubdate,
                        int(v.get("duration", 0)),
                        eff_play,
                        play_7d,
                        play_7d_ts,
                        play_7d_locked,
                        like_val,
                        coin_val,
                        tid_val,
                        tid_v2_val,
                        1,
                        synced_val,
                        stats_play_val,
                        json.dumps(v.get("cids", []), ensure_ascii=False),
                        hot_until,
                        fast_until,
                        zero_streak,
                        play_rate,
                        play_ts,
                        now,
                    )
                )

            conn.executemany(
                """
                INSERT INTO videos (
                    bvid, uid, aid, title, pic, pubdate, duration, play_count,
                    play_count_7d, play_count_7d_ts, play_count_7d_locked,
                    like_count, coin_count, tid, tid_v2, zone_synced,
                    stats_synced_at, stats_play_count, cids_json,
                    hot_until_ts, fast_until_ts, zero_online_streak, play_rate_per_hour,
                    last_play_ts, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    uid=excluded.uid,
                    aid=excluded.aid,
                    title=excluded.title,
                    pic=excluded.pic,
                    pubdate=CASE
                        WHEN excluded.pubdate > 0 THEN excluded.pubdate
                        ELSE videos.pubdate
                    END,
                    duration=CASE
                        WHEN excluded.duration > 0 THEN excluded.duration
                        ELSE videos.duration
                    END,
                    play_count=MAX(videos.play_count, excluded.play_count),
                    play_count_7d=CASE
                        WHEN videos.play_count_7d_locked = 1 THEN videos.play_count_7d
                        ELSE excluded.play_count_7d
                    END,
                    play_count_7d_ts=CASE
                        WHEN videos.play_count_7d_locked = 1 THEN videos.play_count_7d_ts
                        ELSE excluded.play_count_7d_ts
                    END,
                    play_count_7d_locked=MAX(videos.play_count_7d_locked, excluded.play_count_7d_locked),
                    like_count=CASE
                        WHEN excluded.stats_synced_at IS NOT NULL THEN excluded.like_count
                        ELSE videos.like_count
                    END,
                    coin_count=CASE
                        WHEN excluded.stats_synced_at IS NOT NULL THEN excluded.coin_count
                        ELSE videos.coin_count
                    END,
                    tid=CASE
                        WHEN excluded.tid > 0 THEN excluded.tid
                        ELSE videos.tid
                    END,
                    tid_v2=CASE
                        WHEN excluded.tid_v2 > 0 THEN excluded.tid_v2
                        ELSE videos.tid_v2
                    END,
                    zone_synced=CASE
                        WHEN excluded.tid_v2 > 0 THEN 1
                        ELSE videos.zone_synced
                    END,
                    stats_synced_at=COALESCE(excluded.stats_synced_at, videos.stats_synced_at),
                    stats_play_count=COALESCE(excluded.stats_play_count, videos.stats_play_count),
                    cids_json=CASE
                        WHEN excluded.cids_json != '[]' THEN excluded.cids_json
                        ELSE videos.cids_json
                    END,
                    hot_until_ts=MAX(videos.hot_until_ts, excluded.hot_until_ts),
                    fast_until_ts=MAX(videos.fast_until_ts, excluded.fast_until_ts),
                    zero_online_streak=excluded.zero_online_streak,
                    play_rate_per_hour=excluded.play_rate_per_hour,
                    last_play_ts=excluded.last_play_ts,
                    updated_at=excluded.updated_at
                """,
                params,
            )

    def update_video_stats_batch(
        self, items: list[dict[str, Any]], now_ts: Optional[int] = None
    ) -> None:
        """Persist refreshed play, like, and coin counts and update play velocity."""
        if not items:
            return
        now_dt = (
            datetime.fromtimestamp(now_ts, tz=timezone.utc).astimezone()
            if now_ts is not None
            else datetime.now(timezone.utc).astimezone()
        )
        now = now_dt.isoformat()
        effective_now_ts = int(now_ts if now_ts is not None else now_dt.timestamp())

        bvids = list(dict.fromkeys(item["bvid"] for item in items))
        existing_map: dict[str, sqlite3.Row] = {}
        with self.connection() as conn:
            for i in range(0, len(bvids), _SQLITE_CHUNK_SIZE):
                chunk = bvids[i : i + _SQLITE_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT bvid, pubdate, play_count, last_play_ts, play_rate_per_hour,
                           fast_until_ts, hot_until_ts, zero_online_streak,
                           play_count_7d, play_count_7d_ts, play_count_7d_locked
                    FROM videos WHERE bvid IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    existing_map[row["bvid"]] = row

            params = []
            for item in items:
                bvid = item["bvid"]
                incoming_play = max(0, int(item["play_count"]))
                existing = existing_map.get(bvid)
                if existing is not None:
                    (
                        eff_play,
                        play_rate,
                        play_ts,
                        fast_until,
                        hot_until,
                        zero_streak,
                    ) = _compute_play_rate_update(
                        old_play=int(existing["play_count"] or 0),
                        new_play=incoming_play,
                        old_play_ts=int(existing["last_play_ts"] or 0),
                        now_ts=effective_now_ts,
                        old_rate=float(existing["play_rate_per_hour"] or 0.0),
                        old_fast_until=int(existing["fast_until_ts"] or 0),
                        old_hot_until=int(existing["hot_until_ts"] or 0),
                        old_zero_streak=int(existing["zero_online_streak"] or 0),
                    )
                    play_7d, play_7d_ts, play_7d_locked = _compute_play_count_7d_update(
                        pubdate=int(existing["pubdate"] or 0),
                        obs_play=eff_play,
                        obs_ts=effective_now_ts,
                        old_play_7d=existing["play_count_7d"],
                        old_play_7d_ts=int(existing["play_count_7d_ts"] or 0),
                        old_locked=int(existing["play_count_7d_locked"] or 0),
                    )
                else:
                    eff_play = incoming_play
                    play_rate = 0.0
                    play_ts = effective_now_ts
                    fast_until = 0
                    hot_until = 0
                    zero_streak = 0
                    play_7d = None
                    play_7d_ts = 0
                    play_7d_locked = 0

                tid_val = max(0, int(item.get("tid") or 0))
                tid_v2_val = max(0, int(item.get("tid_v2") or 0))
                params.append(
                    (
                        eff_play,
                        play_7d,
                        play_7d_ts,
                        play_7d_locked,
                        max(0, int(item["like_count"])),
                        max(0, int(item["coin_count"])),
                        tid_val,
                        tid_val,
                        tid_v2_val,
                        tid_v2_val,
                        now,
                        eff_play,
                        0,
                        play_rate,
                        play_ts,
                        fast_until,
                        hot_until,
                        zero_streak,
                        now,
                        bvid,
                    )
                )

            conn.executemany(
                """
                UPDATE videos
                SET play_count = ?,
                    play_count_7d = CASE
                        WHEN play_count_7d_locked = 1 THEN play_count_7d
                        ELSE ?
                    END,
                    play_count_7d_ts = CASE
                        WHEN play_count_7d_locked = 1 THEN play_count_7d_ts
                        ELSE ?
                    END,
                    play_count_7d_locked = MAX(play_count_7d_locked, ?),
                    like_count = ?,
                    coin_count = ?,
                    tid = CASE WHEN ? > 0 THEN ? ELSE tid END,
                    tid_v2 = CASE WHEN ? > 0 THEN ? ELSE tid_v2 END,
                    zone_synced = 1,
                    stats_synced_at = ?,
                    stats_play_count = ?,
                    stats_backoff_until_ts = ?,
                    play_rate_per_hour = ?,
                    last_play_ts = ?,
                    fast_until_ts = MAX(fast_until_ts, ?),
                    hot_until_ts = MAX(hot_until_ts, ?),
                    zero_online_streak = ?,
                    updated_at = ?
                WHERE bvid = ?
                """,
                params,
            )

    def set_video_stats_backoff_batch(self, bvids: list[str], backoff_until_ts: int) -> None:
        """Record a stats-refresh backoff window for permanently failing or missing videos."""
        if not bvids:
            return
        unique_bvids = list(dict.fromkeys(bvids))
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            conn.executemany(
                """
                UPDATE videos
                SET stats_backoff_until_ts = MAX(stats_backoff_until_ts, ?),
                    updated_at = ?
                WHERE bvid = ?
                """,
                [(int(backoff_until_ts), now, bvid) for bvid in unique_bvids],
            )

    def set_video_online_backoff_batch(
        self,
        bvids: list[str],
        checked_at: Optional[datetime] = None,
        backoff_until_ts: Optional[int] = None,
    ) -> None:
        """Back off missing/unavailable videos so they stop failing online polls and clear stale viewers."""
        if not bvids:
            return
        unique_bvids = list(dict.fromkeys(bvids))
        dt = checked_at or datetime.now(timezone.utc).astimezone()
        checked_iso = dt.isoformat()
        effective_backoff_until = (
            int(backoff_until_ts)
            if backoff_until_ts is not None
            else int(dt.timestamp()) + 24 * 3600
        )
        with self.connection() as conn:
            conn.executemany(
                """
                UPDATE videos
                SET last_checked_at = ?,
                    last_online_count = 0,
                    last_online_raw = '',
                    zero_online_streak = MAX(zero_online_streak, 6),
                    hot_until_ts = 0,
                    fast_until_ts = 0,
                    online_backoff_until_ts = MAX(online_backoff_until_ts, ?),
                    updated_at = ?
                WHERE bvid = ?
                """,
                [(checked_iso, effective_backoff_until, checked_iso, bvid) for bvid in unique_bvids],
            )

    def delete_stale_videos(
        self, uid: str, active_bvids: set[str], allow_empty: bool = False
    ) -> int:
        if not active_bvids and not allow_empty:
            return 0
        with self.connection() as conn:
            rows = conn.execute("SELECT bvid FROM videos WHERE uid = ?", (uid,)).fetchall()
            to_delete = [r["bvid"] for r in rows if r["bvid"] not in active_bvids]
            if to_delete:
                self._delete_video_snapshots_by_bvids(conn, to_delete)
                if not active_bvids:
                    conn.execute("DELETE FROM videos WHERE uid = ?", (uid,))
                else:
                    for i in range(0, len(to_delete), _SQLITE_CHUNK_SIZE):
                        chunk = to_delete[i : i + _SQLITE_CHUNK_SIZE]
                        placeholders = ",".join("?" for _ in chunk)
                        conn.execute(
                            f"DELETE FROM videos WHERE uid = ? AND bvid IN ({placeholders})",
                            [uid, *chunk],
                        )
            return len(to_delete)

    def get_videos(self, uid: str, now_ts: Optional[int] = None) -> list[dict[str, Any]]:
        effective_now_ts = int(now_ts if now_ts is not None else time.time())
        with self.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT bvid, uid, aid, title, pic, pubdate, duration, play_count,
                       play_count_7d, play_count_7d_ts, play_count_7d_locked,
                       like_count, coin_count, tid, tid_v2, zone_synced,
                       stats_synced_at, stats_play_count,
                       stats_backoff_until_ts, online_backoff_until_ts,
                       cids_json, last_online_count,
                       last_online_raw, last_checked_at, hot_until_ts, fast_until_ts,
                       zero_online_streak, play_rate_per_hour, last_play_ts, updated_at
                FROM videos
                WHERE uid = ?
                ORDER BY last_online_count DESC, pubdate DESC
                """,
                (uid,),
            ).fetchall()
        items = []
        for r in rows:
            raw_cids = r["cids_json"]
            cids = [] if (not raw_cids or raw_cids == "[]") else json.loads(raw_cids)
            tid_val = int(r["tid"] or 0)
            tid_v2_val = int(r["tid_v2"] or 0)
            zone = resolve_video_zone(tid_val, tid_v2_val)
            display_7d, status_7d = _resolve_video_play_count_7d_view(
                pubdate=int(r["pubdate"] or 0),
                play_count=int(r["play_count"] or 0),
                stored_play_7d=r["play_count_7d"],
                stored_play_7d_ts=int(r["play_count_7d_ts"] or 0),
                stored_locked=int(r["play_count_7d_locked"] or 0),
                now_ts=effective_now_ts,
            )
            items.append(
                {
                    "bvid": r["bvid"],
                    "uid": r["uid"],
                    "aid": r["aid"],
                    "title": r["title"],
                    "pic": r["pic"],
                    "pubdate": r["pubdate"],
                    "duration": r["duration"],
                    "play_count": r["play_count"],
                    "play_count_7d": display_7d,
                    "play_count_7d_status": status_7d,
                    "play_count_7d_ts": int(r["play_count_7d_ts"] or 0),
                    "play_count_7d_locked": bool(r["play_count_7d_locked"]),
                    "like_count": r["like_count"],
                    "coin_count": r["coin_count"],
                    "tid": tid_val,
                    "tid_v2": tid_v2_val,
                    "zone_primary": zone["zone_primary"],
                    "zone_sub": zone["zone_sub"],
                    "zone_version": zone["zone_version"],
                    "zone_synced": int(r["zone_synced"] if r["zone_synced"] is not None else 1),
                    "stats_synced_at": r["stats_synced_at"],
                    "stats_play_count": r["stats_play_count"],
                    "stats_backoff_until_ts": int(r["stats_backoff_until_ts"] or 0),
                    "online_backoff_until_ts": int(r["online_backoff_until_ts"] or 0),
                    "cids": cids,
                    "cid_count": max(1, len(cids)),
                    "last_online_count": r["last_online_count"],
                    "last_online_raw": r["last_online_raw"],
                    "last_checked_at": r["last_checked_at"],
                    "hot_until_ts": int(r["hot_until_ts"] or 0),
                    "fast_until_ts": int(r["fast_until_ts"] or 0),
                    "zero_online_streak": int(r["zero_online_streak"] or 0),
                    "play_rate_per_hour": float(r["play_rate_per_hour"] or 0.0),
                    "last_play_ts": int(r["last_play_ts"] or 0),
                    "updated_at": r["updated_at"],
                }
            )
        return items

    def get_crawl_videos(self, uid: str) -> list[dict[str, Any]]:
        """Fetch minimal video scheduling/polling fields without parsing cids_json or resolving UI zone metadata."""
        with self.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT bvid, pubdate, play_count, like_count, coin_count,
                       tid, tid_v2, zone_synced, stats_synced_at, stats_play_count,
                       stats_backoff_until_ts, online_backoff_until_ts, cids_json,
                       last_online_count, last_checked_at, hot_until_ts, fast_until_ts,
                       zero_online_streak, play_rate_per_hour
                FROM videos
                WHERE uid = ?
                """,
                (uid,),
            ).fetchall()
        return [
            {
                "bvid": r["bvid"],
                "pubdate": r["pubdate"],
                "play_count": r["play_count"],
                "like_count": r["like_count"],
                "coin_count": r["coin_count"],
                "tid": int(r["tid"] or 0),
                "tid_v2": int(r["tid_v2"] or 0),
                "zone_synced": int(r["zone_synced"] if r["zone_synced"] is not None else 1),
                "stats_synced_at": r["stats_synced_at"],
                "stats_play_count": r["stats_play_count"],
                "stats_backoff_until_ts": int(r["stats_backoff_until_ts"] or 0),
                "online_backoff_until_ts": int(r["online_backoff_until_ts"] or 0),
                "cids_json": r["cids_json"] or "[]",
                "last_online_count": r["last_online_count"],
                "last_checked_at": r["last_checked_at"],
                "hot_until_ts": int(r["hot_until_ts"] or 0),
                "fast_until_ts": int(r["fast_until_ts"] or 0),
                "zero_online_streak": int(r["zero_online_streak"] or 0),
                "play_rate_per_hour": float(r["play_rate_per_hour"] or 0.0),
            }
            for r in rows
        ]

    def get_dashboard_videos(
        self,
        uid: str,
        *,
        top_limit: Optional[int] = None,
        now_ts: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch lightweight dashboard video rows (no cids_json Python parsing) with optional dual-board top-N cap."""
        effective_now_ts = int(now_ts if now_ts is not None else time.time())
        select_cols = """
            bvid, uid, aid, title, pic, pubdate, duration, play_count,
            play_count_7d, play_count_7d_ts, play_count_7d_locked,
            like_count, coin_count, tid, tid_v2, stats_synced_at,
            CASE
                WHEN json_valid(cids_json) THEN COALESCE(json_array_length(cids_json), 0)
                ELSE 0
            END AS cid_len,
            last_online_count, last_online_raw, last_checked_at
        """
        with self.read_connection() as conn:
            if top_limit is not None and top_limit > 0:
                total_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM videos WHERE uid = ?", (uid,)
                ).fetchone()
                total_videos = int(total_row["cnt"]) if total_row else 0
                if total_videos == 0:
                    return ([], 0)
                online_rows = conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM videos
                    WHERE uid = ?
                    ORDER BY last_online_count DESC, play_count DESC
                    LIMIT ?
                    """,
                    (uid, top_limit),
                ).fetchall()
                play_rows = conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM videos
                    WHERE uid = ?
                    ORDER BY play_count DESC, last_online_count DESC
                    LIMIT ?
                    """,
                    (uid, top_limit),
                ).fetchall()
                seen_bvids: set[str] = set()
                rows: list[sqlite3.Row] = []
                for r in (*online_rows, *play_rows):
                    bvid = r["bvid"]
                    if bvid not in seen_bvids:
                        seen_bvids.add(bvid)
                        rows.append(r)
            else:
                rows = conn.execute(
                    f"""
                    SELECT {select_cols}
                    FROM videos
                    WHERE uid = ?
                    ORDER BY last_online_count DESC, pubdate DESC
                    """,
                    (uid,),
                ).fetchall()
                total_videos = len(rows)

        items: list[dict[str, Any]] = []
        for r in rows:
            tid_val = int(r["tid"] or 0)
            tid_v2_val = int(r["tid_v2"] or 0)
            zone = resolve_video_zone(tid_val, tid_v2_val)
            display_7d, status_7d = _resolve_video_play_count_7d_view(
                pubdate=int(r["pubdate"] or 0),
                play_count=int(r["play_count"] or 0),
                stored_play_7d=r["play_count_7d"],
                stored_play_7d_ts=int(r["play_count_7d_ts"] or 0),
                stored_locked=int(r["play_count_7d_locked"] or 0),
                now_ts=effective_now_ts,
            )
            items.append(
                {
                    "bvid": r["bvid"],
                    "uid": r["uid"],
                    "aid": r["aid"],
                    "title": r["title"],
                    "pic": r["pic"],
                    "pubdate": r["pubdate"],
                    "duration": r["duration"],
                    "play_count": r["play_count"],
                    "play_count_7d": display_7d,
                    "play_count_7d_status": status_7d,
                    "like_count": r["like_count"],
                    "coin_count": r["coin_count"],
                    "tid": tid_val,
                    "tid_v2": tid_v2_val,
                    "zone_primary": zone["zone_primary"],
                    "zone_sub": zone["zone_sub"],
                    "zone_version": zone["zone_version"],
                    "stats_synced_at": r["stats_synced_at"],
                    "cid_count": max(1, int(r["cid_len"] or 0)),
                    "last_online_count": r["last_online_count"],
                    "last_online_raw": r["last_online_raw"],
                    "last_checked_at": r["last_checked_at"],
                }
            )
        return (items, total_videos)

    def update_video_cids(self, bvid: str, cids: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET cids_json = ?, updated_at = ? WHERE bvid = ?",
                (json.dumps(cids, ensure_ascii=False), now, bvid),
            )

    @staticmethod
    def _resolve_video_snapshot_rows(
        conn: sqlite3.Connection,
        video_online_items: list[dict[str, Any]],
    ) -> tuple[dict[str, int], dict[str, Optional[int]]]:
        """Batch-fetch fallback play_count and like_count for snapshot items missing them."""
        missing_bvids = list(
            dict.fromkeys(
                item["bvid"]
                for item in video_online_items
                if item.get("play_count") is None or "like_count" not in item
            )
        )
        fallback_plays: dict[str, int] = {}
        fallback_likes: dict[str, Optional[int]] = {}
        for i in range(0, len(missing_bvids), _SQLITE_CHUNK_SIZE):
            chunk = missing_bvids[i : i + _SQLITE_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            v_rows = conn.execute(
                f"SELECT bvid, play_count, like_count, stats_synced_at FROM videos WHERE bvid IN ({placeholders})",
                chunk,
            ).fetchall()
            for v_row in v_rows:
                bvid_key = v_row["bvid"]
                if v_row["play_count"] is not None:
                    fallback_plays[bvid_key] = int(v_row["play_count"])
                raw_like = v_row["like_count"]
                has_like_stat = bool(v_row["stats_synced_at"]) or (
                    raw_like is not None and int(raw_like) > 0
                )
                fallback_likes[bvid_key] = (
                    int(raw_like) if (has_like_stat and raw_like is not None) else None
                )
        return fallback_plays, fallback_likes

    @staticmethod
    def _apply_video_online_updates(
        conn: sqlite3.Connection,
        video_online_items: list[dict[str, Any]],
        collected_iso: str,
        collected_ts: int,
    ) -> None:
        """Update videos table online state, zero_online_streak, hot_until_ts, and fast_until_ts."""
        hot_until_ts = collected_ts + HOT_STICKY_SECONDS
        fast_until_ts = collected_ts + FAST_STICKY_SECONDS
        video_updates: list[tuple[Any, ...]] = []
        for item in video_online_items:
            bvid = item["bvid"]
            online_count = int(item["online_count"])
            online_raw = item.get("online_raw", "")
            video_updates.append(
                (
                    online_count,
                    online_raw,
                    collected_iso,
                    online_count,
                    online_count,
                    hot_until_ts,
                    online_count,
                    FAST_ONLINE_THRESHOLD,
                    fast_until_ts,
                    bvid,
                )
            )
        conn.executemany(
            """
            UPDATE videos
            SET last_online_count = ?,
                last_online_raw = ?,
                last_checked_at = ?,
                zero_online_streak = CASE
                    WHEN ? > 0 THEN 0
                    ELSE MIN(zero_online_streak + 1, 10000)
                END,
                hot_until_ts = CASE
                    WHEN ? > 0 THEN MAX(hot_until_ts, ?)
                    ELSE hot_until_ts
                END,
                fast_until_ts = CASE
                    WHEN ? >= ? THEN MAX(fast_until_ts, ?)
                    ELSE fast_until_ts
                END,
                online_backoff_until_ts = 0
            WHERE bvid = ?
            """,
            video_updates,
        )

    def insert_snapshot(
        self,
        uid: str,
        follower_count: Optional[int],
        following_count: Optional[int],
        online_viewers_total: Optional[int],
        video_count: int,
        videos_succeeded: int,
        extra_metrics: dict[str, Any],
        status: str,
        error_message: Optional[str],
        duration_ms: int,
        video_online_items: list[dict[str, Any]],
        collected_at: Optional[datetime] = None,
    ) -> int:
        dt = collected_at or datetime.now(timezone.utc).astimezone()
        collected_iso = dt.isoformat()
        collected_ts = int(dt.timestamp())

        with self.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO account_snapshots (
                    uid, collected_at, collected_at_ts, follower_count, following_count,
                    online_viewers_total, video_count, videos_succeeded,
                    extra_metrics, status, error_message, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    collected_iso,
                    collected_ts,
                    follower_count,
                    following_count,
                    online_viewers_total,
                    video_count,
                    videos_succeeded,
                    json.dumps(extra_metrics, ensure_ascii=False),
                    status,
                    error_message,
                    duration_ms,
                ),
            )
            snapshot_id = int(cur.lastrowid)

            if video_online_items:
                fallback_plays, fallback_likes = self._resolve_video_snapshot_rows(
                    conn, video_online_items
                )
                snapshot_rows: list[
                    tuple[int, str, int, int, str, Optional[int], Optional[int]]
                ] = []
                for item in video_online_items:
                    bvid = item["bvid"]
                    online_count = int(item["online_count"])
                    online_raw = item.get("online_raw", "")
                    raw_play_count = item.get("play_count")
                    play_count_val = (
                        fallback_plays.get(bvid)
                        if raw_play_count is None
                        else int(raw_play_count)
                    )
                    if "like_count" in item:
                        raw_like_count = item["like_count"]
                        like_count_val = (
                            None if raw_like_count is None else int(raw_like_count)
                        )
                    else:
                        like_count_val = fallback_likes.get(bvid)
                    snapshot_rows.append(
                        (
                            snapshot_id,
                            bvid,
                            collected_ts,
                            online_count,
                            online_raw,
                            play_count_val,
                            like_count_val,
                        )
                    )

                conn.executemany(
                    """
                    INSERT INTO video_online_snapshots (
                        snapshot_id, bvid, collected_at_ts, online_count, online_raw, play_count, like_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    snapshot_rows,
                )
                self._apply_video_online_updates(
                    conn, video_online_items, collected_iso, collected_ts
                )
        return snapshot_id

    def insert_video_pulse(
        self,
        uid: str,
        video_online_items: list[dict[str, Any]],
        collected_at: Optional[datetime] = None,
    ) -> int:
        """Persist high-frequency video-only pulse samples without creating an account_snapshot."""
        if not video_online_items:
            return 0
        dt = collected_at or datetime.now(timezone.utc).astimezone()
        collected_iso = dt.isoformat()
        collected_ts = int(dt.timestamp())

        with self.connection() as conn:
            fallback_plays, fallback_likes = self._resolve_video_snapshot_rows(
                conn, video_online_items
            )
            pulse_rows: list[
                tuple[str, str, str, int, int, str, Optional[int], Optional[int]]
            ] = []
            for item in video_online_items:
                bvid = item["bvid"]
                online_count = int(item["online_count"])
                online_raw = item.get("online_raw", "")
                raw_play_count = item.get("play_count")
                play_count_val = (
                    fallback_plays.get(bvid)
                    if raw_play_count is None
                    else int(raw_play_count)
                )
                if "like_count" in item:
                    raw_like_count = item["like_count"]
                    like_count_val = (
                        None if raw_like_count is None else int(raw_like_count)
                    )
                else:
                    like_count_val = fallback_likes.get(bvid)
                pulse_rows.append(
                    (
                        uid,
                        bvid,
                        collected_iso,
                        collected_ts,
                        online_count,
                        online_raw,
                        play_count_val,
                        like_count_val,
                    )
                )

            conn.executemany(
                """
                INSERT INTO video_pulse_snapshots (
                    uid, bvid, collected_at, collected_at_ts, online_count,
                    online_raw, play_count, like_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pulse_rows,
            )
            self._apply_video_online_updates(
                conn, video_online_items, collected_iso, collected_ts
            )
        return len(pulse_rows)

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "uid": row["uid"],
            "collected_at": row["collected_at"],
            "collected_at_ts": row["collected_at_ts"],
            "follower_count": row["follower_count"],
            "following_count": row["following_count"],
            "online_viewers_total": row["online_viewers_total"],
            "video_count": row["video_count"],
            "videos_succeeded": row["videos_succeeded"],
            "extra_metrics": json.loads(row["extra_metrics"] or "{}"),
            "status": row["status"],
            "error_message": row["error_message"],
            "duration_ms": row["duration_ms"],
        }

    def get_snapshots(
        self, uid: str, since_hours: Optional[int] = 24, limit: int = 1000
    ) -> list[dict[str, Any]]:
        params: list[Any] = [uid]
        where = "WHERE uid = ?"
        if since_hours is not None and since_hours > 0:
            cutoff = int(time.time()) - since_hours * 3600
            where += " AND collected_at_ts >= ?"
            params.append(cutoff)
        params.append(limit)

        with self.read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM account_snapshots
                {where}
                ORDER BY collected_at_ts DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        # Return chronologically ascending for charting
        return [self._row_to_snapshot(r) for r in reversed(rows)]

    @staticmethod
    def _downsample_account_snapshots(
        rows: list[dict[str, Any]], max_points: int = 480
    ) -> list[dict[str, Any]]:
        """Downsample overview chart snapshots chronologically while preserving endpoints, extrema, and live tail."""
        total = len(rows)
        if total <= max_points or max_points < 16:
            return rows

        tail_keep = min(48, total // 5)
        head_count = total - tail_keep
        target_head = max(16, max_points - tail_keep - 10)
        stride = head_count / target_head

        must_keep: set[int] = {0, total - 1}
        first_f_idx: Optional[int] = None
        last_f_idx: Optional[int] = None
        min_f_idx: Optional[int] = None
        max_f_idx: Optional[int] = None
        min_f = float("inf")
        max_f = float("-inf")

        first_o_idx: Optional[int] = None
        last_o_idx: Optional[int] = None
        min_o_idx: Optional[int] = None
        max_o_idx: Optional[int] = None
        min_o = float("inf")
        max_o = float("-inf")

        for i, row in enumerate(rows):
            f_raw = row.get("follower_count")
            if f_raw is not None:
                f_val = int(f_raw)
                if first_f_idx is None:
                    first_f_idx = i
                last_f_idx = i
                if f_val < min_f:
                    min_f = f_val
                    min_f_idx = i
                if f_val > max_f:
                    max_f = f_val
                    max_f_idx = i

            o_raw = row.get("online_viewers_total")
            if o_raw is not None:
                o_val = int(o_raw)
                if first_o_idx is None:
                    first_o_idx = i
                last_o_idx = i
                if o_val < min_o:
                    min_o = o_val
                    min_o_idx = i
                if o_val > max_o:
                    max_o = o_val
                    max_o_idx = i

        for idx in (
            first_f_idx,
            last_f_idx,
            min_f_idx,
            max_f_idx,
            first_o_idx,
            last_o_idx,
            min_o_idx,
            max_o_idx,
        ):
            if idx is not None:
                must_keep.add(idx)

        for k in range(target_head):
            must_keep.add(min(head_count - 1, round(k * stride)))
        for i in range(max(0, head_count), total):
            must_keep.add(i)

        return [rows[idx] for idx in sorted(must_keep)]

    def get_chart_snapshots(
        self,
        uid: str,
        since_hours: Optional[int] = 24,
        limit: int = 50000,
        max_points: Optional[int] = 480,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """Fetch lightweight chart-only snapshot points with optional extrema-preserving downsampling."""
        params: list[Any] = [uid]
        where = "WHERE uid = ?"
        if since_hours is not None and since_hours > 0:
            cutoff = int(time.time()) - since_hours * 3600
            where += " AND collected_at_ts >= ?"
            params.append(cutoff)
        params.append(limit)

        with self.read_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT collected_at, collected_at_ts, follower_count, online_viewers_total
                FROM account_snapshots
                {where}
                ORDER BY collected_at_ts DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        total_in_window = len(rows)
        items = [
            {
                "collected_at": r["collected_at"],
                "collected_at_ts": r["collected_at_ts"],
                "follower_count": r["follower_count"],
                "online_viewers_total": r["online_viewers_total"],
            }
            for r in reversed(rows)
        ]
        if max_points is not None and max_points > 0 and len(items) > max_points:
            return (
                self._downsample_account_snapshots(items, max_points=max_points),
                True,
                total_in_window,
            )
        truncated = total_in_window >= limit
        return (items, truncated, total_in_window)

    def get_recent_snapshots(self, uid: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.read_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM account_snapshots
                WHERE uid = ?
                ORDER BY collected_at_ts DESC, id DESC
                LIMIT ?
                """,
                (uid, limit),
            ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_latest_two_snapshots(self, uid: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        items = self.get_recent_snapshots(uid, limit=2)
        latest = items[0] if len(items) >= 1 else None
        prev = items[1] if len(items) >= 2 else None
        return latest, prev

    def count_snapshots(self, uid: str) -> int:
        with self.read_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM account_snapshots WHERE uid = ?", (uid,)
            ).fetchone()
        return int(row["cnt"]) if row else 0

    @staticmethod
    def _compute_day_follower_change(
        day_counts: list[int], prev_day_counts: list[int]
    ) -> Optional[int]:
        """Compute net follower change for one calendar day using prior day close as baseline when available."""
        if not day_counts:
            return None
        end_count = day_counts[-1]
        if prev_day_counts:
            return end_count - prev_day_counts[-1]
        if len(day_counts) >= 2:
            return end_count - day_counts[0]
        return None

    @staticmethod
    def _compute_yesterday_same_time_change(
        yesterday_points: list[tuple[int, int]],
        today_points: list[tuple[int, int]],
        day_before_counts: list[int],
        tolerance_sec: int = 3600,
    ) -> tuple[Optional[int], str]:
        """Compute yesterday's cumulative follower change up to today's latest snapshot time of day."""
        if not yesterday_points or not today_points:
            return None, ""
        target_ts = today_points[-1][0] - 86400
        best_idx = -1
        best_dist = tolerance_sec + 1
        for idx, (ts, _count) in enumerate(yesterday_points):
            dist = abs(ts - target_ts)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
        if best_idx < 0 or best_dist > tolerance_sec:
            return None, ""

        matched_ts, matched_count = yesterday_points[best_idx]
        if day_before_counts:
            baseline = day_before_counts[-1]
        elif best_idx > 0:
            baseline = yesterday_points[0][1]
        else:
            return None, ""

        matched_time_str = datetime.fromtimestamp(matched_ts, tz=timezone.utc).astimezone(
            BJT_TZ
        ).strftime("%H:%M")
        return matched_count - baseline, matched_time_str

    def get_follower_daily_changes(
        self, uid: str, now_ts: Optional[int] = None
    ) -> dict[str, Any]:
        """Return yesterday and today follower count changes by Beijing Time (UTC+8) natural days."""
        now_dt = (
            datetime.fromtimestamp(now_ts, tz=timezone.utc)
            if now_ts is not None
            else datetime.now(timezone.utc)
        ).astimezone(BJT_TZ)
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        day_before_start = today_start - timedelta(days=2)
        tomorrow_start = today_start + timedelta(days=1)

        yesterday_date = yesterday_start.strftime("%Y-%m-%d")
        today_date = today_start.strftime("%Y-%m-%d")
        if not uid:
            return {
                "yesterday": None,
                "yesterday_same_time": None,
                "yesterday_same_time_at": "",
                "today": None,
                "yesterday_date": yesterday_date,
                "today_date": today_date,
                "timezone": "Asia/Shanghai",
            }

        t_day_before = int(day_before_start.timestamp())
        t_yesterday = int(yesterday_start.timestamp())
        t_today = int(today_start.timestamp())
        t_tomorrow = int(tomorrow_start.timestamp())
        tolerance_sec = 3600

        with self.read_connection() as conn:
            def _fetch_edge(start_ts: int, end_ts: int, *, latest: bool) -> Optional[sqlite3.Row]:
                order = "DESC" if latest else "ASC"
                return conn.execute(
                    f"""
                    SELECT id, collected_at_ts, follower_count
                    FROM account_snapshots
                    WHERE uid = ?
                      AND follower_count IS NOT NULL
                      AND collected_at_ts >= ?
                      AND collected_at_ts < ?
                    ORDER BY collected_at_ts {order}, id {order}
                    LIMIT 1
                    """,
                    (uid, start_ts, end_ts),
                ).fetchone()

            day_before_last = _fetch_edge(t_day_before, t_yesterday, latest=True)
            yesterday_last = _fetch_edge(t_yesterday, t_today, latest=True)
            yesterday_first = (
                _fetch_edge(t_yesterday, t_today, latest=False)
                if (yesterday_last is not None and day_before_last is None)
                else None
            )
            today_last = _fetch_edge(t_today, t_tomorrow, latest=True)
            today_first = (
                _fetch_edge(t_today, t_tomorrow, latest=False)
                if (today_last is not None and yesterday_last is None)
                else None
            )

            yesterday_same_time: Optional[int] = None
            yesterday_same_time_at = ""
            if yesterday_last is not None and today_last is not None:
                target_ts = int(today_last["collected_at_ts"]) - 86400
                win_lo = max(t_yesterday, target_ts - tolerance_sec)
                win_hi = min(t_today - 1, target_ts + tolerance_sec)
                if win_lo <= win_hi:
                    before_row: Optional[sqlite3.Row] = None
                    if target_ts >= win_lo:
                        b_max = conn.execute(
                            """
                            SELECT id, collected_at_ts, follower_count
                            FROM account_snapshots
                            WHERE uid = ?
                              AND follower_count IS NOT NULL
                              AND collected_at_ts >= ?
                              AND collected_at_ts <= ?
                            ORDER BY collected_at_ts DESC, id DESC
                            LIMIT 1
                            """,
                            (uid, win_lo, min(win_hi, target_ts)),
                        ).fetchone()
                        if b_max is not None:
                            before_row = (
                                conn.execute(
                                    """
                                    SELECT id, collected_at_ts, follower_count
                                    FROM account_snapshots
                                    WHERE uid = ?
                                      AND follower_count IS NOT NULL
                                      AND collected_at_ts = ?
                                    ORDER BY id ASC
                                    LIMIT 1
                                    """,
                                    (uid, int(b_max["collected_at_ts"])),
                                ).fetchone()
                                or b_max
                            )

                    after_row: Optional[sqlite3.Row] = None
                    if target_ts <= win_hi:
                        after_row = conn.execute(
                            """
                            SELECT id, collected_at_ts, follower_count
                            FROM account_snapshots
                            WHERE uid = ?
                              AND follower_count IS NOT NULL
                              AND collected_at_ts >= ?
                              AND collected_at_ts <= ?
                            ORDER BY collected_at_ts ASC, id ASC
                            LIMIT 1
                            """,
                            (uid, max(win_lo, target_ts), win_hi),
                        ).fetchone()

                    matched_row: Optional[sqlite3.Row] = None
                    if before_row is not None and after_row is not None:
                        dist_before = abs(int(before_row["collected_at_ts"]) - target_ts)
                        dist_after = abs(int(after_row["collected_at_ts"]) - target_ts)
                        matched_row = before_row if dist_before <= dist_after else after_row
                    else:
                        matched_row = before_row if before_row is not None else after_row

                    if matched_row is not None:
                        matched_ts = int(matched_row["collected_at_ts"])
                        matched_count = int(matched_row["follower_count"])
                        baseline: Optional[int] = None
                        if day_before_last is not None:
                            baseline = int(day_before_last["follower_count"])
                        elif (
                            yesterday_first is not None
                            and int(matched_row["id"]) != int(yesterday_first["id"])
                        ):
                            baseline = int(yesterday_first["follower_count"])
                        if baseline is not None:
                            yesterday_same_time = matched_count - baseline
                            yesterday_same_time_at = datetime.fromtimestamp(
                                matched_ts, tz=timezone.utc
                            ).astimezone(BJT_TZ).strftime("%H:%M")

        yesterday_change: Optional[int] = None
        if yesterday_last is not None:
            y_end = int(yesterday_last["follower_count"])
            if day_before_last is not None:
                yesterday_change = y_end - int(day_before_last["follower_count"])
            elif (
                yesterday_first is not None
                and int(yesterday_first["id"]) != int(yesterday_last["id"])
            ):
                yesterday_change = y_end - int(yesterday_first["follower_count"])

        today_change: Optional[int] = None
        if today_last is not None:
            t_end = int(today_last["follower_count"])
            if yesterday_last is not None:
                today_change = t_end - int(yesterday_last["follower_count"])
            elif today_first is not None and int(today_first["id"]) != int(today_last["id"]):
                today_change = t_end - int(today_first["follower_count"])

        return {
            "yesterday": yesterday_change,
            "yesterday_same_time": yesterday_same_time,
            "yesterday_same_time_at": yesterday_same_time_at,
            "today": today_change,
            "yesterday_date": yesterday_date,
            "today_date": today_date,
            "timezone": "Asia/Shanghai",
        }

    @staticmethod
    def _downsample_video_history(
        rows: list[dict[str, Any]], max_points: int = 320
    ) -> list[dict[str, Any]]:
        """Downsample video history chronologically while preserving endpoints and extrema."""
        total = len(rows)
        if total <= max_points or max_points < 8:
            return rows

        tail_keep = min(32, total // 4)
        head_count = total - tail_keep
        target_head = max(16, max_points - tail_keep - 8)
        stride = head_count / target_head

        must_keep: set[int] = {0, total - 1}
        min_online_idx = 0
        max_online_idx = 0
        min_online = float("inf")
        max_online = float("-inf")
        first_play_idx: Optional[int] = None
        last_play_idx: Optional[int] = None
        min_play_idx: Optional[int] = None
        max_play_idx: Optional[int] = None
        min_play = float("inf")
        max_play = float("-inf")
        first_like_idx: Optional[int] = None
        last_like_idx: Optional[int] = None
        min_like_idx: Optional[int] = None
        max_like_idx: Optional[int] = None
        min_like = float("inf")
        max_like = float("-inf")

        for i, row in enumerate(rows):
            online = row.get("online_count")
            if online is not None:
                o_val = int(online)
                if o_val < min_online:
                    min_online = o_val
                    min_online_idx = i
                if o_val > max_online:
                    max_online = o_val
                    max_online_idx = i

            play = row.get("play_count")
            if play is not None:
                p_val = int(play)
                if first_play_idx is None:
                    first_play_idx = i
                last_play_idx = i
                if p_val < min_play:
                    min_play = p_val
                    min_play_idx = i
                if p_val > max_play:
                    max_play = p_val
                    max_play_idx = i

            like = row.get("like_count")
            if like is not None:
                l_val = int(like)
                if first_like_idx is None:
                    first_like_idx = i
                last_like_idx = i
                if l_val < min_like:
                    min_like = l_val
                    min_like_idx = i
                if l_val > max_like:
                    max_like = l_val
                    max_like_idx = i

        must_keep.add(min_online_idx)
        must_keep.add(max_online_idx)
        for idx in (
            first_play_idx,
            last_play_idx,
            min_play_idx,
            max_play_idx,
            first_like_idx,
            last_like_idx,
            min_like_idx,
            max_like_idx,
        ):
            if idx is not None:
                must_keep.add(idx)

        for k in range(target_head):
            must_keep.add(min(head_count - 1, round(k * stride)))
        for i in range(max(0, head_count), total):
            must_keep.add(i)

        return [rows[idx] for idx in sorted(must_keep)]

    def get_video_history_with_meta(
        self,
        bvid: str,
        limit: Optional[int] = 100,
        since_hours: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch merged video history using covering indexes on snapshot and pulse tables."""
        cutoff: Optional[int] = None
        if since_hours is not None and since_hours > 0:
            cutoff = int(time.time()) - since_hours * 3600

        snap_params: list[Any] = [bvid]
        pulse_params: list[Any] = [bvid]
        snap_where = "WHERE bvid = ?"
        pulse_where = "WHERE bvid = ?"
        if cutoff is not None:
            snap_where += " AND collected_at_ts >= ?"
            pulse_where += " AND collected_at_ts >= ?"
            snap_params.append(cutoff)
            pulse_params.append(cutoff)

        snap_sql = f"""
            SELECT collected_at_ts, online_count, online_raw,
                   play_count, like_count, snapshot_id
            FROM video_online_snapshots
            {snap_where}
            ORDER BY collected_at_ts DESC, snapshot_id DESC
        """
        pulse_sql = f"""
            SELECT collected_at_ts, online_count, online_raw,
                   play_count, like_count, id
            FROM video_pulse_snapshots
            {pulse_where}
            ORDER BY collected_at_ts DESC, id DESC
        """
        if limit is not None:
            snap_sql += " LIMIT ?"
            pulse_sql += " LIMIT ?"
            snap_params.append(limit)
            pulse_params.append(limit)

        with self.read_connection() as conn:
            snap_rows = conn.execute(snap_sql, snap_params).fetchall()
            pulse_rows = conn.execute(pulse_sql, pulse_params).fetchall()

        # Two-pointer merge of pre-sorted descending streams:
        # key = (collected_at_ts DESC, src_rank DESC [pulse=1, snap=0], sort_id DESC)
        merged_desc: list[tuple[int, int, str, Optional[int], Optional[int]]] = []
        i = 0
        j = 0
        n_snap = len(snap_rows)
        n_pulse = len(pulse_rows)
        cap = limit if limit is not None else (n_snap + n_pulse)

        while (i < n_snap or j < n_pulse) and len(merged_desc) < cap:
            if i < n_snap and j < n_pulse:
                sr = snap_rows[i]
                pr = pulse_rows[j]
                s_ts = int(sr[0] or 0)
                p_ts = int(pr[0] or 0)
                # pulse has src_rank=1 > snap src_rank=0, so pulse wins timestamp ties
                if p_ts >= s_ts:
                    merged_desc.append((p_ts, pr[1], pr[2], pr[3], pr[4]))
                    j += 1
                else:
                    merged_desc.append((s_ts, sr[1], sr[2], sr[3], sr[4]))
                    i += 1
            elif i < n_snap:
                sr = snap_rows[i]
                merged_desc.append((int(sr[0] or 0), sr[1], sr[2], sr[3], sr[4]))
                i += 1
            else:
                pr = pulse_rows[j]
                merged_desc.append((int(pr[0] or 0), pr[1], pr[2], pr[3], pr[4]))
                j += 1

        raw_total = len(merged_desc)
        items: list[dict[str, Any]] = [
            {
                "collected_at_ts": row[0],
                "online_count": row[1],
                "online_raw": row[2],
                "play_count": row[3],
                "like_count": row[4],
            }
            for row in reversed(merged_desc)
        ]

        downsampled = False
        if max_points is not None and max_points > 0 and len(items) > max_points:
            items = self._downsample_video_history(items, max_points=max_points)
            downsampled = True
        elif limit is not None and raw_total >= limit:
            downsampled = True

        # Format ISO timestamp strings only for the final retained points
        local_tz = datetime.now(timezone.utc).astimezone().tzinfo
        for item in items:
            ts_val = int(item["collected_at_ts"] or 0)
            item["collected_at"] = (
                datetime.fromtimestamp(ts_val, tz=local_tz).isoformat()
                if ts_val > 0
                else ""
            )

        return items, downsampled

    def get_video_history(
        self,
        bvid: str,
        limit: Optional[int] = 100,
        since_hours: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        items, _ = self.get_video_history_with_meta(
            bvid,
            limit=limit,
            since_hours=since_hours,
            max_points=max_points,
        )
        return items

    def prune_old_snapshots(self, retention_days: Optional[int] = None) -> dict[str, int]:
        """Delete account and pulse snapshots older than retention_days while keeping newest 2 account snapshots per UID."""
        with self.connection() as conn:
            if retention_days is None:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'snapshot_retention_days'"
                ).fetchone()
                raw_val = (
                    row["value"]
                    if row is not None
                    else DEFAULT_SETTINGS.get("snapshot_retention_days", "90")
                )
                resolved_days = _parse_retention_days(raw_val, default=90)
            else:
                resolved_days = max(0, int(retention_days))

            if resolved_days <= 0:
                return {"deleted_snapshots": 0, "retention_days": 0}

            cutoff_ts = int(time.time()) - resolved_days * 86400
            has_expired_acc = conn.execute(
                "SELECT 1 FROM account_snapshots WHERE collected_at_ts < ? LIMIT 1",
                (cutoff_ts,),
            ).fetchone()
            deleted_count = 0
            if has_expired_acc is not None:
                cur = conn.execute(
                    """
                    DELETE FROM account_snapshots
                    WHERE collected_at_ts < ?
                      AND id NOT IN (
                          SELECT id FROM (
                              SELECT id,
                                     ROW_NUMBER() OVER (
                                         PARTITION BY uid ORDER BY collected_at_ts DESC, id DESC
                                     ) AS rn
                              FROM account_snapshots
                          )
                          WHERE rn <= 2
                      )
                    """,
                    (cutoff_ts,),
                )
                deleted_count = max(0, int(cur.rowcount or 0))
            has_expired_pulse = conn.execute(
                "SELECT 1 FROM video_pulse_snapshots WHERE collected_at_ts < ? LIMIT 1",
                (cutoff_ts,),
            ).fetchone()
            if has_expired_pulse is not None:
                conn.execute(
                    "DELETE FROM video_pulse_snapshots WHERE collected_at_ts < ?",
                    (cutoff_ts,),
                )
            conn.execute("ANALYZE;")
            conn.execute("PRAGMA optimize;")
        return {"deleted_snapshots": deleted_count, "retention_days": resolved_days}

    def optimize(self) -> None:
        """Run SQLite query planner maintenance."""
        with self.connection() as conn:
            conn.execute("ANALYZE;")
            conn.execute("PRAGMA optimize;")


class _LazyDatabase:
    """Lazy proxy that defers default Database() creation until first attribute access."""

    def __init__(self) -> None:
        object.__setattr__(self, "_instance", None)
        object.__setattr__(self, "_lock", threading.Lock())

    def _get_instance(self) -> Database:
        inst = object.__getattribute__(self, "_instance")
        if inst is None:
            with object.__getattribute__(self, "_lock"):
                inst = object.__getattribute__(self, "_instance")
                if inst is None:
                    inst = Database()
                    object.__setattr__(self, "_instance", inst)
        return inst

    def __getattr__(self, name: str) -> Any:
        return getattr(self._get_instance(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._get_instance(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._get_instance(), name)

    def __repr__(self) -> str:
        inst = object.__getattribute__(self, "_instance")
        if inst is None:
            return "<_LazyDatabase uninitialized>"
        return repr(inst)


db: Database = _LazyDatabase()  # type: ignore[assignment]
