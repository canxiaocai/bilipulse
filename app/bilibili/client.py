import asyncio
import hashlib
import hmac
import logging
import threading
import time
import urllib.parse
from collections import OrderedDict
from functools import reduce
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _safe_nonneg_int(raw: Any) -> int:
    """Parse non-negative integer counts safely, returning 0 for invalid values."""
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return 0


def parse_online_count(raw: Any) -> int:
    """Parse Bilibili online viewer text like '11', '100+', '1.2万+' into an integer."""
    if raw is None:
        return 0
    text = str(raw).strip()
    if not text or text == "-":
        return 0
    text = text.rstrip("+").strip()
    try:
        if text.endswith("万"):
            return int(round(float(text[:-1]) * 10_000))
        if text.endswith("亿"):
            return int(round(float(text[:-1]) * 100_000_000))
        return int(float(text))
    except ValueError:
        return 0


def parse_duration_seconds(raw: Any) -> int:
    """Parse Bilibili video duration ('MM:SS', 'HH:MM:SS', or numeric seconds) into seconds."""
    if raw is None or isinstance(raw, bool):
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(0, int(raw))
        except (ValueError, OverflowError):
            return 0
    text = str(raw).strip()
    if not text or text == "-":
        return 0
    if ":" in text:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            return 0
        try:
            nums = [int(p.strip()) for p in parts]
        except ValueError:
            return 0
        if any(p.strip() == "" or n < 0 for p, n in zip(parts, nums)):
            return 0
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    try:
        return max(0, int(float(text)))
    except (ValueError, OverflowError):
        return 0


def parse_cookie_header(cookie_str: str) -> dict[str, str]:
    """Parse raw Cookie header string into a dict."""
    cookies: dict[str, str] = {}
    if not cookie_str:
        return cookies
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


