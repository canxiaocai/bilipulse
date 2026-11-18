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
from app.config import DB_PATH, DEFAULT_SETTINGS, get_default_admin_password

ONLINE_COUNT_RULE = "minus_1_per_cid_v1"
BJT_TZ = timezone(timedelta(hours=8))
_SQLITE_CHUNK_SIZE = 500


def _adjusted_online_from_raw(raw: str, fallback_count: int) -> int:
    """Recompute adjusted online count (-1 per CID, floored at 0) from raw API text."""
    text = (raw or "").strip()
    if not text:
        return max(0, int(fallback_count) - 1)
    parts = [p.strip() for p in text.split("+") if p.strip()]
    if not parts:
        return max(0, int(fallback_count) - 1)
    return sum(max(0, parse_online_count(part) - 1) for part in parts)


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
        self.init_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=30000;")
        try:
            yield conn
            conn.commit()
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

                CREATE INDEX IF NOT EXISTS idx_snapshots_uid_ts
                    ON account_snapshots (uid, collected_at_ts DESC);

                CREATE TABLE IF NOT EXISTS videos (
                    bvid TEXT PRIMARY KEY,
                    uid TEXT NOT NULL,
                    aid INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    pic TEXT NOT NULL DEFAULT '',
                    pubdate INTEGER NOT NULL DEFAULT 0,
                    duration INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    like_count INTEGER NOT NULL DEFAULT 0,
                    coin_count INTEGER NOT NULL DEFAULT 0,
                    stats_synced_at TEXT,
                    cids_json TEXT NOT NULL DEFAULT '[]',
                    last_online_count INTEGER NOT NULL DEFAULT 0,
                    last_online_raw TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_videos_uid_pubdate
                    ON videos (uid, pubdate DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_uid_online_pubdate
                    ON videos (uid, last_online_count DESC, pubdate DESC);

                CREATE TABLE IF NOT EXISTS video_online_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
                    bvid TEXT NOT NULL,
                    online_count INTEGER NOT NULL,
                    online_raw TEXT NOT NULL DEFAULT '',
                    play_count INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_video_snapshots_snapshot
                    ON video_online_snapshots (snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_video_snapshots_bvid
                    ON video_online_snapshots (bvid, id DESC);

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
            video_snap_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(video_online_snapshots)").fetchall()
            }
            if "play_count" not in video_snap_cols:
                conn.execute("ALTER TABLE video_online_snapshots ADD COLUMN play_count INTEGER")
            now = datetime.now(timezone.utc).astimezone().isoformat()
            conn.executemany(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                [(key, str(default_val), now) for key, default_val in DEFAULT_SETTINGS.items()],
            )
            self._migrate_online_count_rule(conn, now)
            self._migrate_initial_account(conn, now)
            self._migrate_global_cookie(conn, now)
            self._seed_default_admin_password(conn, now)

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
        """One-time migration subtracting 1 baseline viewer per CID on legacy snapshots."""
        rule_row = conn.execute(
            "SELECT value FROM settings WHERE key = 'online_count_rule'"
        ).fetchone()
        if rule_row and rule_row["value"] == ONLINE_COUNT_RULE:
            return

        snap_cnt = conn.execute("SELECT COUNT(*) AS cnt FROM account_snapshots").fetchone()["cnt"]
        if snap_cnt > 0:
            video_snap_rows = conn.execute(
                "SELECT id, online_count, online_raw FROM video_online_snapshots"
            ).fetchall()
            for r in video_snap_rows:
                adjusted = _adjusted_online_from_raw(r["online_raw"], r["online_count"])
                conn.execute(
                    "UPDATE video_online_snapshots SET online_count = ? WHERE id = ?",
                    (adjusted, r["id"]),
                )

            video_rows = conn.execute(
                "SELECT bvid, last_online_count, last_online_raw, last_checked_at FROM videos"
            ).fetchall()
            for r in video_rows:
                if r["last_checked_at"] is None and not r["last_online_raw"] and r["last_online_count"] == 0:
                    continue
                adjusted = _adjusted_online_from_raw(r["last_online_raw"], r["last_online_count"])
                conn.execute(
                    "UPDATE videos SET last_online_count = ? WHERE bvid = ?",
                    (adjusted, r["bvid"]),
                )

            conn.execute(
                """
                UPDATE account_snapshots
                SET online_viewers_total = (
                    SELECT COALESCE(SUM(v.online_count), 0)
                    FROM video_online_snapshots v
                    WHERE v.snapshot_id = account_snapshots.id
                )
                WHERE EXISTS (
                    SELECT 1 FROM video_online_snapshots v
                    WHERE v.snapshot_id = account_snapshots.id
                )
                """
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
        with self.connection() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result = dict(DEFAULT_SETTINGS)
        for row in rows:
            result[row["key"]] = row["value"]
        return result

    def get_public_settings(self, uid: Optional[str] = None) -> dict[str, Any]:
        with self.connection() as conn:
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
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def get_effective_cookie(self, uid: Optional[str] = None) -> str:
        """Return account-specific cookie override if set, otherwise the global cookie."""
        clean_uid = str(uid or "").strip()
        with self.connection() as conn:
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
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM monitored_accounts WHERE uid = ?", (clean_uid,)
            ).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM monitored_accounts ORDER BY sort_order ASC, created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_enabled_accounts(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
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
        with self.connection() as conn:
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
            snap_rows = conn.execute(
                """
                SELECT *
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY uid ORDER BY collected_at_ts DESC, id DESC
                           ) AS rn
                    FROM account_snapshots
                )
                WHERE rn <= 2
                ORDER BY uid, rn ASC
                """
            ).fetchall()
            for r in snap_rows:
                snaps_by_uid.setdefault(r["uid"], []).append(self._row_to_snapshot(r))

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
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            fields: list[str] = []
            params: list[Any] = []
            if account_name is not None:
                fields.append("account_name = ?")
                params.append(account_name.strip())
            if account_face is not None:
                fields.append("account_face = ?")
                params.append(account_face.strip())
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
                if account_name is not None:
                    self._upsert_setting(conn, "account_name", account_name.strip(), now)
                if account_face is not None:
                    self._upsert_setting(conn, "account_face", account_face.strip(), now)

    def set_active_account(self, uid: str) -> Optional[dict[str, Any]]:
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
        return next((a for a in self.get_accounts_summary() if a["uid"] == clean_uid), None)

    @staticmethod
    def _delete_video_snapshots_by_bvids(conn: sqlite3.Connection, bvids: list[str]) -> None:
        for i in range(0, len(bvids), _SQLITE_CHUNK_SIZE):
            chunk = bvids[i : i + _SQLITE_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM video_online_snapshots WHERE bvid IN ({placeholders})",
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
                if bvids:
                    self._delete_video_snapshots_by_bvids(conn, bvids)
                conn.execute("DELETE FROM videos WHERE uid = ?", (clean_uid,))
                conn.execute(
                    "DELETE FROM settings WHERE key IN (?, ?, ?)",
                    (
                        f"last_video_sync_ts_{clean_uid}",
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

    def upsert_videos(self, uid: str, videos: list[dict[str, Any]]) -> None:
        if not videos:
            return
        now = datetime.now(timezone.utc).astimezone().isoformat()
        params = []
        for v in videos:
            has_stats = (
                ("like_count" in v and v["like_count"] is not None)
                or ("coin_count" in v and v["coin_count"] is not None)
            )
            like_val = max(0, int(v.get("like_count") or 0)) if has_stats else 0
            coin_val = max(0, int(v.get("coin_count") or 0)) if has_stats else 0
            synced_val = v.get("stats_synced_at") or (now if has_stats else None)
            params.append(
                (
                    v["bvid"],
                    uid,
                    int(v["aid"]),
                    v["title"],
                    v.get("pic", ""),
                    int(v.get("pubdate", 0)),
                    int(v.get("duration", 0)),
                    int(v.get("play_count", 0)),
                    like_val,
                    coin_val,
                    synced_val,
                    json.dumps(v.get("cids", []), ensure_ascii=False),
                    now,
                )
            )
        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO videos (
                    bvid, uid, aid, title, pic, pubdate, duration, play_count,
                    like_count, coin_count, stats_synced_at, cids_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    uid=excluded.uid,
                    aid=excluded.aid,
                    title=excluded.title,
                    pic=excluded.pic,
                    pubdate=excluded.pubdate,
                    duration=CASE
                        WHEN excluded.duration > 0 THEN excluded.duration
                        ELSE videos.duration
                    END,
                    play_count=excluded.play_count,
                    like_count=CASE
                        WHEN excluded.stats_synced_at IS NOT NULL THEN excluded.like_count
                        ELSE videos.like_count
                    END,
                    coin_count=CASE
                        WHEN excluded.stats_synced_at IS NOT NULL THEN excluded.coin_count
                        ELSE videos.coin_count
                    END,
                    stats_synced_at=COALESCE(excluded.stats_synced_at, videos.stats_synced_at),
                    cids_json=CASE
                        WHEN excluded.cids_json != '[]' THEN excluded.cids_json
                        ELSE videos.cids_json
                    END,
                    updated_at=excluded.updated_at
                """,
                params,
            )

    def update_video_stats_batch(self, items: list[dict[str, Any]]) -> None:
        """Persist refreshed play, like, and coin counts for a batch of videos."""
        if not items:
            return
        now = datetime.now(timezone.utc).astimezone().isoformat()
        params = [
            (
                max(0, int(item["play_count"])),
                max(0, int(item["like_count"])),
                max(0, int(item["coin_count"])),
                now,
                now,
                item["bvid"],
            )
            for item in items
        ]
        with self.connection() as conn:
            conn.executemany(
                """
                UPDATE videos
                SET play_count = ?,
                    like_count = ?,
                    coin_count = ?,
                    stats_synced_at = ?,
                    updated_at = ?
                WHERE bvid = ?
                """,
                params,
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

    def get_videos(self, uid: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT bvid, uid, aid, title, pic, pubdate, duration, play_count,
                       like_count, coin_count, stats_synced_at,
                       cids_json, last_online_count, last_online_raw, last_checked_at, updated_at
                FROM videos
                WHERE uid = ?
                ORDER BY last_online_count DESC, pubdate DESC
                """,
                (uid,),
            ).fetchall()
        items = []
        for r in rows:
            cids = json.loads(r["cids_json"] or "[]")
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
                    "like_count": r["like_count"],
                    "coin_count": r["coin_count"],
                    "stats_synced_at": r["stats_synced_at"],
                    "cids": cids,
                    "cid_count": max(1, len(cids)),
                    "last_online_count": r["last_online_count"],
                    "last_online_raw": r["last_online_raw"],
                    "last_checked_at": r["last_checked_at"],
                    "updated_at": r["updated_at"],
                }
            )
        return items

    def update_video_cids(self, bvid: str, cids: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat()
        with self.connection() as conn:
            conn.execute(
                "UPDATE videos SET cids_json = ?, updated_at = ? WHERE bvid = ?",
                (json.dumps(cids, ensure_ascii=False), now, bvid),
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
                missing_bvids = list(
                    dict.fromkeys(
                        item["bvid"]
                        for item in video_online_items
                        if item.get("play_count") is None
                    )
                )
                fallback_plays: dict[str, int] = {}
                for i in range(0, len(missing_bvids), _SQLITE_CHUNK_SIZE):
                    chunk = missing_bvids[i : i + _SQLITE_CHUNK_SIZE]
                    placeholders = ",".join("?" for _ in chunk)
                    v_rows = conn.execute(
                        f"SELECT bvid, play_count FROM videos WHERE bvid IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for v_row in v_rows:
                        if v_row["play_count"] is not None:
                            fallback_plays[v_row["bvid"]] = int(v_row["play_count"])

                snapshot_rows: list[tuple[int, str, int, str, Optional[int]]] = []
                video_updates: list[tuple[int, str, str, str]] = []
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
                    snapshot_rows.append(
                        (snapshot_id, bvid, online_count, online_raw, play_count_val)
                    )
                    video_updates.append((online_count, online_raw, collected_iso, bvid))

                conn.executemany(
                    """
                    INSERT INTO video_online_snapshots (
                        snapshot_id, bvid, online_count, online_raw, play_count
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    snapshot_rows,
                )
                conn.executemany(
                    """
                    UPDATE videos
                    SET last_online_count = ?, last_online_raw = ?, last_checked_at = ?
                    WHERE bvid = ?
                    """,
                    video_updates,
                )
        return snapshot_id

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

        with self.connection() as conn:
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

    def get_recent_snapshots(self, uid: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.connection() as conn:
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
        with self.connection() as conn:
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
                "today": None,
                "yesterday_date": yesterday_date,
                "today_date": today_date,
                "timezone": "Asia/Shanghai",
            }

        t_day_before = int(day_before_start.timestamp())
        t_yesterday = int(yesterday_start.timestamp())
        t_today = int(today_start.timestamp())
        t_tomorrow = int(tomorrow_start.timestamp())

        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT collected_at_ts, follower_count
                FROM account_snapshots
                WHERE uid = ?
                  AND follower_count IS NOT NULL
                  AND collected_at_ts >= ?
                  AND collected_at_ts < ?
                ORDER BY collected_at_ts ASC, id ASC
                """,
                (uid, t_day_before, t_tomorrow),
            ).fetchall()

        day_before_counts: list[int] = []
        yesterday_counts: list[int] = []
        today_counts: list[int] = []
        for row in rows:
            ts = int(row["collected_at_ts"])
            count = int(row["follower_count"])
            if ts < t_yesterday:
                day_before_counts.append(count)
            elif ts < t_today:
                yesterday_counts.append(count)
            else:
                today_counts.append(count)

        return {
            "yesterday": self._compute_day_follower_change(yesterday_counts, day_before_counts),
            "today": self._compute_day_follower_change(today_counts, yesterday_counts),
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
        target_head = max(16, max_points - tail_keep)
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

        must_keep.add(min_online_idx)
        must_keep.add(max_online_idx)
        for idx in (first_play_idx, last_play_idx, min_play_idx, max_play_idx):
            if idx is not None:
                must_keep.add(idx)

        for k in range(target_head):
            must_keep.add(min(head_count - 1, round(k * stride)))
        for i in range(max(0, head_count), total):
            must_keep.add(i)

        return [rows[idx] for idx in sorted(must_keep)]

    def get_video_history(
        self,
        bvid: str,
        limit: Optional[int] = 100,
        since_hours: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [bvid]
        where = "WHERE v.bvid = ?"
        if since_hours is not None and since_hours > 0:
            cutoff = int(time.time()) - since_hours * 3600
            where += " AND s.collected_at_ts >= ?"
            params.append(cutoff)

        sql = f"""
            SELECT s.collected_at, s.collected_at_ts, v.online_count, v.online_raw, v.play_count
            FROM video_online_snapshots v
            JOIN account_snapshots s ON s.id = v.snapshot_id
            {where}
            ORDER BY s.collected_at_ts DESC, s.id DESC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [dict(r) for r in reversed(rows)]
        if max_points is not None and max_points > 0 and len(items) > max_points:
            return self._downsample_video_history(items, max_points=max_points)
        return items

    def prune_old_snapshots(self, retention_days: Optional[int] = None) -> dict[str, int]:
        """Delete account snapshots older than retention_days while keeping the newest 2 per UID."""
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
            conn.execute("PRAGMA optimize;")
        return {"deleted_snapshots": deleted_count, "retention_days": resolved_days}

    def optimize(self) -> None:
        """Run SQLite query planner maintenance."""
        with self.connection() as conn:
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
