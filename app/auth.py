from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import TYPE_CHECKING, Any, Optional

from fastapi import HTTPException, Request, Response

from app.config import (
    ADMIN_SESSION_COOKIE_NAME,
    get_admin_session_ttl_seconds,
    get_env_admin_password,
    get_trust_proxy,
)

if TYPE_CHECKING:
    from app.database import Database

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password_hash(password: str, stored_hash: str) -> bool:
    """Verify a plaintext password against a stored PBKDF2-HMAC-SHA256 hash."""
    parts = (stored_hash or "").split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected_digest = bytes.fromhex(parts[3])
    except ValueError:
        return False
    if iterations < 10_000:
        return False
    actual_digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual_digest, expected_digest)


def get_admin_auth_info(db: Database) -> dict[str, Any]:
    """Resolve active admin password state (`env`, `db`, or `none`) and password fingerprint."""
    env_pw = get_env_admin_password()
    if env_pw:
        fp = hashlib.sha256(f"env:{env_pw}".encode("utf-8")).hexdigest()[:16]
        return {
            "enabled": True,
            "source": "env",
            "fingerprint": fp,
            "env_password": env_pw,
            "db_hash": "",
        }

    db_hash = db.get_setting("admin_password_hash", "").strip()
    if db_hash:
        fp = hashlib.sha256(f"db:{db_hash}".encode("utf-8")).hexdigest()[:16]
        return {
            "enabled": True,
            "source": "db",
            "fingerprint": fp,
            "env_password": "",
            "db_hash": db_hash,
        }

    return {
        "enabled": False,
        "source": "none",
        "fingerprint": "",
        "env_password": "",
        "db_hash": "",
    }


def verify_admin_password(db: Database, candidate: str) -> bool:
    """Verify candidate password against env or DB admin password."""
    info = get_admin_auth_info(db)
    if not info["enabled"]:
        return True
    if not candidate:
        return False
    if info["source"] == "env":
        return hmac.compare_digest(candidate, info["env_password"])
    return verify_password_hash(candidate, info["db_hash"])


def get_or_create_session_secret(db: Database) -> str:
    """Return persistent per-database HMAC secret for signing admin session cookies."""
    secret = db.get_setting("admin_session_secret", "").strip()
    if len(secret) >= 32:
        return secret
    secret = secrets.token_hex(32)
    db.update_settings({"admin_session_secret": secret})
    return secret


def _sign_session_payload(
    secret: str,
    exp_ts: int,
    nonce: str,
    fingerprint: str,
    mode: Optional[str] = None,
) -> str:
    if mode is not None:
        message = f"{exp_ts}.{nonce}.{mode}.{fingerprint}".encode("utf-8")
    else:
        message = f"{exp_ts}.{nonce}.{fingerprint}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def create_session_token(
    db: Database,
    ttl_seconds: Optional[int] = None,
    *,
    remember: bool = True,
) -> str:
    """Create a signed admin session token bound to the current password fingerprint."""
    info = get_admin_auth_info(db)
    ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else get_admin_session_ttl_seconds(remember=remember)
    )
    exp_ts = int(time.time()) + max(60, int(ttl))
    nonce = secrets.token_hex(8)
    mode = "r" if remember else "s"
    secret = get_or_create_session_secret(db)
    sig = _sign_session_payload(secret, exp_ts, nonce, info["fingerprint"], mode=mode)
    return f"{exp_ts}.{nonce}.{mode}.{sig}"