class BilibiliAPIError(RuntimeError):
    """Raised when Bilibili API returns a non-zero business error or HTTP error."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class BilibiliClient:
    """Async Bilibili HTTP client supporting optional cookies, buvid3/bili_ticket, and WBI signing."""

    SESSION_TOKEN_TTL: int = 21600
    WBI_KEYS_TTL: int = 3600
    MAX_CREDENTIAL_CACHE_SIZE: int = 64
    _CREDENTIAL_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
    _CACHE_LOCK: threading.Lock = threading.Lock()

    def __init__(self, cookie: str = "", timeout: float = 15.0) -> None:
        self.raw_cookie = cookie.strip()
        self._cookie_key = hashlib.sha256(self.raw_cookie.encode("utf-8")).hexdigest()
        self.timeout = timeout
        self._wbi_keys: Optional[tuple[str, str]] = None
        self._wbi_fetched_at: float = 0.0
        self._session_bootstrapped = False
        self._cookies = parse_cookie_header(self.raw_cookie)
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": DEFAULT_UA,
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            cookies=self._cookies,
            timeout=timeout,
            follow_redirects=True,
        )

    @classmethod
    def clear_credential_cache(cls) -> None:
        """Reset all in-memory cached session tokens and WBI keys."""
        with cls._CACHE_LOCK:
            cls._CREDENTIAL_CACHE.clear()

    @classmethod
    def _get_or_create_cache_entry_locked(cls, cookie_key: str) -> dict[str, Any]:
        entry = cls._CREDENTIAL_CACHE.get(cookie_key)
        if entry is not None:
            cls._CREDENTIAL_CACHE.move_to_end(cookie_key)
            return entry
        entry = {}
        cls._CREDENTIAL_CACHE[cookie_key] = entry
        while len(cls._CREDENTIAL_CACHE) > cls.MAX_CREDENTIAL_CACHE_SIZE:
            cls._CREDENTIAL_CACHE.popitem(last=False)
        return entry

    @classmethod
    def _get_or_create_cache_entry(cls, cookie_key: str) -> dict[str, Any]:
        with cls._CACHE_LOCK:
            return cls._get_or_create_cache_entry_locked(cookie_key)

    def _invalidate_wbi_keys(self) -> None:
        self._wbi_keys = None
        self._wbi_fetched_at = 0.0
        with self._CACHE_LOCK:
            entry = self._CREDENTIAL_CACHE.get(self._cookie_key)
            if entry is not None:
                entry.pop("wbi_keys", None)
                entry.pop("wbi_fetched_at", None)

    async def __aenter__(self) -> "BilibiliClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def ensure_session_tokens(self) -> None:
        """Fetch buvid3/buvid4/bili_ticket if missing so Bilibili endpoints trust the session."""
        if self._session_bootstrapped:
            return

        need_buvid3 = "buvid3" not in self._cookies
        need_ticket = "bili_ticket" not in self._cookies
        if not need_buvid3 and not need_ticket:
            self._session_bootstrapped = True
            return

        now = time.time()
        cached_tokens: Optional[dict[str, str]] = None
        with self._CACHE_LOCK:
            entry = self._CREDENTIAL_CACHE.get(self._cookie_key)
            if entry is not None:
                raw_cached = entry.get("session_tokens")
                fetched_at = entry.get("session_fetched_at", 0.0)
                if raw_cached and (now - fetched_at) < self.SESSION_TOKEN_TTL:
                    self._CREDENTIAL_CACHE.move_to_end(self._cookie_key)
                    cached_tokens = dict(raw_cached)

        if cached_tokens:
            for name, val in cached_tokens.items():
                if val and name not in self._cookies:
                    self._client.cookies.set(name, val, domain=".bilibili.com")
            need_buvid3 = need_buvid3 and ("buvid3" not in cached_tokens)
            need_ticket = need_ticket and ("bili_ticket" not in cached_tokens)
            if not need_buvid3 and not need_ticket:
                self._session_bootstrapped = True
                return

        self._session_bootstrapped = True
        fetched_tokens: dict[str, str] = {}

        if need_buvid3:
            try:
                resp = await self._client.get("https://api.bilibili.com/x/frontend/finger/spi")
                if resp.status_code == 200:
                    data = resp.json().get("data") or {}
                    if data.get("b_3"):
                        b3 = str(data["b_3"])
                        self._client.cookies.set("buvid3", b3, domain=".bilibili.com")
                        fetched_tokens["buvid3"] = b3
                    if data.get("b_4"):
                        b4 = str(data["b_4"])
                        self._client.cookies.set("buvid4", b4, domain=".bilibili.com")
                        fetched_tokens["buvid4"] = b4
            except Exception as exc:
                logger.debug("Failed to fetch spi buvid3: %s", exc)

        if need_ticket:
            try:
                ts = int(time.time())
                hexsign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
                resp = await self._client.post(
                    "https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                    params={
                        "key_id": "ec02",
                        "hexsign": hexsign,
                        "context[ts]": str(ts),
                        "csrf": self._cookies.get("bili_jct", ""),
                    },
                )
                if resp.status_code == 200:
                    ticket = (resp.json().get("data") or {}).get("ticket")
                    if ticket:
                        ticket_str = str(ticket)
                        ts_str = str(ts)
                        self._client.cookies.set("bili_ticket", ticket_str, domain=".bilibili.com")
                        self._client.cookies.set("b_nut", ts_str, domain=".bilibili.com")
                        fetched_tokens["bili_ticket"] = ticket_str
                        fetched_tokens["b_nut"] = ts_str
            except Exception as exc:
                logger.debug("Failed to fetch bili_ticket: %s", exc)

        if fetched_tokens:
            now_save = time.time()
            with self._CACHE_LOCK:
                cache_entry = self._get_or_create_cache_entry_locked(self._cookie_key)
                merged_tokens: dict[str, str] = {}
                existing_tokens = cache_entry.get("session_tokens")
                existing_at = cache_entry.get("session_fetched_at", 0.0)
                if existing_tokens and (now_save - existing_at) < self.SESSION_TOKEN_TTL:
                    merged_tokens.update(existing_tokens)
                    saved_at = existing_at
                elif cached_tokens:
                    merged_tokens.update(cached_tokens)
                    saved_at = now_save
                else:
                    saved_at = now_save
                merged_tokens.update(fetched_tokens)
                cache_entry["session_tokens"] = merged_tokens
                cache_entry["session_fetched_at"] = saved_at

    async def _get_wbi_keys(self) -> tuple[str, str]:
        now = time.time()
        if self._wbi_keys and (now - self._wbi_fetched_at) < self.WBI_KEYS_TTL:
            return self._wbi_keys

        with self._CACHE_LOCK:
            entry = self._CREDENTIAL_CACHE.get(self._cookie_key)
            if entry is not None:
                cached_keys = entry.get("wbi_keys")
                cached_at = entry.get("wbi_fetched_at", 0.0)
                if cached_keys and (now - cached_at) < self.WBI_KEYS_TTL:
                    self._CREDENTIAL_CACHE.move_to_end(self._cookie_key)
                    self._wbi_keys = cached_keys
                    self._wbi_fetched_at = cached_at
                    return self._wbi_keys

        resp = await self._client.get("https://api.bilibili.com/x/web-interface/nav")
        resp.raise_for_status()
        payload = resp.json()
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_url = wbi_img.get("img_url", "")
        sub_url = wbi_img.get("sub_url", "")
        if not img_url or not sub_url:
            raise BilibiliAPIError("Unable to retrieve WBI keys from /x/web-interface/nav")

        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        keys = (img_key, sub_key)
        self._wbi_keys = keys
        self._wbi_fetched_at = now
        with self._CACHE_LOCK:
            cache_entry = self._get_or_create_cache_entry_locked(self._cookie_key)
            cache_entry["wbi_keys"] = keys
            cache_entry["wbi_fetched_at"] = now
        return self._wbi_keys

    @staticmethod
    def sign_wbi_params(params: dict[str, Any], img_key: str, sub_key: str) -> str:
        """Compute WBI signature and return the exact encoded query string."""
        raw_key = img_key + sub_key
        mixin_key = reduce(lambda acc, i: acc + raw_key[i], MIXIN_KEY_ENC_TAB, "")[:32]
        signed = {**params, "wts": round(time.time())}
        filtered = {
            str(k): "".join(ch for ch in str(v) if ch not in "!'()*")
            for k, v in sorted(signed.items())
        }
        query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in filtered.items()
        )
        w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return f"{query}&w_rid={w_rid}"

    async def _request_json(
        self,
        url: str,
        params: Optional[dict[str, Any]] = None,
        referer: Optional[str] = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        headers = {}
        if referer:
            headers["Referer"] = referer

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.get(url, params=params, headers=headers)
                if resp.status_code == 412:
                    raise BilibiliAPIError(f"HTTP 412 from {url}", code=412)
                if resp.status_code == 429:
                    if attempt < retries:
                        await asyncio.sleep(1.5 * (2 ** attempt))
                        continue
                    raise BilibiliAPIError(f"HTTP 429 from {url}", code=429)
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code")
                if "/wbi/" in url and code in (-403, -412):
                    self._invalidate_wbi_keys()
                if code == -799 and attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return data
            except BilibiliAPIError:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def get_relation_stat(self, uid: str) -> dict[str, Any]:
        """Get follower and following counts for the target UID."""
        data = await self._request_json(
            "https://api.bilibili.com/x/relation/stat",
            params={"vmid": uid},
            referer=f"https://space.bilibili.com/{uid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"relation/stat error {data.get('code')}: {data.get('message')}",
                code=data.get("code"),
            )
        info = data.get("data") or {}
        return {
            "follower": int(info.get("follower", 0)),
            "following": int(info.get("following", 0)),
        }

    async def get_account_card(self, uid: str) -> dict[str, Any]:
        """Get account profile name, avatar, and follower count."""
        data = await self._request_json(
            "https://api.bilibili.com/x/web-interface/card",
            params={"mid": uid},
            referer=f"https://space.bilibili.com/{uid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"web-interface/card error {data.get('code')}: {data.get('message')}",
                code=data.get("code"),
            )
        payload = data.get("data") or {}
        card = payload.get("card") or {}
        return {
            "name": str(card.get("name", "")),
            "face": str(card.get("face", "")),
            "follower": int(payload.get("follower", 0)),
            "archive_count": int(payload.get("archive_count", 0)),
        }

    async def _fetch_video_view_data(self, bvid: str) -> dict[str, Any]:
        """Fetch raw data payload from /x/web-interface/view for a video."""
        data = await self._request_json(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"web-interface/view error {data.get('code')} for {bvid}: {data.get('message')}",
                code=data.get("code"),
            )
        return data.get("data") or {}

    async def get_video_owner_mid(self, bvid: str) -> Optional[str]:
        """Fetch the primary UP主 mid for a video via /x/web-interface/view."""
        payload = await self._fetch_video_view_data(bvid)
        owner = payload.get("owner") or {}
        owner_mid = owner.get("mid")
        if owner_mid is None or str(owner_mid).strip() == "":
            return None
        return str(owner_mid).strip()

    async def get_video_stat(self, bvid: str) -> dict[str, int]:
        """Fetch play, like, and coin counts for a video via /x/web-interface/view."""
        payload = await self._fetch_video_view_data(bvid)
        stat = payload.get("stat") or {}
        return {
            "play_count": _safe_nonneg_int(stat.get("view")),
            "like_count": _safe_nonneg_int(stat.get("like")),
            "coin_count": _safe_nonneg_int(stat.get("coin")),
        }

    async def _get_videos_page_series_raw(
        self, uid: str, pn: int = 1, ps: int = 100
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch one page from /x/series/recArchivesByKeywords, filtered to UP-owned videos."""
        await self.ensure_session_tokens()
        data = await self._request_json(
            "https://api.bilibili.com/x/series/recArchivesByKeywords",
            params={"mid": uid, "keywords": "", "ps": ps, "pn": pn},
            referer=f"https://space.bilibili.com/{uid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"recArchivesByKeywords error {data.get('code')}: {data.get('message')}",
                code=data.get("code"),
            )
        inner = data.get("data") or {}
        total = int((inner.get("page") or {}).get("total") or 0)
        archives = inner.get("archives") or []
        target_uid = str(uid).strip()
        videos: list[dict[str, Any]] = []
        for item in archives:
            raw_up_mid = item.get("upMid")
            if raw_up_mid is not None and str(raw_up_mid).strip() != "":
                up_mid = str(raw_up_mid).strip()
                if up_mid != target_uid:
                    continue
            else:
                up_mid = target_uid

            videos.append(
                {
                    "bvid": item["bvid"],
                    "aid": int(item["aid"]),
                    "title": str(item.get("title", "")),
                    "pic": str(item.get("pic", "")),
                    "pubdate": int(item.get("pubdate", 0)),
                    "duration": int(item.get("duration", 0)),
                    "play_count": int((item.get("stat") or {}).get("view", 0)),
                    "up_mid": up_mid,
                }
            )
        return videos, len(archives), total

    async def get_videos_page_series(
        self, uid: str, pn: int = 1, ps: int = 100
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch uploaded videos using /x/series/recArchivesByKeywords (works without login)."""
        videos, _, total = await self._get_videos_page_series_raw(uid, pn=pn, ps=ps)
        return videos, total

    async def _get_videos_page_wbi_raw(
        self, uid: str, pn: int = 1, ps: int = 50
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch one page from /x/space/wbi/arc/search, filtered to UP-owned videos."""
        await self.ensure_session_tokens()
        img_key, sub_key = await self._get_wbi_keys()
        query = self.sign_wbi_params(
            {"mid": uid, "ps": ps, "pn": pn, "order": "pubdate", "tid": 0, "keyword": ""},
            img_key,
            sub_key,
        )
        url = f"https://api.bilibili.com/x/space/wbi/arc/search?{query}"
        data = await self._request_json(url, referer=f"https://space.bilibili.com/{uid}/video")
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"space/wbi/arc/search error {data.get('code')}: {data.get('message')}",
                code=data.get("code"),
            )
        inner = data.get("data") or {}
        total = int((inner.get("page") or {}).get("count") or 0)
        vlist = ((inner.get("list") or {}).get("vlist")) or []
        target_uid = str(uid).strip()
        videos: list[dict[str, Any]] = []
        for item in vlist:
            raw_mid = item.get("mid")
            if raw_mid is not None and str(raw_mid).strip() != "":
                up_mid = str(raw_mid).strip()
                if up_mid != target_uid:
                    continue
            else:
                up_mid = target_uid

            if int(item.get("is_union_video") or 0) == 1:
                try:
                    owner_mid = await self.get_video_owner_mid(item["bvid"])
                except Exception as exc:
                    logger.warning(
                        "Failed to verify owner mid via view API for %s: %s",
                        item.get("bvid"),
                        exc,
                    )
                    owner_mid = None
                if owner_mid is not None:
                    if owner_mid != target_uid:
                        continue
                    up_mid = owner_mid

            play_val = item.get("play", 0)
            raw_duration = item.get("length")
            if raw_duration is None or raw_duration == "":
                raw_duration = item.get("duration", 0)
            videos.append(
                {
                    "bvid": item["bvid"],
                    "aid": int(item["aid"]),
                    "title": str(item.get("title", "")),
                    "pic": str(item.get("pic", "")),
                    "pubdate": int(item.get("pubdate", 0)),
                    "duration": parse_duration_seconds(raw_duration),
                    "play_count": int(play_val) if str(play_val).isdigit() else 0,
                    "up_mid": up_mid,
                }
            )
        return videos, len(vlist), total

    async def get_videos_page_wbi(
        self, uid: str, pn: int = 1, ps: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """Fetch uploaded videos via WBI /x/space/wbi/arc/search (fallback / cookie-backed)."""
        videos, _, total = await self._get_videos_page_wbi_raw(uid, pn=pn, ps=ps)
        return videos, total

    async def get_all_videos(self, uid: str) -> list[dict[str, Any]]:
        """Fetch all published UP-owned videos for the given UID across pages."""
        all_videos: dict[str, dict[str, Any]] = {}
        pn = 1
        ps = 100
        use_wbi = False

        while True:
            if not use_wbi:
                try:
                    page_items, raw_count, total = await self._get_videos_page_series_raw(
                        uid, pn=pn, ps=ps
                    )
                except BilibiliAPIError:
                    if pn == 1:
                        use_wbi = True
                        ps = 50
                        page_items, raw_count, total = await self._get_videos_page_wbi_raw(
                            uid, pn=pn, ps=ps
                        )
                    else:
                        raise
            else:
                page_items, raw_count, total = await self._get_videos_page_wbi_raw(
                    uid, pn=pn, ps=ps
                )

            if raw_count == 0:
                break
            for item in page_items:
                all_videos[item["bvid"]] = item

            if (total > 0 and pn * ps >= total) or (total <= 0 and raw_count < ps):
                break
            pn += 1
            await asyncio.sleep(0.25)

        return list(all_videos.values())

    async def get_video_cids(self, bvid: str) -> list[dict[str, Any]]:
        """Fetch part CIDs for a video via /x/player/pagelist."""
        data = await self._request_json(
            "https://api.bilibili.com/x/player/pagelist",
            params={"bvid": bvid},
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"pagelist error {data.get('code')} for {bvid}: {data.get('message')}",
                code=data.get("code"),
            )
        pages = data.get("data") or []
        return [
            {
                "cid": int(p["cid"]),
                "page": int(p.get("page", idx + 1)),
                "part": str(p.get("part", "")),
            }
            for idx, p in enumerate(pages)
            if p.get("cid")
        ]

    async def get_video_online_total(self, bvid: str, cid: int) -> tuple[int, str]:
        """Get current concurrent viewer count for a single (bvid, cid)."""
        data = await self._request_json(
            "https://api.bilibili.com/x/player/online/total",
            params={"bvid": bvid, "cid": cid},
            referer=f"https://www.bilibili.com/video/{bvid}",
        )
        if data.get("code") != 0:
            raise BilibiliAPIError(
                f"online/total error {data.get('code')} for {bvid}/{cid}: {data.get('message')}",
                code=data.get("code"),
            )
        raw_total = str((data.get("data") or {}).get("total", "0"))
        return parse_online_count(raw_total), raw_total
