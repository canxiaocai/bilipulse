import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BILI_DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("BILI_DB_PATH", str(DATA_DIR / "bili_monitor.db")))
STATIC_DIR = BASE_DIR / "static"

HOST = os.getenv("BILI_HOST", "0.0.0.0")
PORT = int(os.getenv("BILI_PORT", "8000"))

ADMIN_SESSION_COOKIE_NAME = "bilipulse_admin_session"


def get_env_admin_password() -> str:
    """Return the admin password from BILI_ADMIN_PASSWORD if configured, else empty string."""
    raw = os.getenv("BILI_ADMIN_PASSWORD", "").rstrip("\r\n")
    return raw if raw.strip() else ""


def get_default_admin_password() -> str:
    """Return optional initial admin password seeded into new databases from BILI_DEFAULT_ADMIN_PASSWORD."""
    raw = os.getenv("BILI_DEFAULT_ADMIN_PASSWORD", "").rstrip("\r\n")
    return raw if raw.strip() else ""


def get_admin_session_ttl_seconds(remember: bool = True) -> int:
    """Return admin verification cookie lifetime in seconds (default 30 days, or 12h when remember=False)."""
    try:
        hours = int(os.getenv("BILI_ADMIN_SESSION_HOURS", "720") or "720")
    except ValueError:
        hours = 720
    full_ttl = max(300, hours * 3600)
    if not remember:
        return max(300, min(full_ttl, 12 * 3600))
    return full_ttl


def get_earnings_owner_uid() -> str:
    """Return the Bilibili UID allowed to access the personal earnings dashboard."""
    return os.getenv("BILI_EARNINGS_UID", "3690980052044072").strip()


def get_trust_proxy() -> bool:
    """Return True iff reverse-proxy X-Forwarded-* headers are explicitly trusted."""
    return os.getenv("BILI_TRUST_PROXY", "").strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_SETTINGS = {
    "uid": os.getenv("BILI_DEFAULT_UID", ""),
    "cookie": os.getenv("BILI_DEFAULT_COOKIE", ""),
    "interval_minutes": os.getenv("BILI_DEFAULT_INTERVAL", "5"),
    "enabled": "true",
    "request_delay_ms": os.getenv("BILI_REQUEST_DELAY_MS", "200"),
    "video_sync_interval_minutes": os.getenv("BILI_VIDEO_SYNC_INTERVAL", "30"),
    "snapshot_retention_days": os.getenv("BILI_SNAPSHOT_RETENTION_DAYS", "90"),
    "account_name": "",
    "account_face": "",
}