def parse_verified_session_token(db: Database, token: str) -> Optional[dict[str, Any]]:
    """Validate an admin session token and return its metadata (exp_ts, remaining, remember)."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) == 3:
        exp_str, nonce, sig = parts
        mode: Optional[str] = None
        remember = True
    elif len(parts) == 4:
        exp_str, nonce, mode, sig = parts
        if mode not in ("r", "s"):
            return None
        remember = mode == "r"
    else:
        return None

    try:
        exp_ts = int(exp_str)
    except ValueError:
        return None
    now_ts = int(time.time())
    if exp_ts <= now_ts or not nonce or not sig:
        return None

    info = get_admin_auth_info(db)
    if not info["enabled"]:
        return {
            "exp_ts": exp_ts,
            "remaining": exp_ts - now_ts,
            "remember": remember,
            "legacy": mode is None,
        }

    secret = db.get_setting("admin_session_secret", "").strip()
    if not secret:
        return None
    expected_sig = _sign_session_payload(secret, exp_ts, nonce, info["fingerprint"], mode=mode)
    if not hmac.compare_digest(sig, expected_sig):
        return None
    return {
        "exp_ts": exp_ts,
        "remaining": exp_ts - now_ts,
        "remember": remember,
        "legacy": mode is None,
    }


def verify_session_token(db: Database, token: str) -> bool:
    """Validate signature, expiration, and password fingerprint of an admin session token."""
    return parse_verified_session_token(db, token) is not None


def is_request_authenticated(request: Request) -> bool:
    """Return True if admin protection is disabled or request carries a valid admin cookie."""
    db: Database = request.app.state.db
    info = get_admin_auth_info(db)
    if not info["enabled"]:
        return True
    token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME, "")
    return verify_session_token(db, token)


def get_admin_status_payload(
    request: Request,
    *,
    force_unauthenticated: bool = False,
) -> dict[str, Any]:
    """Build public admin auth status dictionary for API responses."""
    db: Database = request.app.state.db
    info = get_admin_auth_info(db)
    if not info["enabled"]:
        authenticated = True
    elif force_unauthenticated:
        authenticated = False
    else:
        token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME, "")
        authenticated = verify_session_token(db, token)
    return {
        "enabled": info["enabled"],
        "authenticated": authenticated,
        "source": info["source"],
    }


def require_admin(request: Request) -> None:
    """FastAPI dependency enforcing admin verification when an admin password is configured."""
    if not is_request_authenticated(request):
        raise HTTPException(
            status_code=401,
            detail="需要管理员密码验证后才能执行此操作",
        )


def set_admin_session_cookie(
    response: Response,
    db: Database,
    request: Request,
    *,
    remember: bool = True,
) -> None:
    """Attach an HttpOnly signed session cookie after successful admin verification."""
    ttl = get_admin_session_ttl_seconds(remember=remember)
    token = create_session_token(db, ttl_seconds=ttl, remember=remember)
    forwarded_proto = ""
    if get_trust_proxy():
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    is_https = request.url.scheme == "https" or forwarded_proto == "https"
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl,
        expires=ttl,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )


def refresh_admin_session_cookie_if_needed(
    request: Request, response: Response, db: Database
) -> bool:
    """Slide-renew a valid admin session cookie when remaining lifetime drops below 75% of TTL."""
    info = get_admin_auth_info(db)
    if not info["enabled"]:
        return False
    token = request.cookies.get(ADMIN_SESSION_COOKIE_NAME, "")
    parsed = parse_verified_session_token(db, token)
    if parsed is None:
        return False
    remember = bool(parsed["remember"])
    target_ttl = get_admin_session_ttl_seconds(remember=remember)
    if parsed["legacy"] or parsed["remaining"] < int(target_ttl * 0.75):
        set_admin_session_cookie(response, db, request, remember=remember)
        return True
    return False


def clear_admin_session_cookie(response: Response) -> None:
    """Remove the admin session cookie."""
    response.delete_cookie(key=ADMIN_SESSION_COOKIE_NAME, path="/")


class AdminVerifyRateLimiter:
    """In-memory per-IP rate limiter for admin password verification attempts."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: int = 60,
        max_keys: int = 2048,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.max_keys = max(1, max_keys)
        self._failures: dict[str, list[float]] = {}

    @staticmethod
    def _client_key(request: Request) -> str:
        if get_trust_proxy():
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    def _prune_expired(self, cutoff: float) -> None:
        expired_keys: list[str] = []
        for k, timestamps in self._failures.items():
            valid = [ts for ts in timestamps if ts > cutoff]
            if valid:
                self._failures[k] = valid
            else:
                expired_keys.append(k)
        for k in expired_keys:
            self._failures.pop(k, None)

    def check_allowed(self, request: Request) -> None:
        key = self._client_key(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [ts for ts in self._failures.get(key, []) if ts > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        if len(recent) >= self.max_failures:
            raise HTTPException(
                status_code=429,
                detail="密码错误次数过多，请 60 秒后再试",
            )

    def record_failure(self, request: Request) -> None:
        key = self._client_key(request)
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [ts for ts in self._failures.get(key, []) if ts > cutoff]
        if len(self._failures) >= self.max_keys:
            self._prune_expired(cutoff)
            target_cap = self.max_keys if key in self._failures else self.max_keys - 1
            while len(self._failures) > target_cap and self._failures:
                oldest_key = min(
                    self._failures,
                    key=lambda k: (
                        (self._failures[k][-1] if self._failures[k] else float("-inf")),
                    )
                    if k != key
                    else (float("inf"),),
                )
                if oldest_key == key:
                    break
                self._failures.pop(oldest_key, None)
        recent.append(now)
        self._failures[key] = recent

    def reset(self, request: Request) -> None:
        key = self._client_key(request)
        self._failures.pop(key, None)
