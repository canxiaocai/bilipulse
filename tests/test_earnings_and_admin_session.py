from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from fastapi.testclient import TestClient

from app.auth import create_session_token, _sign_session_payload, get_admin_auth_info, get_or_create_session_secret
from app.database import Database
from app.earnings import (
    DEFAULT_CAMPAIGN_ID,
    compute_campaign_earnings,
    compute_tiered_payout,
    get_campaign,
)
from app.main import create_app

BJT_TZ = timezone(timedelta(hours=8))
OWNER_UID = "3690980052044072"
TARGET_TAG = "崩坏：因缘精灵创作者激励计划"


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, 0, 0, tzinfo=BJT_TZ).timestamp())


def test_admin_session_long_ttl_remember_and_sliding_renewal(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BILI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BILI_ADMIN_SESSION_HOURS", raising=False)
    db = Database(db_path=tmp_path / "admin_session.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        assert (
            client.put("/api/settings", json={"uid": OWNER_UID, "admin_password": "pw-2026"}).status_code
            == 200
        )

        # Default verify (remember=True) issues a 30-day persistent cookie with Max-Age & Expires
        resp_long = client.post("/api/admin/verify", json={"password": "pw-2026"})
        assert resp_long.status_code == 200
        set_cookie_long = resp_long.headers.get("set-cookie", "").lower()
        assert "max-age=2592000" in set_cookie_long
        assert "expires=" in set_cookie_long

        # Freshly issued cookie does not redundantly re-set Set-Cookie on /api/admin/status
        status_fresh = client.get("/api/admin/status")
        assert status_fresh.status_code == 200
        assert status_fresh.json()["authenticated"] is True
        assert "bilipulse_admin_session=" not in status_fresh.headers.get("set-cookie", "")

        # Short session (remember=False) caps cookie TTL at 12 hours
        resp_short = client.post(
            "/api/admin/verify", json={"password": "pw-2026", "remember": False}
        )
        assert resp_short.status_code == 200
        assert "max-age=43200" in resp_short.headers.get("set-cookie", "").lower()

        # Near-expiry token (<75% TTL left) triggers automatic sliding renewal on /api/dashboard
        short_remaining_token = create_session_token(db, ttl_seconds=3600, remember=True)
        client.cookies.set("bilipulse_admin_session", short_remaining_token)
        dash_renew = client.get("/api/dashboard?hours=24")
        assert dash_renew.status_code == 200
        assert dash_renew.json()["admin_auth"]["authenticated"] is True
        renewed_cookie = dash_renew.headers.get("set-cookie", "").lower()
        assert "bilipulse_admin_session=" in renewed_cookie
        assert "max-age=2592000" in renewed_cookie

        # Legacy 3-part cookie remains valid and upgrades to 30-day 4-part cookie
        info = get_admin_auth_info(db)
        secret = get_or_create_session_secret(db)
        exp_ts = int(time.time()) + 3600
        nonce = "legacy01"
        legacy_sig = _sign_session_payload(secret, exp_ts, nonce, info["fingerprint"])
        client.cookies.set("bilipulse_admin_session", f"{exp_ts}.{nonce}.{legacy_sig}")
        status_upgrade = client.get("/api/admin/status")
        assert status_upgrade.status_code == 200
        assert status_upgrade.json()["authenticated"] is True
        assert "max-age=2592000" in status_upgrade.headers.get("set-cookie", "").lower()


def test_earnings_tiered_cpm_and_rules_math():
    campaign = get_campaign(DEFAULT_CAMPAIGN_ID)
    assert campaign is not None

    # Verify exact progressive CPM examples from policy table
    p_24k, _ = compute_tiered_payout(campaign, 24_000)
    assert p_24k == 432.0

    p_100k, _ = compute_tiered_payout(campaign, 100_000)
    assert p_100k == 1800.0

    p_300k, _ = compute_tiered_payout(campaign, 300_000)
    assert p_300k == 5100.0

    p_800k, _ = compute_tiered_payout(campaign, 800_000)
    assert p_800k == 11100.0

    p_1m, _ = compute_tiered_payout(campaign, 1_200_000)
    assert p_1m == 11100.0

    # 1) Fewer than 8 qualified videos + 1 pending (<3000) + 1 untagged + 1 out-of-window
    videos_partial = [
        {
            "bvid": f"BV1q0000000{i}",
            "title": f"Qualified {i}",
            "pubdate": _ts(2026, 11, 28) + i * 86400,
            "play_count": 10_000,
            "like_count": 1200,
            "reply_count": 60,
            "tags": [TARGET_TAG, "日常"],
            "tags_synced_at": "2026-11-28T12:00:00+08:00",
        }
        for i in range(5)
    ] + [
        {
            "bvid": "BV1pending001",
            "title": "Under 3000 plays",
            "pubdate": _ts(2026, 12, 5),
            "play_count": 2999,
            "like_count": 400,
            "reply_count": 10,
            "tags": [TARGET_TAG],
            "tags_synced_at": "2026-12-05T12:00:00+08:00",
        },
        {
            "bvid": "BV1other00001",
            "title": "No target tag",
            "pubdate": _ts(2026, 12, 6),
            "play_count": 90_000,
            "like_count": 5000,
            "reply_count": 200,
            "tags": ["其他活动"],
            "tags_synced_at": "2026-12-06T12:00:00+08:00",
        },
        {
            "bvid": "BV1old0000001",
            "title": "Before campaign",
            "pubdate": _ts(2026, 11, 20),
            "play_count": 50_000,
            "like_count": 3000,
            "reply_count": 100,
            "tags": [TARGET_TAG],
            "tags_synced_at": "2026-11-20T12:00:00+08:00",
        },
    ]

    res_partial = compute_campaign_earnings(
        campaign, videos_partial, uid=OWNER_UID, now_ts=_ts(2026, 12, 7)
    )
    s = res_partial["summary"]
    assert s["matched_video_count"] == 6
    assert s["qualified_video_count"] == 5
    assert s["pending_video_count"] == 1
    assert s["videos_needed"] == 3
    assert s["total_play"] == 50_000
    assert s["avg_top10_likes"] == 1200.0  # divided by actual 5, NOT 10
    assert s["top_likes_sample_size"] == 5
    assert s["estimated_payout"] == 900.0
    assert s["settled_payout"] == 0.0  # < 8 videos
    assert s["is_settled_ready"] is False

    # 2) Exactly 8 qualified videos at 3,000 plays each (no 100k minimum!) with avg likes 1,100
    videos_min8 = [
        {
            "bvid": f"BV1m0000000{i}",
            "title": f"Min8 {i}",
            "pubdate": _ts(2026, 11, 28) + i * 86400,
            "play_count": 3_000,
            "like_count": 1_100,
            "reply_count": 30,
            "tags": [TARGET_TAG],
            "tags_synced_at": "2026-12-01T12:00:00+08:00",
        }
        for i in range(8)
    ]
    res_min8 = compute_campaign_earnings(
        campaign, videos_min8, uid=OWNER_UID, now_ts=_ts(2026, 12, 8)
    )
    assert res_min8["summary"]["total_play"] == 24_000
    assert res_min8["summary"]["estimated_payout"] == 432.0
    assert res_min8["summary"]["settled_payout"] == 432.0
    assert res_min8["summary"]["is_settled_ready"] is True

    # 3) 12 qualified videos: TOP10 likes averaged, 300k plays, avg_likes=2500 (caps settled at tier 1)
    videos_12 = [
        {
            "bvid": f"BV1t000000{i:02d}",
            "title": f"Top {i}",
            "pubdate": _ts(2026, 11, 27) + (i % 10) * 86400,
            "play_count": 25_000,
            "like_count": 2_500 if i < 10 else 100,
            "reply_count": 50,
            "tags": [TARGET_TAG],
            "tags_synced_at": "2026-12-02T12:00:00+08:00",
        }
        for i in range(12)
    ]
    res_12 = compute_campaign_earnings(
        campaign, videos_12, uid=OWNER_UID, now_ts=_ts(2026, 12, 10)
    )
    assert res_12["summary"]["total_play"] == 300_000
    assert res_12["summary"]["top_likes_sample_size"] == 10
    assert res_12["summary"]["avg_top10_likes"] == 2500.0  # ignores the 2 videos with 100 likes
    assert res_12["summary"]["estimated_payout"] == 5100.0
    assert res_12["summary"]["unlocked_tier_index"] == 1
    assert res_12["summary"]["settled_payout"] == 3600.0  # capped at 200k until avg likes >= 3000


def test_earnings_api_protection_owner_restriction_refresh_and_freeze(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("BILI_ADMIN_PASSWORD", "earn-secret-99")
    monkeypatch.setenv("BILI_EARNINGS_UID", OWNER_UID)

    db = Database(db_path=tmp_path / "earnings_api.db")
    db.update_settings({"uid": OWNER_UID, "account_name": "凉小郁"})
    db.upsert_videos(
        OWNER_UID,
        [
            {
                "bvid": "BV1dd1tzqEj7",
                "aid": 1001,
                "title": "帕拉雅雅",
                "pic": "https://i0.hdslb.com/test.jpg",
                "pubdate": _ts(2026, 11, 27, 22),
                "duration": 89,
                "play_count": 5000,
            }
        ],
    )
    db.update_video_stats_batch(
        [
            {
                "bvid": "BV1dd1tzqEj7",
                "play_count": 5000,
                "like_count": 1200,
                "coin_count": 80,
                "reply_count": 45,
                "tags": [TARGET_TAG, "帕拉雅雅"],
            }
        ]
    )

    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        # Unauthenticated request is rejected with 401
        assert client.get(f"/api/earnings?uid={OWNER_UID}").status_code == 401
        assert client.post(f"/api/earnings/refresh?uid={OWNER_UID}").status_code == 401

        # Verify admin
        assert (
            client.post("/api/admin/verify", json={"password": "earn-secret-99"}).status_code
            == 200
        )

        # Non-owner UID is rejected with 404
        assert client.get("/api/earnings?uid=12345678").status_code == 404

        # Owner UID returns live earnings
        r = client.get(f"/api/earnings?uid={OWNER_UID}&sync_missing=false")
        assert r.status_code == 200
        data = r.json()
        assert data["uid"] == OWNER_UID
        assert data["period"]["id"] == DEFAULT_CAMPAIGN_ID
        assert data["summary"]["qualified_video_count"] == 1
        assert data["summary"]["total_play"] == 5000
        assert data["summary"]["estimated_payout"] == 90.0
        assert data["summary"]["settled_payout"] == 0.0
        assert data["videos"][0]["bvid"] == "BV1dd1tzqEj7"
        assert data["videos"][0]["reply_count"] == 45

        # Period-end freeze: simulate clock after 2026-12-15 23:59:59+08:00
        after_end = _ts(2026, 12, 16, 1)
        monkeypatch.setattr("app.routers.earnings.time.time", lambda: after_end)
        r_freeze = client.get(f"/api/earnings?uid={OWNER_UID}&sync_missing=false")
        assert r_freeze.status_code == 200
        frozen_data = r_freeze.json()
        assert frozen_data["period"]["status"] == "ended"
        assert frozen_data["period"]["frozen"] is True
        assert frozen_data["summary"]["total_play"] == 5000

        # Subsequent play growth after period end does NOT change the frozen Period 1 snapshot
        db.update_video_stats_batch(
            [
                {
                    "bvid": "BV1dd1tzqEj7",
                    "play_count": 99_999,
                    "like_count": 9999,
                    "coin_count": 100,
                    "reply_count": 500,
                    "tags": [TARGET_TAG],
                }
            ]
        )
        r_after = client.get(f"/api/earnings?uid={OWNER_UID}&sync_missing=false")
        assert r_after.status_code == 200
        assert r_after.json()["summary"]["total_play"] == 5000
        assert r_after.json()["period"]["frozen"] is True

        # Frontend HTML includes earnings modal & admin remember UI
        html = client.get("/").text
        assert "earningsModal" in html
        assert "adminAuthRemember" in html
        assert 'autocomplete="username"' in html
