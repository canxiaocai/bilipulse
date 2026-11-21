import asyncio
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.bilibili.client import (
    BilibiliAPIError,
    BilibiliClient,
    parse_duration_seconds,
    parse_online_count,
)
from app.bilibili.collectors import (
    VIDEO_SYNC_RULE,
    AllVideosOnlineCollector,
    CrawlContext,
    CrawlService,
)
from app.database import Database
from app.main import create_app


@pytest.fixture(autouse=True)
def _disable_default_admin_password_for_unit_tests(monkeypatch):
    monkeypatch.delenv("BILI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BILI_TRUST_PROXY", raising=False)
    monkeypatch.setenv("BILI_DEFAULT_ADMIN_PASSWORD", "")
    BilibiliClient.clear_credential_cache()
    yield
    BilibiliClient.clear_credential_cache()


def test_parse_online_count():
    assert parse_online_count("0") == 0
    assert parse_online_count("11") == 11
    assert parse_online_count("100+") == 100
    assert parse_online_count("1.2万+") == 12000
    assert parse_online_count("10万+") == 100000
    assert parse_online_count("2亿") == 200000000
    assert parse_online_count("-") == 0
    assert parse_online_count("") == 0
    assert parse_online_count(None) == 0


def test_wbi_signing_strips_special_chars():
    query = BilibiliClient.sign_wbi_params(
        {"mid": "12345", "keyword": "a!b'c(d)e*"},
        "7cd084941338484aae1ad9425b84077c",
        "4932caff0ff746eab6f01bf08b70ac45",
    )
    assert "w_rid=" in query
    assert "wts=" in query
    assert "keyword=abcde" in query


def test_database_settings_and_snapshots(tmp_path: Path):
    db = Database(db_path=tmp_path / "test.db")
    pub = db.get_public_settings()
    assert pub["uid"] == ""
    assert pub["interval_minutes"] == 5
    assert pub["has_cookie"] is False

    updated = db.update_settings({"uid": "100001001", "cookie": "SESSDATA=secret_token_12345"})
    assert updated["uid"] == "100001001"
    assert updated["has_cookie"] is True
    assert "secret_token_12345" not in updated["cookie_preview"]

    db.upsert_videos(
        "100001001",
        [
            {
                "bvid": "BV1xx",
                "aid": 1001,
                "title": "Video A",
                "play_count": 500,
                "cids": [{"cid": 11, "page": 1, "part": "P1"}],
            }
        ],
    )
    snap1 = db.insert_snapshot(
        uid="100001001",
        follower_count=1000,
        following_count=10,
        online_viewers_total=25,
        video_count=1,
        videos_succeeded=1,
        extra_metrics={"total_video_plays": 500},
        status="success",
        error_message=None,
        duration_ms=120,
        video_online_items=[{"bvid": "BV1xx", "online_count": 25, "online_raw": "25"}],
    )
    assert snap1 > 0
    latest, prev = db.get_latest_two_snapshots("100001001")
    assert latest is not None
    assert prev is None
    assert latest["follower_count"] == 1000
    assert latest["online_viewers_total"] == 25

    history = db.get_video_history("BV1xx")
    assert len(history) == 1
    assert history[0]["online_count"] == 25
    assert history[0]["play_count"] == 500


def test_crawl_service_sums_all_videos(tmp_path: Path):
    db = Database(db_path=tmp_path / "crawl.db")
    db.update_settings({"uid": "123456", "request_delay_ms": "50"})

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 54321, "following": 12}
    fake_client.get_account_card.return_value = {
        "name": "测试UP主",
        "face": "https://example.com/face.jpg",
        "follower": 54321,
        "archive_count": 2,
    }
    fake_client.get_all_videos.return_value = [
        {"bvid": "BV1A", "aid": 1, "title": "Video 1", "pic": "", "pubdate": 100, "duration": 60, "play_count": 300},
        {"bvid": "BV1B", "aid": 2, "title": "Video 2", "pic": "", "pubdate": 200, "duration": 90, "play_count": 700},
    ]

    async def fake_cids(bvid: str):
        if bvid == "BV1A":
            return [{"cid": 101, "page": 1, "part": "P1"}]
        return [{"cid": 201, "page": 1, "part": "P1"}, {"cid": 202, "page": 2, "part": "P2"}]

    async def fake_online(bvid: str, cid: int):
        mapping = {101: (12, "12"), 201: (8, "8"), 202: (5, "5")}
        return mapping[cid]

    fake_client.get_video_cids.side_effect = fake_cids
    fake_client.get_video_online_total.side_effect = fake_online

    service = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        result = asyncio.run(service.run_crawl(force_sync_videos=True))

    assert result["skipped"] is False
    assert result["status"] == "success"
    assert result["follower_count"] == 54321
    assert result["online_viewers_total"] == 22  # (12 - 1) + (8 - 1) + (5 - 1)
    assert result["video_count"] == 2
    assert result["videos_succeeded"] == 2
    videos = {v["bvid"]: v for v in db.get_videos("123456")}
    assert videos["BV1A"]["last_online_count"] == 11
    assert videos["BV1A"]["last_online_raw"] == "12"
    assert videos["BV1B"]["last_online_count"] == 11
    assert videos["BV1B"]["last_online_raw"] == "8+5"

    pub = db.get_public_settings()
    assert pub["account_name"] == "测试UP主"


def test_fastapi_routes(tmp_path: Path):
    db = Database(db_path=tmp_path / "api.db")
    app = create_app(database=db, enable_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}

        bad_uid = client.put("/api/settings", json={"uid": "abc"})
        assert bad_uid.status_code == 400

        ok_put = client.put("/api/settings", json={"uid": "100001001", "interval_minutes": 5, "enabled": False})
        assert ok_put.status_code == 200
        assert ok_put.json()["uid"] == "100001001"

        dash = client.get("/api/dashboard?hours=24")
        assert dash.status_code == 200
        assert dash.json()["settings"]["uid"] == "100001001"

        html_resp = client.get("/")
        assert html_resp.status_code == 200
        assert "B站账号实时监控" in html_resp.text
        assert "btnOpenSettings" in html_resp.text
        assert "btnCrawlNow" not in html_resp.text
        assert "btnCrawlAll" not in html_resp.text
        assert client.post("/api/crawl/all").status_code == 404


def test_series_filters_non_up_joint_videos_and_paginates_partial_pages():
    async def scenario():
        uid = "3546100000000001"
        async with BilibiliClient() as client:
            client._session_bootstrapped = True

            async def fake_request(url, params=None, referer=None, retries=2):
                assert "recArchivesByKeywords" in url
                pn = params["pn"]
                if pn == 1:
                    # raw_count < ps (3 < 100) AND filtered page is empty, while pn * ps < total (100 < 150)
                    return {
                        "code": 0,
                        "data": {
                            "page": {"total": 150},
                            "archives": [
                                {
                                    "bvid": "BV1collab1",
                                    "aid": 1,
                                    "title": "Collaborator Joint 1",
                                    "upMid": 200002002,
                                    "stat": {"view": 999},
                                },
                                {
                                    "bvid": "BV1collab2",
                                    "aid": 2,
                                    "title": "Collaborator Joint 2",
                                    "upMid": "200002002",
                                    "stat": {"view": 888},
                                },
                            ],
                        },
                    }
                if pn == 2:
                    return {
                        "code": 0,
                        "data": {
                            "page": {"total": 150},
                            "archives": [
                                {
                                    "bvid": "BV1solo",
                                    "aid": 3,
                                    "title": "Solo Video",
                                    "upMid": int(uid),
                                    "stat": {"view": 100},
                                },
                                {
                                    "bvid": "BV1up_joint",
                                    "aid": 4,
                                    "title": "UP-Owned Joint Video",
                                    "upMid": uid,
                                    "stat": {"view": 200},
                                },
                                {
                                    "bvid": "BV1no_upmid",
                                    "aid": 5,
                                    "title": "Missing upMid Fallback",
                                    "stat": {"view": 50},
                                },
                                {
                                    "bvid": "BV1collab3",
                                    "aid": 6,
                                    "title": "Collaborator Joint 3",
                                    "upMid": 999999,
                                    "stat": {"view": 777},
                                },
                            ],
                        },
                    }
                raise AssertionError(f"Unexpected page {pn}")

            with patch.object(client, "_request_json", side_effect=fake_request):
                with patch("app.bilibili.client.asyncio.sleep", new=AsyncMock()):
                    videos = await client.get_all_videos(uid)

            assert [v["bvid"] for v in videos] == ["BV1solo", "BV1up_joint", "BV1no_upmid"]
            assert all(v["up_mid"] == uid for v in videos)

    asyncio.run(scenario())


def test_wbi_filters_mid_and_verifies_union_video_owner():
    async def scenario():
        uid = "3546100000000001"
        async with BilibiliClient() as client:
            client._session_bootstrapped = True
            client._wbi_keys = ("a" * 32, "b" * 32)
            client._wbi_fetched_at = time.time()

            async def fake_request(url, params=None, referer=None, retries=2):
                if "recArchivesByKeywords" in url:
                    raise BilibiliAPIError("series blocked", code=-352)
                if "wbi/arc/search" in url:
                    return {
                        "code": 0,
                        "data": {
                            "page": {"count": 5},
                            "list": {
                                "vlist": [
                                    {
                                        "bvid": "BV1other_mid",
                                        "aid": 10,
                                        "title": "Other mid",
                                        "mid": 200002002,
                                        "is_union_video": 1,
                                        "play": 123,
                                    },
                                    {
                                        "bvid": "BV1union_not_owner",
                                        "aid": 11,
                                        "title": "Union disguised mid",
                                        "mid": int(uid),
                                        "is_union_video": 1,
                                        "play": 456,
                                    },
                                    {
                                        "bvid": "BV1union_owner",
                                        "aid": 12,
                                        "title": "Union owned by UP",
                                        "mid": int(uid),
                                        "is_union_video": 1,
                                        "play": 789,
                                    },
                                    {
                                        "bvid": "BV1union_view_err",
                                        "aid": 13,
                                        "title": "Union view transient error",
                                        "mid": int(uid),
                                        "is_union_video": 1,
                                        "play": 10,
                                    },
                                    {
                                        "bvid": "BV1solo_wbi",
                                        "aid": 14,
                                        "title": "Solo WBI",
                                        "mid": int(uid),
                                        "is_union_video": 0,
                                        "play": "250",
                                    },
                                ]
                            },
                        },
                    }
                if "web-interface/view" in url:
                    bvid = params["bvid"]
                    if bvid == "BV1union_not_owner":
                        return {"code": 0, "data": {"owner": {"mid": 200002002}}}
                    if bvid == "BV1union_owner":
                        return {"code": 0, "data": {"owner": {"mid": int(uid)}}}
                    if bvid == "BV1union_view_err":
                        raise BilibiliAPIError("transient timeout", code=-504)
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.object(client, "_request_json", side_effect=fake_request):
                videos = await client.get_all_videos(uid)

            assert [v["bvid"] for v in videos] == [
                "BV1union_owner",
                "BV1union_view_err",
                "BV1solo_wbi",
            ]
            assert all(v["up_mid"] == uid for v in videos)

    asyncio.run(scenario())


def test_get_video_owner_mid():
    async def scenario():
        async with BilibiliClient() as client:
            with patch.object(
                client,
                "_request_json",
                new=AsyncMock(return_value={"code": 0, "data": {"owner": {"mid": 200002002}}}),
            ):
                assert await client.get_video_owner_mid("BV1test") == "200002002"

            with patch.object(
                client,
                "_request_json",
                new=AsyncMock(return_value={"code": 0, "data": {"owner": {}}}),
            ):
                assert await client.get_video_owner_mid("BV1missing") is None

            with patch.object(
                client,
                "_request_json",
                new=AsyncMock(return_value={"code": -404, "message": "not found"}),
            ):
                with pytest.raises(BilibiliAPIError):
                    await client.get_video_owner_mid("BV1err")

    asyncio.run(scenario())


def test_database_delete_stale_videos_allow_empty(tmp_path: Path):
    db = Database(db_path=tmp_path / "stale.db")
    uid = "3546100000000001"
    db.upsert_videos(
        uid,
        [
            {"bvid": "BV1A", "aid": 1, "title": "A"},
            {"bvid": "BV1B", "aid": 2, "title": "B"},
        ],
    )
    assert db.delete_stale_videos(uid, set(), allow_empty=False) == 0
    assert len(db.get_videos(uid)) == 2

    deleted = db.delete_stale_videos(uid, set(), allow_empty=True)
    assert deleted == 2
    assert db.get_videos(uid) == []


def test_collector_rule_upgrade_purges_cached_joint_and_avoids_resync_loop(tmp_path: Path):
    uid = "3546100000000001"
    db = Database(db_path=tmp_path / "collector.db")
    now_ts = int(time.time())
    db.update_settings(
        {
            "uid": uid,
            "request_delay_ms": "50",
            "video_sync_interval_minutes": "30",
            f"last_video_sync_ts_{uid}": str(now_ts),
        }
    )
    # Pre-populate legacy cache with 1 owned video + 2 collaborator joint videos
    db.upsert_videos(
        uid,
        [
            {"bvid": "BV1owned", "aid": 1, "title": "Owned", "play_count": 100, "cids": [{"cid": 11, "page": 1, "part": "P1"}]},
            {"bvid": "BV1collab1", "aid": 2, "title": "Collab 1", "play_count": 500, "cids": [{"cid": 22, "page": 1, "part": "P1"}]},
            {"bvid": "BV1collab2", "aid": 3, "title": "Collab 2", "play_count": 900, "cids": [{"cid": 33, "page": 1, "part": "P1"}]},
        ],
    )

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 1000, "following": 5}
    fake_client.get_account_card.return_value = {
        "name": "Tester",
        "face": "",
        "follower": 1000,
        "archive_count": 3,  # Bilibili card still reports 3 (1 owned + 2 collaborator)
    }
    fake_client.get_all_videos.return_value = [
        {"bvid": "BV1owned", "aid": 1, "title": "Owned", "pic": "", "pubdate": 100, "duration": 60, "play_count": 100, "up_mid": uid},
        {"bvid": "BV1collab1", "aid": 2, "title": "Collab 1", "pic": "", "pubdate": 101, "duration": 60, "play_count": 500, "up_mid": "200002002"},
    ]
    fake_client.get_video_online_total.return_value = (7, "7")

    service = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        # 1) Rule-upgrade crawl (non-forced): should resync despite recent last_video_sync_ts
        res1 = asyncio.run(service.run_crawl(force_sync_videos=False))
        assert res1["status"] == "success"
        assert res1["video_count"] == 1
        assert res1["online_viewers_total"] == 6  # 7 - 1
        assert fake_client.get_all_videos.await_count == 1
        assert [v["bvid"] for v in db.get_videos(uid)] == ["BV1owned"]
        assert db.get_setting(f"video_sync_rule_{uid}") == VIDEO_SYNC_RULE
        assert db.get_setting(f"last_archive_count_{uid}") == "3"

        # 2) Next crawl: archive_count_reported (3) != len(cached_videos) (1),
        #    but matches last_archive_count (3) -> MUST NOT trigger resync loop!
        res2 = asyncio.run(service.run_crawl(force_sync_videos=False))
        assert res2["status"] == "success"
        assert res2["video_count"] == 1
        assert fake_client.get_all_videos.await_count == 1

        # 3) Archive count changes (3 -> 4): should trigger resync
        fake_client.get_account_card.return_value["archive_count"] = 4
        res3 = asyncio.run(service.run_crawl(force_sync_videos=False))
        assert res3["status"] == "success"
        assert fake_client.get_all_videos.await_count == 2
        assert db.get_setting(f"last_archive_count_{uid}") == "4"


def test_collector_purges_all_when_only_non_up_videos_and_skips_metadata_on_error(tmp_path: Path):
    uid = "3546100000000001"
    db = Database(db_path=tmp_path / "collector_empty.db")
    db.update_settings({"uid": uid, "request_delay_ms": "50"})
    db.upsert_videos(
        uid,
        [{"bvid": "BV1collab_only", "aid": 1, "title": "Collab Only", "play_count": 300}],
    )

    # Failed fetch must NOT update video_sync_rule or purge cached videos
    failing_client = AsyncMock()
    failing_client.get_all_videos.side_effect = BilibiliAPIError("temporary error", code=-500)
    collector = AllVideosOnlineCollector()
    ctx_fail = CrawlContext(
        uid=uid,
        client=failing_client,
        db=db,
        settings=db.get_all_settings(),
        force_sync_videos=True,
        extra_metrics={"archive_count_reported": 1},
    )
    asyncio.run(collector.collect(ctx_fail))
    assert len(db.get_videos(uid)) == 1
    assert db.get_setting(f"video_sync_rule_{uid}") == ""
    assert db.get_setting(f"last_video_sync_ts_{uid}") == ""

    # Successful fetch returning only non-UP video (empty after filtering) purges cache via allow_empty=True
    clean_client = AsyncMock()
    clean_client.get_all_videos.return_value = [
        {"bvid": "BV1collab_only", "aid": 1, "title": "Collab Only", "up_mid": "200002002"}
    ]
    ctx_ok = CrawlContext(
        uid=uid,
        client=clean_client,
        db=db,
        settings=db.get_all_settings(),
        force_sync_videos=True,
        extra_metrics={"archive_count_reported": 1},
    )
    asyncio.run(collector.collect(ctx_ok))
    assert db.get_videos(uid) == []
    assert ctx_ok.video_count == 0
    assert ctx_ok.online_viewers_total == 0
    assert db.get_setting(f"video_sync_rule_{uid}") == VIDEO_SYNC_RULE
    assert db.get_setting(f"last_archive_count_{uid}") == "1"


def test_test_connection_filters_non_up_videos(tmp_path: Path):
    uid = "3546100000000001"
    db = Database(db_path=tmp_path / "test_conn.db")
    app = create_app(database=db, enable_scheduler=False)

    async def fake_request(self_client, url, params=None, referer=None, retries=2):
        if "relation/stat" in url:
            return {"code": 0, "data": {"follower": 2000, "following": 10}}
        if "web-interface/card" in url:
            return {
                "code": 0,
                "data": {
                    "card": {"name": "UP主", "face": "https://example.com/a.jpg"},
                    "follower": 2000,
                    "archive_count": 5,
                },
            }
        if "recArchivesByKeywords" in url:
            assert params["ps"] == 100
            return {
                "code": 0,
                "data": {
                    "page": {"total": 5},
                    "archives": [
                        {"bvid": "BV1collabA", "aid": 1, "title": "Collab A", "upMid": 200002002},
                        {"bvid": "BV1own1", "aid": 2, "title": "Own 1", "upMid": int(uid)},
                        {"bvid": "BV1collabB", "aid": 3, "title": "Collab B", "upMid": 200002002},
                        {"bvid": "BV1own2", "aid": 4, "title": "Own 2", "upMid": int(uid)},
                        {"bvid": "BV1own3", "aid": 5, "title": "Own 3", "upMid": int(uid)},
                    ],
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    with patch.object(BilibiliClient, "ensure_session_tokens", new=AsyncMock()):
        with patch.object(BilibiliClient, "_request_json", new=fake_request):
            with TestClient(app) as client:
                resp = client.post("/api/test-connection", json={"uid": uid})
                assert resp.status_code == 200
                body = resp.json()
                assert body["ok"] is True
                assert body["video_total"] == 3
                assert body["sample_videos"] == [
                    {"bvid": "BV1own1", "title": "Own 1"},
                    {"bvid": "BV1own2", "title": "Own 2"},
                    {"bvid": "BV1own3", "title": "Own 3"},
                ]

    async def fake_request_large(self_client, url, params=None, referer=None, retries=2):
        if "recArchivesByKeywords" in url:
            return {
                "code": 0,
                "data": {
                    "page": {"total": 150},
                    "archives": [
                        {"bvid": "BV1collabX", "aid": 10, "title": "Collab X", "upMid": 999},
                        {"bvid": "BV1ownX", "aid": 11, "title": "Own X", "upMid": int(uid)},
                    ],
                },
            }
        return await fake_request(self_client, url, params=params, referer=referer, retries=retries)

    with patch.object(BilibiliClient, "ensure_session_tokens", new=AsyncMock()):
        with patch.object(BilibiliClient, "_request_json", new=fake_request_large):
            with TestClient(app) as client:
                resp = client.post("/api/test-connection", json={"uid": uid})
                assert resp.status_code == 200
                body = resp.json()
                assert body["ok"] is True
                assert body["video_total"] == 150
                assert body["sample_videos"] == [{"bvid": "BV1ownX", "title": "Own X"}]


def test_online_count_subtracts_one_per_cid_and_clamps_to_zero(tmp_path: Path):
    db = Database(db_path=tmp_path / "clamp.db")
    db.update_settings({"uid": "123456", "request_delay_ms": "50"})
    db.upsert_videos(
        "123456",
        [
            {"bvid": "BV1zero", "aid": 1, "title": "Zero", "cids": [{"cid": 1, "page": 1, "part": "P1"}]},
            {"bvid": "BV1one", "aid": 2, "title": "One", "cids": [{"cid": 2, "page": 1, "part": "P1"}]},
            {
                "bvid": "BV1multi",
                "aid": 3,
                "title": "Multi",
                "cids": [
                    {"cid": 3, "page": 1, "part": "P1"},
                    {"cid": 4, "page": 2, "part": "P2"},
                ],
            },
        ],
    )
    db.update_settings(
        {
            "last_video_sync_ts_123456": str(int(time.time())),
            "video_sync_rule_123456": VIDEO_SYNC_RULE,
        }
    )

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 100, "following": 1}
    fake_client.get_account_card.return_value = {
        "name": "Tester",
        "face": "",
        "follower": 100,
        "archive_count": 3,
    }

    async def fake_online(bvid: str, cid: int):
        mapping = {1: (0, "0"), 2: (1, "1"), 3: (1, "1"), 4: (3, "3")}
        return mapping[cid]

    fake_client.get_video_online_total.side_effect = fake_online

    service = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        result = asyncio.run(service.run_crawl(force_sync_videos=False))

    assert result["status"] == "success"
    assert result["online_viewers_total"] == 2  # 0 + 0 + (0 + 2)
    videos = {v["bvid"]: v for v in db.get_videos("123456")}
    assert videos["BV1zero"]["last_online_count"] == 0
    assert videos["BV1one"]["last_online_count"] == 0
    assert videos["BV1multi"]["last_online_count"] == 2
    assert videos["BV1multi"]["last_online_raw"] == "1+3"


def test_database_migrates_legacy_online_counts_once(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    db = Database(db_path=db_path)
    # Simulate a legacy database before online_count_rule existed
    with db.connection() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'online_count_rule'")
    db.upsert_videos(
        "123",
        [
            {"bvid": "BV1A", "aid": 1, "title": "A", "cids": [{"cid": 11, "page": 1, "part": "P1"}]},
            {
                "bvid": "BV1B",
                "aid": 2,
                "title": "B",
                "cids": [{"cid": 21, "page": 1, "part": "P1"}, {"cid": 22, "page": 2, "part": "P2"}],
            },
        ],
    )
    db.insert_snapshot(
        uid="123",
        follower_count=500,
        following_count=5,
        online_viewers_total=14,
        video_count=2,
        videos_succeeded=2,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=100,
        video_online_items=[
            {"bvid": "BV1A", "online_count": 1, "online_raw": "1"},
            {"bvid": "BV1B", "online_count": 13, "online_raw": "8+5"},
        ],
    )

    # Re-initializing Database triggers one-time migration
    migrated = Database(db_path=db_path)
    latest, _ = migrated.get_latest_two_snapshots("123")
    assert latest is not None
    assert latest["online_viewers_total"] == 11  # (1-1) + (8-1) + (5-1)
    videos = {v["bvid"]: v for v in migrated.get_videos("123")}
    assert videos["BV1A"]["last_online_count"] == 0
    assert videos["BV1B"]["last_online_count"] == 11
    assert migrated.get_video_history("BV1A")[0]["online_count"] == 0
    assert migrated.get_video_history("BV1B")[0]["online_count"] == 11

    # Re-initializing again must be idempotent (must NOT subtract a second time)
    migrated_again = Database(db_path=db_path)
    latest2, _ = migrated_again.get_latest_two_snapshots("123")
    assert latest2 is not None
    assert latest2["online_viewers_total"] == 11
    with sqlite3.connect(db_path) as raw_conn:
        row = raw_conn.execute("SELECT value FROM settings WHERE key = 'online_count_rule'").fetchone()
        assert row is not None and row[0] == "minus_1_per_cid_v1"


def test_video_history_records_play_count_and_migrates_legacy_schema(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "play_hist.db"
    # Create legacy video_online_snapshots table without play_count column
    with sqlite3.connect(db_path) as raw_conn:
        raw_conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE account_snapshots (
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
            CREATE TABLE videos (
                bvid TEXT PRIMARY KEY,
                uid TEXT NOT NULL,
                aid INTEGER NOT NULL,
                title TEXT NOT NULL,
                pic TEXT NOT NULL DEFAULT '',
                pubdate INTEGER NOT NULL DEFAULT 0,
                duration INTEGER NOT NULL DEFAULT 0,
                play_count INTEGER NOT NULL DEFAULT 0,
                cids_json TEXT NOT NULL DEFAULT '[]',
                last_online_count INTEGER NOT NULL DEFAULT 0,
                last_online_raw TEXT NOT NULL DEFAULT '',
                last_checked_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE video_online_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
                bvid TEXT NOT NULL,
                online_count INTEGER NOT NULL,
                online_raw TEXT NOT NULL DEFAULT ''
            );
            """
        )

    db = Database(db_path=db_path)
    db.upsert_videos("999", [{"bvid": "BV1play", "aid": 10, "title": "Play Test", "play_count": 1200}])
    db.insert_snapshot(
        uid="999",
        follower_count=100,
        following_count=1,
        online_viewers_total=8,
        video_count=1,
        videos_succeeded=1,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=50,
        video_online_items=[{"bvid": "BV1play", "online_count": 8, "online_raw": "8", "play_count": 1250}],
    )

    app = create_app(database=db, enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.get("/api/videos/BV1play/history?limit=10")
        assert resp.status_code == 200
        hist = resp.json()["history"]
        assert len(hist) == 1
        assert hist[0]["online_count"] == 8
        assert hist[0]["play_count"] == 1250


def test_multi_account_crud_and_switching(tmp_path: Path):
    db = Database(db_path=tmp_path / "multi_acc.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        # Initially empty
        res0 = client.get("/api/accounts")
        assert res0.status_code == 200
        assert res0.json() == {"active_uid": "", "accounts": []}

        # Invalid UID rejected
        bad = client.post("/api/accounts", json={"uid": "not_digits"})
        assert bad.status_code == 400

        # Configure global cookie once; all accounts inherit it across switches/deletions
        set_global = client.put("/api/settings", json={"cookie": "SESSDATA=global_shared_12345"})
        assert set_global.status_code == 200
        assert set_global.json()["has_cookie"] is True

        # Add first account without per-account cookie
        add1 = client.post(
            "/api/accounts",
            json={"uid": "10001", "enabled": True},
        )
        assert add1.status_code == 200
        assert add1.json()["active_uid"] == "10001"
        assert len(add1.json()["accounts"]) == 1
        assert add1.json()["accounts"][0]["has_cookie"] is True

        # Duplicate rejected
        dup = client.post("/api/accounts", json={"uid": "10001"})
        assert dup.status_code == 409

        # Add second account and keep active on it; global cookie remains active
        add2 = client.post(
            "/api/accounts",
            json={"uid": "20002", "cookie": "", "enabled": True, "set_active": True},
        )
        assert add2.status_code == 200
        assert add2.json()["active_uid"] == "20002"
        assert [a["uid"] for a in add2.json()["accounts"]] == ["10001", "20002"]
        assert all(a["has_cookie"] is True for a in add2.json()["accounts"])

        # Seed distinct snapshots for each UID
        db.insert_snapshot(
            uid="10001",
            follower_count=1111,
            following_count=10,
            online_viewers_total=22,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=50,
            video_online_items=[],
        )
        db.insert_snapshot(
            uid="20002",
            follower_count=2222,
            following_count=20,
            online_viewers_total=44,
            video_count=2,
            videos_succeeded=2,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=60,
            video_online_items=[],
        )

        # Dashboard query for explicit uid vs active uid
        dash2 = client.get("/api/dashboard?hours=24")
        assert dash2.status_code == 200
        assert dash2.json()["settings"]["uid"] == "20002"
        assert dash2.json()["latest"]["follower_count"] == 2222
        assert len(dash2.json()["accounts"]) == 2

        dash1_explicit = client.get("/api/dashboard?hours=24&uid=10001")
        assert dash1_explicit.status_code == 200
        assert dash1_explicit.json()["settings"]["uid"] == "10001"
        assert dash1_explicit.json()["latest"]["follower_count"] == 1111

        # Activate account 10001
        act = client.post("/api/accounts/10001/activate")
        assert act.status_code == 200
        assert act.json()["active_uid"] == "10001"
        assert client.get("/api/settings").json()["uid"] == "10001"
        assert client.get("/api/settings").json()["has_cookie"] is True

        # Update account 20002 enabled flag
        upd = client.put("/api/accounts/20002", json={"enabled": False})
        assert upd.status_code == 200
        assert upd.json()["account"]["enabled"] is False

        # Delete active account 10001 with delete_data=True -> falls back to 20002 and keeps global cookie
        dlt = client.delete("/api/accounts/10001?delete_data=true")
        assert dlt.status_code == 200
        assert dlt.json()["active_uid"] == "20002"
        assert client.get("/api/settings").json()["has_cookie"] is True
        assert db.count_snapshots("10001") == 0
        assert db.count_snapshots("20002") == 1


def test_legacy_single_account_migrates_to_monitored_accounts(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "legacy_single.db"
    with sqlite3.connect(db_path) as raw_conn:
        raw_conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO settings (key, value, updated_at) VALUES
                ('uid', '888888', '2026-01-01T00:00:00Z'),
                ('account_name', '老账号', '2026-01-01T00:00:00Z'),
                ('account_face', 'https://example.com/old.jpg', '2026-01-01T00:00:00Z'),
                ('cookie', 'SESSDATA=legacy_cookie_9999', '2026-01-01T00:00:00Z'),
                ('enabled', 'true', '2026-01-01T00:00:00Z');
            """
        )

    db = Database(db_path=db_path)
    accounts = db.get_accounts_summary()
    assert len(accounts) == 1
    assert accounts[0]["uid"] == "888888"
    assert accounts[0]["account_name"] == "老账号"
    assert accounts[0]["has_cookie"] is True
    assert accounts[0]["enabled"] is True


def test_crawl_all_crawls_enabled_accounts_with_isolated_cookies_and_profiles(tmp_path: Path):
    db = Database(db_path=tmp_path / "crawl_all.db")
    db.create_account("101", cookie="SESSDATA=cookie_101", enabled=True, set_active=True)
    db.create_account("202", cookie="SESSDATA=cookie_202", enabled=True, set_active=False)
    db.create_account("303", cookie="SESSDATA=cookie_303", enabled=False, set_active=False)

    seen_cookies: dict[str, str] = {}

    def make_client(cookie: str = "", **kwargs):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None

        async def get_relation_stat(uid: str):
            seen_cookies[uid] = cookie
            return {"follower": 1000 if uid == "101" else 2000, "following": 5}

        async def get_account_card(uid: str):
            return {
                "name": f"UP_{uid}",
                "face": f"https://example.com/{uid}.jpg",
                "follower": 1000 if uid == "101" else 2000,
                "archive_count": 1,
            }

        async def get_all_videos(uid: str):
            return [
                {
                    "bvid": f"BV1_{uid}",
                    "aid": int(uid),
                    "title": f"Video {uid}",
                    "pic": "",
                    "pubdate": 100,
                    "duration": 60,
                    "play_count": 500,
                    "up_mid": uid,
                }
            ]

        async def get_video_cids(bvid: str):
            return [{"cid": int(bvid.split("_")[1]) * 10, "page": 1, "part": "P1"}]

        async def get_video_online_total(bvid: str, cid: int):
            return (11, "11") if bvid == "BV1_101" else (21, "21")

        client.get_relation_stat.side_effect = get_relation_stat
        client.get_account_card.side_effect = get_account_card
        client.get_all_videos.side_effect = get_all_videos
        client.get_video_cids.side_effect = get_video_cids
        client.get_video_online_total.side_effect = get_video_online_total
        return client

    service = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_client):
        res = asyncio.run(service.run_crawl_all(force_sync_videos=True))

    assert res["skipped"] is False
    assert res["total"] == 2
    assert seen_cookies == {"101": "SESSDATA=cookie_101", "202": "SESSDATA=cookie_202"}

    summaries = {a["uid"]: a for a in db.get_accounts_summary()}
    assert summaries["101"]["account_name"] == "UP_101"
    assert summaries["101"]["latest_follower_count"] == 1000
    assert summaries["101"]["latest_online_viewers_total"] == 10
    assert summaries["202"]["account_name"] == "UP_202"
    assert summaries["202"]["latest_follower_count"] == 2000
    assert summaries["202"]["latest_online_viewers_total"] == 20
    assert summaries["303"]["latest_follower_count"] is None

    # Active account 101 settings must NOT be overwritten by 202's profile
    assert db.get_public_settings()["account_name"] == "UP_101"


def test_account_reorder_persists_sort_order_and_crawl_order(tmp_path: Path):
    db = Database(db_path=tmp_path / "reorder.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        for uid, set_active in [("10001", True), ("20002", False), ("30003", False)]:
            resp = client.post(
                "/api/accounts",
                json={"uid": uid, "enabled": True, "set_active": set_active},
            )
            assert resp.status_code == 200

        initial = client.get("/api/accounts").json()["accounts"]
        assert [a["uid"] for a in initial] == ["10001", "20002", "30003"]

        # Invalid UID rejected
        bad = client.put("/api/accounts/reorder", json={"uids": ["10001", "invalid"]})
        assert bad.status_code == 400

        # Reorder accounts
        reordered = client.put(
            "/api/accounts/reorder",
            json={"uids": ["30003", "10001", "20002", "30003"]},
        )
        assert reordered.status_code == 200
        body = reordered.json()
        assert body["active_uid"] == "10001"
        assert [a["uid"] for a in body["accounts"]] == ["30003", "10001", "20002"]
        assert [a["sort_order"] for a in body["accounts"]] == [0, 1, 2]

        # Persists across subsequent GET /api/accounts, GET /api/dashboard, and enabled crawl order
        listed = client.get("/api/accounts").json()["accounts"]
        assert [a["uid"] for a in listed] == ["30003", "10001", "20002"]

        dash = client.get("/api/dashboard?hours=24").json()
        assert [a["uid"] for a in dash["accounts"]] == ["30003", "10001", "20002"]
        assert [a["uid"] for a in db.get_enabled_accounts()] == ["30003", "10001", "20002"]

        # Partial list keeps omitted existing accounts after the specified ones
        partial = client.put("/api/accounts/reorder", json={"uids": ["20002"]})
        assert partial.status_code == 200
        assert [a["uid"] for a in partial.json()["accounts"]] == ["20002", "30003", "10001"]

        # Deleting active account (10001) falls back to the first account in sorted order (20002)
        dlt = client.delete("/api/accounts/10001")
        assert dlt.status_code == 200
        assert dlt.json()["active_uid"] == "20002"


def test_run_crawl_wait_queues_behind_active_crawl(tmp_path: Path):
    db = Database(db_path=tmp_path / "queue.db")
    db.create_account("101", enabled=True, set_active=True)
    db.create_account("202", enabled=True, set_active=False)

    def make_client(cookie: str = "", **kwargs):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None

        async def get_relation_stat(uid: str):
            if uid == "101":
                await asyncio.sleep(0.05)
            return {"follower": int(uid) * 10, "following": 1}

        async def get_account_card(uid: str):
            return {
                "name": f"UP_{uid}",
                "face": f"https://example.com/{uid}.jpg",
                "follower": int(uid) * 10,
                "archive_count": 0,
            }

        client.get_relation_stat.side_effect = get_relation_stat
        client.get_account_card.side_effect = get_account_card
        client.get_all_videos.return_value = []
        return client

    service = CrawlService(db)

    async def scenario():
        t1 = asyncio.create_task(service.run_crawl(uid="101", force_sync_videos=True, wait=True))
        await asyncio.sleep(0.01)
        assert service.is_crawling is True
        t2 = asyncio.create_task(service.run_crawl(uid="202", force_sync_videos=True, wait=True))
        return await asyncio.gather(t1, t2)

    with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_client):
        r1, r2 = asyncio.run(scenario())

    assert r1["skipped"] is False
    assert r2["skipped"] is False
    summaries = {a["uid"]: a for a in db.get_accounts_summary()}
    assert summaries["101"]["latest_follower_count"] == 1010
    assert summaries["202"]["latest_follower_count"] == 2020
    assert summaries["202"]["account_name"] == "UP_202"


def test_scheduler_reschedule_preserves_existing_countdown(tmp_path: Path):
    from app.scheduler import JOB_ID, SchedulerManager

    db = Database(db_path=tmp_path / "sched.db")
    db.create_account("101", enabled=True, set_active=True)
    service = CrawlService(db)
    mgr = SchedulerManager(db, service)

    async def scenario():
        mgr.start()
        try:
            job_before = mgr.scheduler.get_job(JOB_ID)
            assert job_before is not None
            next_before = job_before.next_run_time

            # Adding another account with same interval_minutes must NOT reset next_run_time
            db.create_account("202", enabled=True, set_active=False)
            mgr.reschedule()
            job_after = mgr.scheduler.get_job(JOB_ID)
            assert job_after is not None
            assert job_after.next_run_time == next_before

            # Changing interval_minutes MUST reschedule with new interval
            db.update_settings({"interval_minutes": "10"})
            mgr.reschedule()
            job_changed = mgr.scheduler.get_job(JOB_ID)
            assert job_changed is not None
            assert int(job_changed.trigger.interval.total_seconds()) == 600
        finally:
            mgr.shutdown()

    asyncio.run(scenario())


def test_global_cookie_shared_across_accounts_and_test_connection(tmp_path: Path):
    db = Database(db_path=tmp_path / "global_cookie.db")
    app = create_app(database=db, enable_scheduler=False)

    seen_crawl_cookies: dict[str, str] = {}
    seen_test_cookies: list[str] = []

    def make_crawl_client(cookie: str = "", **kwargs):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None

        async def get_relation_stat(uid: str):
            seen_crawl_cookies[uid] = cookie
            return {"follower": 100, "following": 1}

        async def get_account_card(uid: str):
            return {"name": f"UP_{uid}", "face": "", "follower": 100, "archive_count": 0}

        client.get_relation_stat.side_effect = get_relation_stat
        client.get_account_card.side_effect = get_account_card
        client.get_all_videos.return_value = []
        return client

    def make_test_client(cookie: str = "", **kwargs):
        seen_test_cookies.append(cookie)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.__aexit__.return_value = None
        client.get_relation_stat.return_value = {"follower": 123, "following": 4}
        client.get_account_card.return_value = {"name": "UP_202", "face": "", "follower": 123, "archive_count": 0}
        client.get_videos_page_series.return_value = ([], 0)
        return client

    with TestClient(app) as client:
        assert client.post("/api/accounts", json={"uid": "101", "enabled": True}).status_code == 200
        assert client.post("/api/accounts", json={"uid": "202", "enabled": True}).status_code == 200
        assert client.put("/api/settings", json={"cookie": "SESSDATA=shared_global_abc"}).status_code == 200

        service = CrawlService(db)
        with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_crawl_client):
            res = asyncio.run(service.run_crawl_all(force_sync_videos=True))
        assert res["skipped"] is False
        assert seen_crawl_cookies == {
            "101": "SESSDATA=shared_global_abc",
            "202": "SESSDATA=shared_global_abc",
        }

        with patch("app.routers.settings.BilibiliClient", side_effect=make_test_client):
            test_resp = client.post("/api/test-connection", json={"uid": "202"})
        assert test_resp.status_code == 200
        assert test_resp.json()["ok"] is True
        assert seen_test_cookies == ["SESSDATA=shared_global_abc"]

        # Clearing global cookie clears it for all accounts
        clr = client.put("/api/settings", json={"cookie": ""})
        assert clr.status_code == 200
        assert clr.json()["has_cookie"] is False
        accs = client.get("/api/accounts").json()["accounts"]
        assert all(a["has_cookie"] is False for a in accs)


def test_database_migrates_legacy_account_cookie_to_global(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "legacy_acc_cookie.db"
    db = Database(db_path=db_path)
    # Simulate a legacy DB state before global_cookie_migrated_v1 existed
    with db.connection() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'global_cookie_migrated_v1'")
        conn.execute("UPDATE settings SET value = '' WHERE key = 'cookie'")
        conn.execute(
            """
            INSERT INTO monitored_accounts (
                uid, account_name, account_face, cookie, enabled, sort_order, created_at, updated_at
            ) VALUES ('101', 'A', '', 'SESSDATA=from_acc_101', 1, 0, '2026-01-01', '2026-01-01'),
                     ('202', 'B', '', '', 1, 1, '2026-01-01', '2026-01-01')
            """
        )

    migrated = Database(db_path=db_path)
    assert migrated.get_public_settings()["has_cookie"] is True
    assert migrated.get_effective_cookie("101") == "SESSDATA=from_acc_101"
    assert migrated.get_effective_cookie("202") == "SESSDATA=from_acc_101"

    # Legacy per-account cookie was cleared so global clear affects all accounts
    migrated.update_settings({"cookie": ""})
    assert migrated.get_effective_cookie("101") == ""
    assert migrated.get_effective_cookie("202") == ""
    with sqlite3.connect(db_path) as raw_conn:
        row = raw_conn.execute("SELECT cookie FROM monitored_accounts WHERE uid = '101'").fetchone()
        assert row is not None and row[0] == ""


def test_follower_daily_changes_beijing_natural_days(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    bjt = timezone(timedelta(hours=8))
    db = Database(db_path=tmp_path / "daily.db")
    uid = "3546100000000001"

    def insert_at(dt_bjt: datetime, followers: int | None):
        db.insert_snapshot(
            uid=uid,
            follower_count=followers,
            following_count=10,
            online_viewers_total=20,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success" if followers is not None else "failed",
            error_message=None,
            duration_ms=50,
            video_online_items=[],
            collected_at=dt_bjt,
        )

    # Now = 2026-11-18 13:50:00 BJT (UTC+8)
    now_bjt = datetime(2026, 11, 18, 13, 50, 0, tzinfo=bjt)
    now_ts = int(now_bjt.timestamp())

    # Empty uid / no snapshots
    empty_res = db.get_follower_daily_changes("", now_ts=now_ts)
    assert empty_res == {
        "yesterday": None,
        "today": None,
        "yesterday_date": "2026-11-17",
        "today_date": "2026-11-18",
        "timezone": "Asia/Shanghai",
    }
    assert db.get_follower_daily_changes(uid, now_ts=now_ts)["today"] is None

    # 1. Single snapshot today only -> today change is None (establishing baseline)
    insert_at(datetime(2026, 11, 18, 0, 34, 0, tzinfo=bjt), 19977)
    res_one = db.get_follower_daily_changes(uid, now_ts=now_ts)
    assert res_one["yesterday"] is None
    assert res_one["today"] is None

    # 2. Second snapshot today (still no yesterday data) -> today = 20343 - 19977 = +366
    insert_at(datetime(2026, 11, 18, 13, 42, 0, tzinfo=bjt), 20343)
    res_today_only = db.get_follower_daily_changes(uid, now_ts=now_ts)
    assert res_today_only["yesterday"] is None
    assert res_today_only["today"] == 366

    # 3. Add yesterday snapshots (2026-11-17 01:00 and 23:55 BJT, which spans UTC date boundary!)
    # Note: 2026-11-17 01:00 BJT == 2026-11-16 17:00 UTC, 2026-11-17 23:55 BJT == 2026-11-17 15:55 UTC
    insert_at(datetime(2026, 11, 17, 1, 0, 0, tzinfo=bjt), 19800)
    insert_at(datetime(2026, 11, 17, 23, 55, 0, tzinfo=bjt), 19970)
    # Failed snapshot today with None follower_count should be ignored
    insert_at(datetime(2026, 11, 18, 13, 45, 0, tzinfo=bjt), None)
    res_two_days = db.get_follower_daily_changes(uid, now_ts=now_ts)
    # Yesterday has no 2026-11-16 close -> uses yesterday first->last: 19970 - 19800 = 170
    assert res_two_days["yesterday"] == 170
    # Today uses yesterday close (19970) -> today last (20343): 20343 - 19970 = 373
    assert res_two_days["today"] == 373

    # 4. Add 2026-11-16 close (23:50 BJT) -> yesterday uses 2026-11-16 close (19750) -> 19970 - 19750 = 220
    insert_at(datetime(2026, 11, 16, 23, 50, 0, tzinfo=bjt), 19750)
    res_three_days = db.get_follower_daily_changes(uid, now_ts=now_ts)
    assert res_three_days["yesterday"] == 220
    assert res_three_days["today"] == 373

    # 5. Boundary check: 2026-11-18 07:30 BJT (still 2026-11-17 23:30 UTC!) belongs to 2026-11-18 BJT
    db2 = Database(db_path=tmp_path / "utc_boundary.db")
    db2.insert_snapshot(
        uid=uid,
        follower_count=1000,
        following_count=1,
        online_viewers_total=0,
        video_count=0,
        videos_succeeded=0,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=10,
        video_online_items=[],
        collected_at=datetime(2026, 11, 18, 0, 5, 0, tzinfo=bjt),  # 2026-11-17 16:05 UTC
    )
    db2.insert_snapshot(
        uid=uid,
        follower_count=988,
        following_count=1,
        online_viewers_total=0,
        video_count=0,
        videos_succeeded=0,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=10,
        video_online_items=[],
        collected_at=datetime(2026, 11, 18, 7, 30, 0, tzinfo=bjt),  # 2026-11-17 23:30 UTC
    )
    res_neg = db2.get_follower_daily_changes(
        uid, now_ts=int(datetime(2026, 11, 18, 8, 0, 0, tzinfo=bjt).timestamp())
    )
    assert res_neg["yesterday"] is None
    assert res_neg["today"] == -12


def test_dashboard_api_returns_follower_daily_independent_of_hours_filter(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    bjt = timezone(timedelta(hours=8))
    db = Database(db_path=tmp_path / "dash_daily.db")
    app = create_app(database=db, enable_scheduler=False)

    now_bjt = datetime.now(timezone.utc).astimezone(bjt)
    today_00_10 = now_bjt.replace(hour=0, minute=10, second=0, microsecond=0)
    yesterday_12_00 = today_00_10 - timedelta(hours=12, minutes=10)
    yesterday_23_50 = today_00_10 - timedelta(minutes=20)
    day_before_23_50 = yesterday_12_00 - timedelta(hours=12)

    with TestClient(app) as client:
        assert client.post("/api/accounts", json={"uid": "998877", "enabled": True}).status_code == 200
        for dt, followers in [
            (day_before_23_50, 5000),
            (yesterday_12_00, 5050),
            (yesterday_23_50, 5120),
            (today_00_10, 5135),
            (now_bjt, 5160),
        ]:
            db.insert_snapshot(
                uid="998877",
                follower_count=followers,
                following_count=10,
                online_viewers_total=5,
                video_count=1,
                videos_succeeded=1,
                extra_metrics={},
                status="success",
                error_message=None,
                duration_ms=20,
                video_online_items=[],
                collected_at=dt,
            )

        # Even with hours=1, yesterday & today BJT natural day stats remain complete
        dash = client.get("/api/dashboard?hours=1")
        assert dash.status_code == 200
        daily = dash.json()["follower_daily"]
        assert daily["yesterday"] == 120
        assert daily["today"] == 40
        assert daily["timezone"] == "Asia/Shanghai"
        assert daily["today_date"] == now_bjt.strftime("%Y-%m-%d")

        html_resp = client.get("/")
        assert html_resp.status_code == 200
        assert "kpiFollowersYesterday" in html_resp.text
        assert "kpiFollowersToday" in html_resp.text
        assert "followerDailyPanel" in html_resp.text


def test_video_history_hours_filter_and_downsampling(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    db = Database(db_path=tmp_path / "video_hours.db")
    db.update_settings({"uid": "556677"})
    db.upsert_videos(
        "556677",
        [{"bvid": "BV1win", "aid": 2001, "title": "Window Video", "play_count": 5000}],
    )

    now = datetime.now(timezone.utc).astimezone()
    # Insert 400 points over ~40 hours (every 6 minutes) with a distinct historical peak at -20h
    for idx in range(400):
        dt = now - timedelta(minutes=(399 - idx) * 6)
        online = 180 if idx == 199 else 10 + (idx % 15)
        plays = 1000 + idx * 10
        db.insert_snapshot(
            uid="556677",
            follower_count=100,
            following_count=1,
            online_viewers_total=online,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=20,
            video_online_items=[
                {
                    "bvid": "BV1win",
                    "online_count": online,
                    "online_raw": str(online),
                    "play_count": plays,
                }
            ],
            collected_at=dt,
        )

    app = create_app(database=db, enable_scheduler=False)
    with TestClient(app) as client:
        # 1-hour window: ~10 recent points, no -20h peak
        r_1h = client.get("/api/videos/BV1win/history?hours=1")
        assert r_1h.status_code == 200
        h_1h = r_1h.json()["history"]
        assert 9 <= len(h_1h) <= 12
        assert max(pt["online_count"] for pt in h_1h) < 30
        assert h_1h[-1]["play_count"] - h_1h[0]["play_count"] == (len(h_1h) - 1) * 10

        # 6-hour window: ~60 points
        r_6h = client.get("/api/videos/BV1win/history?hours=6")
        assert r_6h.status_code == 200
        h_6h = r_6h.json()["history"]
        assert 59 <= len(h_6h) <= 62
        assert max(pt["online_count"] for pt in h_6h) < 30

        # 24-hour window: ~240 points, includes the -20h peak of 180
        r_24h = client.get("/api/videos/BV1win/history?hours=24")
        assert r_24h.status_code == 200
        h_24h = r_24h.json()["history"]
        assert 238 <= len(h_24h) <= 242
        assert max(pt["online_count"] for pt in h_24h) == 180

        # All-time window (hours=0): 400 points downsampled, still preserves first/last play & peak
        r_all = client.get("/api/videos/BV1win/history?hours=0")
        assert r_all.status_code == 200
        h_all = r_all.json()["history"]
        assert len(h_all) <= 330
        assert h_all[0]["play_count"] == 1000
        assert h_all[-1]["play_count"] == 4990
        assert max(pt["online_count"] for pt in h_all) == 180

        # Modal time-window toolbar markup present in index.html
        html_resp = client.get("/")
        assert html_resp.status_code == 200
        assert 'id="modalRangeGroup"' in html_resp.text
        for h in ("1", "6", "24", "168", "720", "0"):
            assert f'data-hours="{h}"' in html_resp.text


def test_admin_password_db_auth_flow_and_protection(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BILI_ADMIN_PASSWORD", raising=False)
    db = Database(db_path=tmp_path / "admin_db.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        # Initially no admin password is set
        status0 = client.get("/api/admin/status")
        assert status0.status_code == 200
        assert status0.json() == {"enabled": False, "authenticated": True, "source": "none"}

        # Set admin password via settings
        put_pw = client.put(
            "/api/settings",
            json={"uid": "100001001", "admin_password": "secret-pass-01"},
        )
        assert put_pw.status_code == 200
        body_pw = put_pw.json()
        assert body_pw["admin_auth"] == {
            "enabled": True,
            "authenticated": False,
            "source": "db",
        }
        assert "secret-pass-01" not in put_pw.text
        assert "admin_password_hash" not in put_pw.text

        # Read-only endpoints remain accessible
        dash = client.get("/api/dashboard?hours=24")
        assert dash.status_code == 200
        assert dash.json()["admin_auth"] == {
            "enabled": True,
            "authenticated": False,
            "source": "db",
        }
        assert client.get("/api/accounts").status_code == 200
        assert client.get("/api/health").status_code == 200

        # Protected config & account endpoints reject unauthenticated requests
        assert client.get("/api/settings").status_code == 401
        assert client.put("/api/settings", json={"interval_minutes": 10}).status_code == 401
        assert client.post("/api/accounts", json={"uid": "123456"}).status_code == 401
        assert client.put("/api/accounts/100001001", json={"enabled": False}).status_code == 401
        assert client.put("/api/accounts/reorder", json={"uids": ["100001001"]}).status_code == 401
        assert client.post("/api/accounts/100001001/activate").status_code == 401
        assert client.delete("/api/accounts/100001001").status_code == 401
        assert client.post("/api/test-connection", json={"uid": "100001001"}).status_code == 401
        assert client.post("/api/crawl/now").status_code == 401

        # Wrong password returns 401
        bad_verify = client.post("/api/admin/verify", json={"password": "wrong"})
        assert bad_verify.status_code == 401

        # Correct password sets HttpOnly session cookie and unlocks endpoints
        ok_verify = client.post("/api/admin/verify", json={"password": "secret-pass-01"})
        assert ok_verify.status_code == 200
        assert ok_verify.json() == {
            "ok": True,
            "enabled": True,
            "authenticated": True,
            "source": "db",
        }
        assert "bilipulse_admin_session" in ok_verify.cookies

        assert client.get("/api/settings").status_code == 200
        assert client.post("/api/accounts", json={"uid": "123456", "enabled": True}).status_code == 200
        assert client.put("/api/settings", json={"interval_minutes": 10}).status_code == 200

        # Logout locks session again
        logout_resp = client.post("/api/admin/logout")
        assert logout_resp.status_code == 200
        assert logout_resp.json()["authenticated"] is False
        assert client.get("/api/settings").status_code == 401

        # Re-verify and clear DB password
        assert client.post("/api/admin/verify", json={"password": "secret-pass-01"}).status_code == 200
        clear_resp = client.put("/api/settings", json={"admin_password": ""})
        assert clear_resp.status_code == 200
        assert clear_resp.json()["admin_auth"] == {
            "enabled": False,
            "authenticated": True,
            "source": "none",
        }
        assert client.get("/api/settings").status_code == 200

        # Frontend HTML exposes centered admin auth dialog and password toggle
        html_resp = client.get("/")
        assert html_resp.status_code == 200
        assert "adminAuthModal" in html_resp.text
        assert "adminAuthForm" in html_resp.text
        assert "adminAuthInput" in html_resp.text
        assert "btnToggleAdminAuthPassword" in html_resp.text
        assert "inputAdminPassword" in html_resp.text


def test_admin_password_env_precedence_rate_limit_and_invalidation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BILI_ADMIN_PASSWORD", "env-master-2026")
    db = Database(db_path=tmp_path / "admin_env.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        status = client.get("/api/admin/status").json()
        assert status == {"enabled": True, "authenticated": False, "source": "env"}

        assert client.post("/api/accounts", json={"uid": "555555"}).status_code == 401
        assert client.post("/api/admin/verify", json={"password": "env-master-2026"}).status_code == 200

        # Env-managed password cannot be modified or cleared via web settings API
        override_resp = client.put("/api/settings", json={"admin_password": "new-pw"})
        assert override_resp.status_code == 400

        # Changing BILI_ADMIN_PASSWORD invalidates existing session cookies automatically
        monkeypatch.setenv("BILI_ADMIN_PASSWORD", "env-master-rotated")
        assert client.get("/api/settings").status_code == 401

        # Rate limiter blocks after 5 consecutive failed attempts
        for _ in range(5):
            r = client.post("/api/admin/verify", json={"password": "bad-guess"})
            assert r.status_code == 401
        locked = client.post("/api/admin/verify", json={"password": "env-master-rotated"})
        assert locked.status_code == 429


def test_default_admin_password_none_and_env_seed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BILI_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("BILI_DEFAULT_ADMIN_PASSWORD", raising=False)

    # 1) Out of the box with no env vars -> no password seeded
    unseeded_db = Database(db_path=tmp_path / "unseeded.db")
    unseeded_app = create_app(database=unseeded_db, enable_scheduler=False)
    with TestClient(unseeded_app) as client0:
        assert client0.get("/api/admin/status").json() == {
            "enabled": False,
            "authenticated": True,
            "source": "none",
        }
        assert client0.get("/api/settings").status_code == 200

    # 2) When BILI_DEFAULT_ADMIN_PASSWORD is set -> seeds initial DB password once
    monkeypatch.setenv("BILI_DEFAULT_ADMIN_PASSWORD", "seeded-admin-pass")
    db_path = tmp_path / "default_pw.db"
    db = Database(db_path=db_path)
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        status = client.get("/api/admin/status").json()
        assert status == {"enabled": True, "authenticated": False, "source": "db"}
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/admin/verify", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/admin/verify", json={"password": "seeded-admin-pass"}).status_code == 200
        assert client.get("/api/settings").status_code == 200

        # Clearing password stays cleared across DB re-initialization even if env var remains set
        assert client.put("/api/settings", json={"admin_password": ""}).status_code == 200

    reloaded_db = Database(db_path=db_path)
    app2 = create_app(database=reloaded_db, enable_scheduler=False)
    with TestClient(app2) as client2:
        assert client2.get("/api/admin/status").json() == {
            "enabled": False,
            "authenticated": True,
            "source": "none",
        }


def test_lazy_database_proxy_defers_init_and_forwards_attributes(tmp_path: Path, monkeypatch):
    import os
    import subprocess
    import sys
    import threading
    from app.database import _LazyDatabase

    # 1) Importing app.database and app.main in a fresh process must NOT create DB file
    sub_db_path = tmp_path / "sub_dir" / "never_touched.db"
    env = os.environ.copy()
    env["BILI_DB_PATH"] = str(sub_db_path)
    env["BILI_DATA_DIR"] = str(tmp_path / "sub_data")
    env["BILI_DEFAULT_ADMIN_PASSWORD"] = ""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.database, app.main; "
                "assert repr(app.database.db) == '<_LazyDatabase uninitialized>'; "
                "print('ok')"
            ),
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "ok"
    assert not sub_db_path.exists()

    # 2) Direct _LazyDatabase attribute forwarding & thread-safe single initialization
    lazy_path = tmp_path / "lazy_dir" / "lazy_unit.db"
    monkeypatch.setattr("app.database.DB_PATH", lazy_path)
    lazy = _LazyDatabase()
    assert not lazy_path.exists()
    assert repr(lazy) == "<_LazyDatabase uninitialized>"
    assert object.__getattribute__(lazy, "_instance") is None

    results: list[dict] = []
    barrier = threading.Barrier(4)

    def _worker() -> None:
        barrier.wait()
        results.append(lazy.get_public_settings())

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    assert lazy_path.exists()
    inst = object.__getattribute__(lazy, "_instance")
    assert isinstance(inst, Database)
    assert lazy.db_path == lazy_path
    assert repr(lazy) == repr(inst)

    lazy.custom_marker = "forwarded"
    assert inst.custom_marker == "forwarded"
    assert lazy.custom_marker == "forwarded"
    del lazy.custom_marker
    assert not hasattr(lazy, "custom_marker")


def test_upsert_videos_preserves_cached_duration_when_incoming_zero(tmp_path: Path):
    db = Database(db_path=tmp_path / "duration_preserve.db")
    uid = "100001001"

    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1dur",
                "aid": 100,
                "title": "Initial Title",
                "duration": 185,
                "play_count": 1000,
            }
        ],
    )
    v0 = {v["bvid"]: v for v in db.get_videos(uid)}
    assert v0["BV1dur"]["duration"] == 185

    # Incoming duration=0 (e.g., incomplete fallback item) must NOT clobber cached duration 185
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1dur",
                "aid": 100,
                "title": "Updated Title",
                "duration": 0,
                "play_count": 1500,
            },
            {
                "bvid": "BV1new_zero",
                "aid": 101,
                "title": "New Zero Duration",
                "duration": 0,
                "play_count": 50,
            },
        ],
    )
    v1 = {v["bvid"]: v for v in db.get_videos(uid)}
    assert v1["BV1dur"]["duration"] == 185
    assert v1["BV1dur"]["title"] == "Updated Title"
    assert v1["BV1dur"]["play_count"] == 1500
    assert v1["BV1new_zero"]["duration"] == 0

    # Positive incoming duration updates cached duration normally
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1dur",
                "aid": 100,
                "title": "Updated Title",
                "duration": 240,
                "play_count": 1600,
            }
        ],
    )
    v2 = {v["bvid"]: v for v in db.get_videos(uid)}
    assert v2["BV1dur"]["duration"] == 240


def test_delete_stale_videos_and_delete_account_purge_orphan_video_snapshots(tmp_path: Path):
    db = Database(db_path=tmp_path / "orphan_cleanup.db")

    # Verify composite index exists and optimize() runs cleanly
    with db.connection() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert "idx_videos_uid_online_pubdate" in indexes
    db.optimize()

    db.create_account("10001", enabled=True, set_active=True)
    db.create_account("20002", enabled=True, set_active=False)

    # Seed > 500 stale videos on 10001 to also exercise chunked SQLite parameter deletion
    stale_bvids = [f"BV1stale_{idx:03d}" for idx in range(520)]
    videos_1 = [{"bvid": "BV1keep", "aid": 1, "title": "Keep", "play_count": 100}] + [
        {"bvid": bvid, "aid": idx + 2, "title": f"Stale {idx}", "play_count": 10}
        for idx, bvid in enumerate(stale_bvids)
    ]
    db.upsert_videos("10001", videos_1)
    snap_1 = db.insert_snapshot(
        uid="10001",
        follower_count=1000,
        following_count=10,
        online_viewers_total=521,
        video_count=521,
        videos_succeeded=521,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=80,
        video_online_items=[
            {"bvid": v["bvid"], "online_count": 1, "online_raw": "2"} for v in videos_1
        ],
    )

    deleted_count = db.delete_stale_videos("10001", {"BV1keep"})
    assert deleted_count == 520
    assert [v["bvid"] for v in db.get_videos("10001")] == ["BV1keep"]
    with db.connection() as conn:
        remaining_1 = [
            r["bvid"]
            for r in conn.execute(
                "SELECT bvid FROM video_online_snapshots WHERE snapshot_id = ?", (snap_1,)
            ).fetchall()
        ]
    assert remaining_1 == ["BV1keep"]

    # Seed 20002 videos + snapshot, plus an orphan row attached to snap_1 for BV2_orphan
    db.upsert_videos(
        "20002",
        [
            {"bvid": "BV2_main", "aid": 2001, "title": "Main 2", "play_count": 300},
            {"bvid": "BV2_orphan", "aid": 2002, "title": "Orphan 2", "play_count": 400},
        ],
    )
    db.insert_snapshot(
        uid="20002",
        follower_count=2000,
        following_count=5,
        online_viewers_total=9,
        video_count=2,
        videos_succeeded=2,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=50,
        video_online_items=[
            {"bvid": "BV2_main", "online_count": 5, "online_raw": "6"},
            {"bvid": "BV2_orphan", "online_count": 4, "online_raw": "5"},
        ],
    )
    with db.connection() as conn:
        conn.execute(
            """
            INSERT INTO video_online_snapshots (snapshot_id, bvid, online_count, online_raw, play_count)
            VALUES (?, 'BV2_orphan', 3, '4', 400)
            """,
            (snap_1,),
        )

    res = db.delete_account("20002", delete_data=True)
    assert res == {"deleted": True, "uid": "20002", "active_uid": "10001"}
    assert db.count_snapshots("20002") == 0
    assert db.get_videos("20002") == []
    with db.connection() as conn:
        cnt_2 = conn.execute(
            "SELECT COUNT(*) AS cnt FROM video_online_snapshots WHERE bvid IN ('BV2_main', 'BV2_orphan')"
        ).fetchone()["cnt"]
        cnt_keep = conn.execute(
            "SELECT COUNT(*) AS cnt FROM video_online_snapshots WHERE bvid = 'BV1keep'"
        ).fetchone()["cnt"]
    assert cnt_2 == 0
    assert cnt_keep == 1


def test_get_accounts_summary_multi_account_deltas_and_video_count_fallback(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    db = Database(db_path=tmp_path / "summary_bulk.db")
    db.update_settings({"cookie": "SESSDATA=global_cookie_abcdef123456"})

    db.create_account("1001", cookie="", enabled=True, account_name="UP_1001", set_active=True)
    db.create_account(
        "1002",
        cookie="SESSDATA=override_cookie_999999888",
        enabled=True,
        account_name="UP_1002",
        set_active=False,
    )
    db.create_account("1003", cookie="", enabled=True, account_name="UP_1003", set_active=False)
    db.create_account("1004", cookie="", enabled=False, account_name="UP_1004", set_active=False)

    t0 = datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=5)

    # 1001: 3 rows in videos table + 2 snapshots (positive deltas)
    db.upsert_videos(
        "1001",
        [
            {"bvid": "BV1_1", "aid": 1, "title": "V1"},
            {"bvid": "BV1_2", "aid": 2, "title": "V2"},
            {"bvid": "BV1_3", "aid": 3, "title": "V3"},
        ],
    )
    db.insert_snapshot(
        uid="1001",
        follower_count=1000,
        following_count=10,
        online_viewers_total=20,
        video_count=3,
        videos_succeeded=3,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=40,
        video_online_items=[],
        collected_at=t0,
    )
    db.insert_snapshot(
        uid="1001",
        follower_count=1045,
        following_count=10,
        online_viewers_total=35,
        video_count=3,
        videos_succeeded=3,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=45,
        video_online_items=[],
        collected_at=t1,
    )

    # 1002: 0 rows in videos table -> falls back to latest snapshot video_count (9), negative deltas
    db.insert_snapshot(
        uid="1002",
        follower_count=500,
        following_count=5,
        online_viewers_total=18,
        video_count=9,
        videos_succeeded=9,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=30,
        video_online_items=[],
        collected_at=t0,
    )
    db.insert_snapshot(
        uid="1002",
        follower_count=488,
        following_count=5,
        online_viewers_total=11,
        video_count=9,
        videos_succeeded=8,
        extra_metrics={},
        status="partial",
        error_message="warn",
        duration_ms=35,
        video_online_items=[],
        collected_at=t1,
    )

    # 1003: only 1 snapshot -> deltas are None
    db.insert_snapshot(
        uid="1003",
        follower_count=300,
        following_count=2,
        online_viewers_total=4,
        video_count=2,
        videos_succeeded=2,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=25,
        video_online_items=[],
        collected_at=t1,
    )

    summaries = {a["uid"]: a for a in db.get_accounts_summary(crawling_uid="1002")}
    assert list(summaries.keys()) == ["1001", "1002", "1003", "1004"]

    assert summaries["1001"]["latest_follower_count"] == 1045
    assert summaries["1001"]["latest_online_viewers_total"] == 35
    assert summaries["1001"]["follower_delta"] == 45
    assert summaries["1001"]["online_delta"] == 15
    assert summaries["1001"]["video_count"] == 3
    assert summaries["1001"]["has_cookie"] is True
    assert summaries["1001"]["is_crawling"] is False

    assert summaries["1002"]["latest_follower_count"] == 488
    assert summaries["1002"]["latest_online_viewers_total"] == 11
    assert summaries["1002"]["follower_delta"] == -12
    assert summaries["1002"]["online_delta"] == -7
    assert summaries["1002"]["video_count"] == 9
    assert summaries["1002"]["last_crawl_status"] == "partial"
    assert summaries["1002"]["cookie_preview"] != summaries["1001"]["cookie_preview"]
    assert summaries["1002"]["is_crawling"] is True

    assert summaries["1003"]["latest_follower_count"] == 300
    assert summaries["1003"]["follower_delta"] is None
    assert summaries["1003"]["online_delta"] is None
    assert summaries["1003"]["video_count"] == 2

    assert summaries["1004"]["latest_follower_count"] is None
    assert summaries["1004"]["follower_delta"] is None
    assert summaries["1004"]["online_delta"] is None
    assert summaries["1004"]["video_count"] == 0
    assert summaries["1004"]["enabled"] is False


def test_prune_old_snapshots_retention_and_keep_latest_two_per_uid(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    db = Database(db_path=tmp_path / "retention.db")
    now = datetime.now(timezone.utc)

    for uid in ("111", "222", "333"):
        db.upsert_videos(uid, [{"bvid": f"BV_{uid}", "aid": int(uid), "title": f"Vid {uid}"}])

    def seed(uid: str, days_ago: int, followers: int) -> int:
        return db.insert_snapshot(
            uid=uid,
            follower_count=followers,
            following_count=1,
            online_viewers_total=5,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=20,
            video_online_items=[{"bvid": f"BV_{uid}", "online_count": 5, "online_raw": "6"}],
            collected_at=now - timedelta(days=days_ago),
        )

    # uid 111: 3 expired (>30d) + 2 recent (<30d) -> 3 deleted, 2 kept
    for d in (100, 60, 40, 10, 1):
        seed("111", d, 1000 - d)
    # uid 222: only 2 snapshots, BOTH expired (>30d) -> 0 deleted, both preserved!
    for d in (120, 95):
        seed("222", d, 2000 - d)
    # uid 333: 4 snapshots, ALL expired (>30d) -> oldest 2 deleted, newest 2 (-80d, -50d) preserved!
    for d in (200, 150, 80, 50):
        seed("333", d, 3000 - d)

    # 1) retention_days == 0 (unlimited) keeps everything
    db.update_settings({"snapshot_retention_days": "0"})
    assert db.get_public_settings()["snapshot_retention_days"] == 0
    assert db.prune_old_snapshots() == {"deleted_snapshots": 0, "retention_days": 0}
    assert db.prune_old_snapshots(0) == {"deleted_snapshots": 0, "retention_days": 0}
    assert db.count_snapshots("111") == 5
    assert db.count_snapshots("222") == 2
    assert db.count_snapshots("333") == 4

    # 2) retention_days == 30 deletes expired snapshots beyond top-2 per UID and cascades child rows
    db.update_settings({"snapshot_retention_days": "30"})
    pruned = db.prune_old_snapshots()
    assert pruned == {"deleted_snapshots": 5, "retention_days": 30}
    assert db.count_snapshots("111") == 2
    assert db.count_snapshots("222") == 2
    assert db.count_snapshots("333") == 2

    latest_333, prev_333 = db.get_latest_two_snapshots("333")
    assert latest_333 is not None and latest_333["follower_count"] == 2950  # -50d
    assert prev_333 is not None and prev_333["follower_count"] == 2920  # -80d

    # Child video_online_snapshots cascaded cleanly via ON DELETE CASCADE
    assert len(db.get_video_history("BV_111")) == 2
    assert len(db.get_video_history("BV_222")) == 2
    assert len(db.get_video_history("BV_333")) == 2
    with db.connection() as conn:
        total_child_rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM video_online_snapshots"
        ).fetchone()["cnt"]
    assert total_child_rows == 6


def test_parse_duration_seconds_and_wbi_fallback_duration():
    assert parse_duration_seconds("12:34") == 754
    assert parse_duration_seconds("1:02:03") == 3723
    assert parse_duration_seconds("00:08") == 8
    assert parse_duration_seconds(90) == 90
    assert parse_duration_seconds("45") == 45
    assert parse_duration_seconds(0) == 0
    assert parse_duration_seconds("") == 0
    assert parse_duration_seconds("-") == 0
    assert parse_duration_seconds(None) == 0
    assert parse_duration_seconds(True) == 0
    assert parse_duration_seconds("bad:val") == 0
    assert parse_duration_seconds("-15") == 0
    assert parse_duration_seconds("1:2:3:4") == 0

    async def scenario():
        uid = "100001001"
        async with BilibiliClient() as client:
            client._session_bootstrapped = True
            client._wbi_keys = ("a" * 32, "b" * 32)
            client._wbi_fetched_at = time.time()

            async def fake_request(url, params=None, referer=None, retries=2):
                if "recArchivesByKeywords" in url:
                    raise BilibiliAPIError("series unavailable", code=-352)
                if "wbi/arc/search" in url:
                    return {
                        "code": 0,
                        "data": {
                            "page": {"count": 3},
                            "list": {
                                "vlist": [
                                    {
                                        "bvid": "BV1mmss",
                                        "aid": 1,
                                        "title": "MMSS",
                                        "mid": int(uid),
                                        "length": "12:34",
                                        "play": 100,
                                    },
                                    {
                                        "bvid": "BV1hhmmss",
                                        "aid": 2,
                                        "title": "HHMMSS",
                                        "mid": int(uid),
                                        "length": "1:02:03",
                                        "play": 200,
                                    },
                                    {
                                        "bvid": "BV1numeric_fallback",
                                        "aid": 3,
                                        "title": "Fallback Duration",
                                        "mid": int(uid),
                                        "length": "",
                                        "duration": 55,
                                        "play": 300,
                                    },
                                ]
                            },
                        },
                    }
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.object(client, "_request_json", side_effect=fake_request):
                videos = await client.get_all_videos(uid)

            by_bvid = {v["bvid"]: v["duration"] for v in videos}
            assert by_bvid == {
                "BV1mmss": 754,
                "BV1hhmmss": 3723,
                "BV1numeric_fallback": 55,
            }

    asyncio.run(scenario())


def test_bilibili_client_cross_instance_credential_and_wbi_cache():
    import httpx

    BilibiliClient.clear_credential_cache()

    async def scenario():
        cookie = "SESSDATA=cached_session_token_abc"
        calls: list[str] = []
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"

        async def fake_get(url: str, **kwargs):
            calls.append(url)
            req = httpx.Request("GET", url)
            if "finger/spi" in url:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"b_3": "buvid3_cached", "b_4": "buvid4_cached"}},
                    request=req,
                )
            if "web-interface/nav" in url:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "wbi_img": {
                                "img_url": f"https://i0.hdslb.com/bfs/wbi/{img_key}.png",
                                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png",
                            }
                        },
                    },
                    request=req,
                )
            raise AssertionError(f"Unexpected GET {url}")

        async def fake_post(url: str, **kwargs):
            calls.append(url)
            req = httpx.Request("POST", url)
            if "GenWebTicket" in url:
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"ticket": "ticket_cached"}},
                    request=req,
                )
            raise AssertionError(f"Unexpected POST {url}")

        # 1) First client fetches session tokens + WBI keys from network and populates cache
        async with BilibiliClient(cookie=cookie) as client1:
            with (
                patch.object(client1._client, "get", side_effect=fake_get),
                patch.object(client1._client, "post", side_effect=fake_post),
            ):
                await client1.ensure_session_tokens()
                keys1 = await client1._get_wbi_keys()
        assert keys1 == (img_key, sub_key)
        assert len(calls) == 3

        # 2) Second client with same cookie reuses cached tokens & WBI keys with zero HTTP calls
        async with BilibiliClient(cookie=cookie) as client2:
            with (
                patch.object(
                    client2._client,
                    "get",
                    side_effect=AssertionError("Expected credential cache hit for GET"),
                ),
                patch.object(
                    client2._client,
                    "post",
                    side_effect=AssertionError("Expected credential cache hit for POST"),
                ),
            ):
                await client2.ensure_session_tokens()
                keys2 = await client2._get_wbi_keys()
            assert keys2 == (img_key, sub_key)
            assert client2._client.cookies.get("buvid3") == "buvid3_cached"
            assert client2._client.cookies.get("buvid4") == "buvid4_cached"
            assert client2._client.cookies.get("bili_ticket") == "ticket_cached"

        # 3) Bounded cache size eviction + clear_credential_cache()
        for idx in range(BilibiliClient.MAX_CREDENTIAL_CACHE_SIZE + 10):
            BilibiliClient._get_or_create_cache_entry(f"key_{idx}")
        assert len(BilibiliClient._CREDENTIAL_CACHE) == BilibiliClient.MAX_CREDENTIAL_CACHE_SIZE

        BilibiliClient.clear_credential_cache()
        assert len(BilibiliClient._CREDENTIAL_CACHE) == 0

        # After clearing cache, a third client fetches from network again
        calls.clear()
        async with BilibiliClient(cookie=cookie) as client3:
            with (
                patch.object(client3._client, "get", side_effect=fake_get),
                patch.object(client3._client, "post", side_effect=fake_post),
            ):
                await client3.ensure_session_tokens()
                keys3 = await client3._get_wbi_keys()
        assert keys3 == (img_key, sub_key)
        assert len(calls) == 3

    asyncio.run(scenario())


def test_bilibili_client_wbi_invalidation_and_412_vs_429_retry():
    import httpx

    async def scenario():
        async with BilibiliClient(cookie="SESSDATA=retry_test") as client:
            # 1) WBI cache invalidation on /wbi/ -403 and -412 while keeping session_tokens
            for err_code in (-403, -412):
                entry = BilibiliClient._get_or_create_cache_entry(client._cookie_key)
                entry["session_tokens"] = {"buvid3": "keep_me"}
                entry["session_fetched_at"] = time.time()
                entry["wbi_keys"] = ("a" * 32, "b" * 32)
                entry["wbi_fetched_at"] = time.time()
                client._wbi_keys = ("a" * 32, "b" * 32)
                client._wbi_fetched_at = time.time()

                wbi_url = "https://api.bilibili.com/x/space/wbi/arc/search?mid=1&w_rid=xyz"
                resp_err = httpx.Response(
                    200,
                    json={"code": err_code, "message": "wbi signature error"},
                    request=httpx.Request("GET", wbi_url),
                )
                with patch.object(client._client, "get", new=AsyncMock(return_value=resp_err)):
                    payload = await client._request_json(wbi_url, retries=2)
                assert payload["code"] == err_code
                assert client._wbi_keys is None
                assert "wbi_keys" not in BilibiliClient._CREDENTIAL_CACHE[client._cookie_key]
                assert BilibiliClient._CREDENTIAL_CACHE[client._cookie_key]["session_tokens"] == {
                    "buvid3": "keep_me"
                }

            # 2) HTTP 412 fails immediately on first attempt without retry or sleep
            url_412 = "https://api.bilibili.com/x/player/online/total"
            mock_get_412 = AsyncMock(
                return_value=httpx.Response(412, request=httpx.Request("GET", url_412))
            )
            mock_sleep_412 = AsyncMock()
            with (
                patch.object(client._client, "get", new=mock_get_412),
                patch("app.bilibili.client.asyncio.sleep", new=mock_sleep_412),
            ):
                with pytest.raises(BilibiliAPIError) as exc_412:
                    await client._request_json(url_412, retries=2)
            assert exc_412.value.code == 412
            assert mock_get_412.await_count == 1
            assert mock_sleep_412.await_count == 0

            # 3) HTTP 429 backs off exponentially (1.5s, 3.0s) and succeeds when retry recovers
            url_429 = "https://api.bilibili.com/x/relation/stat"
            req_429 = httpx.Request("GET", url_429)
            mock_get_429 = AsyncMock(
                side_effect=[
                    httpx.Response(429, request=req_429),
                    httpx.Response(429, request=req_429),
                    httpx.Response(200, json={"code": 0, "data": {"follower": 99}}, request=req_429),
                ]
            )
            mock_sleep_429 = AsyncMock()
            with (
                patch.object(client._client, "get", new=mock_get_429),
                patch("app.bilibili.client.asyncio.sleep", new=mock_sleep_429),
            ):
                recovered = await client._request_json(url_429, retries=2)
            assert recovered == {"code": 0, "data": {"follower": 99}}
            assert mock_get_429.await_count == 3
            assert [c.args[0] for c in mock_sleep_429.await_args_list] == [1.5, 3.0]

            # 4) Exhausted HTTP 429 raises BilibiliAPIError(code=429)
            mock_get_429_fail = AsyncMock(return_value=httpx.Response(429, request=req_429))
            with (
                patch.object(client._client, "get", new=mock_get_429_fail),
                patch("app.bilibili.client.asyncio.sleep", new=AsyncMock()),
            ):
                with pytest.raises(BilibiliAPIError) as exc_429:
                    await client._request_json(url_429, retries=1)
            assert exc_429.value.code == 429
            assert mock_get_429_fail.await_count == 2

    asyncio.run(scenario())


def test_collector_multi_cid_partial_fault_tolerance_and_412_circuit_breaker(tmp_path: Path):
    # 1) Multi-CID partial fault tolerance: retains succeeded CIDs and marks snapshot "partial"
    uid_partial = "777888"
    db_partial = Database(db_path=tmp_path / "partial_cid.db")
    db_partial.update_settings(
        {
            "uid": uid_partial,
            "request_delay_ms": "50",
            f"last_video_sync_ts_{uid_partial}": str(int(time.time())),
            f"video_sync_rule_{uid_partial}": VIDEO_SYNC_RULE,
        }
    )
    db_partial.upsert_videos(
        uid_partial,
        [
            {
                "bvid": "BV1multi",
                "aid": 1,
                "title": "Three Part Video",
                "play_count": 1000,
                "cids": [
                    {"cid": 101, "page": 1, "part": "P1"},
                    {"cid": 102, "page": 2, "part": "P2"},
                    {"cid": 103, "page": 3, "part": "P3"},
                ],
            },
            {
                "bvid": "BV1all_fail",
                "aid": 2,
                "title": "All CIDs Fail",
                "play_count": 500,
                "cids": [
                    {"cid": 201, "page": 1, "part": "P1"},
                    {"cid": 202, "page": 2, "part": "P2"},
                ],
            },
        ],
    )

    fake_partial_client = AsyncMock()
    fake_partial_client.__aenter__.return_value = fake_partial_client
    fake_partial_client.__aexit__.return_value = None
    fake_partial_client.get_relation_stat.return_value = {"follower": 888, "following": 3}
    fake_partial_client.get_account_card.return_value = {
        "name": "PartialUP",
        "face": "",
        "follower": 888,
        "archive_count": 2,
    }

    async def fake_online_partial(bvid: str, cid: int):
        if cid == 101:
            return (11, "11")  # adjusted -> 10
        if cid == 102:
            raise BilibiliAPIError("cid 102 timeout", code=-504)
        if cid == 103:
            return (6, "6")  # adjusted -> 5
        raise BilibiliAPIError("cid unavailable", code=-404)

    fake_partial_client.get_video_online_total.side_effect = fake_online_partial
    svc_partial = CrawlService(db_partial)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_partial_client):
        res_partial = asyncio.run(svc_partial.run_crawl(force_sync_videos=False))

    assert res_partial["status"] == "partial"
    assert res_partial["video_count"] == 2
    assert res_partial["videos_succeeded"] == 1
    assert res_partial["online_viewers_total"] == 15  # 10 + 5
    assert "BV1multi(cid=102)" in (res_partial["error_message"] or "")
    vids_partial = {v["bvid"]: v for v in db_partial.get_videos(uid_partial)}
    assert vids_partial["BV1multi"]["last_online_count"] == 15
    assert vids_partial["BV1multi"]["last_online_raw"] == "11+6"

    # 2) HTTP 412 circuit breaker: immediately halts remaining CIDs and unstarted videos
    uid_412 = "999000"
    db_412 = Database(db_path=tmp_path / "breaker_412.db")
    db_412.update_settings(
        {
            "uid": uid_412,
            "request_delay_ms": "50",
            f"last_video_sync_ts_{uid_412}": str(int(time.time())),
            f"video_sync_rule_{uid_412}": VIDEO_SYNC_RULE,
        }
    )
    seeded_412 = [
        {
            "bvid": "BV412_0",
            "aid": 1,
            "title": "Trigger 412",
            "pubdate": 1000,
            "cids": [
                {"cid": 10, "page": 1, "part": "P1"},
                {"cid": 11, "page": 2, "part": "P2"},
                {"cid": 12, "page": 3, "part": "P3"},
            ],
        }
    ] + [
        {
            "bvid": f"BV412_{i}",
            "aid": i + 1,
            "title": f"Queued {i}",
            "pubdate": 1000 - i,
            "cids": [{"cid": 100 + i, "page": 1, "part": "P1"}],
        }
        for i in range(1, 10)
    ]
    db_412.upsert_videos(uid_412, seeded_412)

    fake_412_client = AsyncMock()
    fake_412_client.__aenter__.return_value = fake_412_client
    fake_412_client.__aexit__.return_value = None
    fake_412_client.get_relation_stat.return_value = {"follower": 500, "following": 2}
    fake_412_client.get_account_card.return_value = {
        "name": "BreakerUP",
        "face": "",
        "follower": 500,
        "archive_count": 10,
    }

    polled_cids: list[int] = []

    async def fake_online_412(bvid: str, cid: int):
        polled_cids.append(cid)
        if cid == 11 or len(polled_cids) == 1:
            raise BilibiliAPIError("HTTP 412 anti-bot", code=412)
        return (3, "3")

    fake_412_client.get_video_online_total.side_effect = fake_online_412
    svc_412 = CrawlService(db_412)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_412_client):
        res_412 = asyncio.run(svc_412.run_crawl(force_sync_videos=False))

    assert res_412["status"] == "partial"
    assert "412" in (res_412["error_message"] or "")
    # Subsequent CIDs (11, 12) on BV412_0 and queued videos beyond Semaphore(4) were aborted!
    assert 12 not in polled_cids
    assert len(polled_cids) <= 4


def test_admin_rate_limiter_trust_proxy_max_keys_and_secure_cookie_header(
    tmp_path: Path, monkeypatch
):
    from starlette.requests import Request
    from app.auth import AdminVerifyRateLimiter

    monkeypatch.setenv("BILI_ADMIN_PASSWORD", "proxy-guard-pw")

    # 1) Default (BILI_TRUST_PROXY unset/false): ignores X-Forwarded-Proto and X-Forwarded-For
    monkeypatch.setenv("BILI_TRUST_PROXY", "false")
    db_untrusted = Database(db_path=tmp_path / "proxy_untrusted.db")
    app_untrusted = create_app(database=db_untrusted, enable_scheduler=False)
    with TestClient(app_untrusted) as client:
        ok_resp = client.post(
            "/api/admin/verify",
            json={"password": "proxy-guard-pw"},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert ok_resp.status_code == 200
        set_cookie_untrusted = ok_resp.headers.get("set-cookie", "").lower()
        assert "bilipulse_admin_session=" in set_cookie_untrusted
        assert "secure" not in set_cookie_untrusted

        assert client.post("/api/admin/logout").status_code == 200

        # Spoofing distinct X-Forwarded-For values cannot bypass rate limiting
        for idx in range(5):
            r = client.post(
                "/api/admin/verify",
                json={"password": "wrong"},
                headers={"X-Forwarded-For": f"198.51.100.{idx + 1}"},
            )
            assert r.status_code == 401
        blocked = client.post(
            "/api/admin/verify",
            json={"password": "proxy-guard-pw"},
            headers={"X-Forwarded-For": "203.0.113.99"},
        )
        assert blocked.status_code == 429

    # 2) BILI_TRUST_PROXY=true: honors X-Forwarded-For per client IP and X-Forwarded-Proto=https
    monkeypatch.setenv("BILI_TRUST_PROXY", "true")
    db_trusted = Database(db_path=tmp_path / "proxy_trusted.db")
    app_trusted = create_app(database=db_trusted, enable_scheduler=False)
    with TestClient(app_trusted) as client:
        for _ in range(5):
            r = client.post(
                "/api/admin/verify",
                json={"password": "wrong"},
                headers={"X-Forwarded-For": "198.51.100.10, 10.0.0.1"},
            )
            assert r.status_code == 401
        assert (
            client.post(
                "/api/admin/verify",
                json={"password": "proxy-guard-pw"},
                headers={"X-Forwarded-For": "198.51.100.10, 10.0.0.1"},
            ).status_code
            == 429
        )

        # Different forwarded IP remains allowed and receives Secure cookie over X-Forwarded-Proto: https
        ok_trusted = client.post(
            "/api/admin/verify",
            json={"password": "proxy-guard-pw"},
            headers={
                "X-Forwarded-For": "198.51.100.20, 10.0.0.1",
                "X-Forwarded-Proto": "https",
            },
        )
        assert ok_trusted.status_code == 200
        set_cookie_trusted = ok_trusted.headers.get("set-cookie", "").lower()
        assert "secure" in set_cookie_trusted

    # 3) max_keys bounded memory eviction (prunes expired first, then evicts oldest key)
    limiter = AdminVerifyRateLimiter(max_failures=3, window_seconds=60, max_keys=3)

    def _make_req(ip: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/admin/verify",
                "headers": [(b"x-forwarded-for", ip.encode("ascii"))],
                "client": ("127.0.0.1", 50000),
            }
        )

    clock = [100.0]
    with patch("app.auth.time.monotonic", side_effect=lambda: clock[0]):
        for idx, ip in enumerate(("10.0.0.1", "10.0.0.2", "10.0.0.3")):
            clock[0] = 100.0 + idx * 10.0
            limiter.record_failure(_make_req(ip))
        assert set(limiter._failures.keys()) == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}

        # At capacity with all keys unexpired -> evicts oldest (10.0.0.1)
        clock[0] = 130.0
        limiter.record_failure(_make_req("10.0.0.4"))
        assert len(limiter._failures) == 3
        assert set(limiter._failures.keys()) == {"10.0.0.2", "10.0.0.3", "10.0.0.4"}

        # Advance past window for 10.0.0.2 (110) and 10.0.0.3 (120), keeping 10.0.0.4 (130) valid
        clock[0] = 185.0
        limiter.record_failure(_make_req("10.0.0.5"))
        assert set(limiter._failures.keys()) == {"10.0.0.4", "10.0.0.5"}


def test_settings_retention_api_background_crawl_now_and_scheduler_optimizations(tmp_path: Path):
    from app.scheduler import RETENTION_JOB_ID, SchedulerManager

    db = Database(db_path=tmp_path / "api_sched_opt.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        # 1) SettingsOut & PUT /api/settings snapshot_retention_days (including 0 = unlimited)
        s0 = client.get("/api/settings")
        assert s0.status_code == 200
        assert s0.json()["snapshot_retention_days"] == 90
        assert client.get("/api/dashboard").json()["settings"]["snapshot_retention_days"] == 90

        s_15 = client.put("/api/settings", json={"snapshot_retention_days": 15})
        assert s_15.status_code == 200
        assert s_15.json()["snapshot_retention_days"] == 15

        s_zero = client.put("/api/settings", json={"snapshot_retention_days": 0})
        assert s_zero.status_code == 200
        assert s_zero.json()["snapshot_retention_days"] == 0
        assert client.get("/api/settings").json()["snapshot_retention_days"] == 0
        assert client.get("/api/dashboard").json()["settings"]["snapshot_retention_days"] == 0

        assert client.put("/api/settings", json={"snapshot_retention_days": -1}).status_code == 422
        assert client.put("/api/settings", json={"snapshot_retention_days": 3651}).status_code == 422

        # 2) POST /api/crawl/now?background=true: 400 invalid/missing UID, 200 accepted, 409 when busy
        assert client.post("/api/crawl/now?background=true").status_code == 400
        assert client.post("/api/crawl/now?background=true&uid=not_digits").status_code == 400

        assert client.put("/api/settings", json={"uid": "100001001"}).status_code == 200

        bg_calls: list[tuple[bool, str | None]] = []

        async def fake_bg_crawl(
            force_sync_videos: bool = False,
            uid: str | None = None,
            wait: bool = False,
        ):
            bg_calls.append((force_sync_videos, uid))
            return {"skipped": False, "uid": uid, "status": "success"}

        with patch.object(app.state.crawl_service, "run_crawl", side_effect=fake_bg_crawl):
            ok_bg = client.post("/api/crawl/now?background=true&force_sync_videos=true")
            assert ok_bg.status_code == 200
            assert ok_bg.json() == {
                "accepted": True,
                "background": True,
                "uid": "100001001",
            }
        assert bg_calls == [(True, "100001001")]

        # Busy lock -> 409 Conflict
        with patch.object(
            type(app.state.crawl_service),
            "is_crawling",
            new_callable=lambda: property(lambda self: True),
        ):
            busy_bg = client.post("/api/crawl/now?background=true")
            assert busy_bg.status_code == 409

        # 3) /api/dashboard deduplicates settings and latest_two_snapshots queries
        db.insert_snapshot(
            uid="100001001",
            follower_count=1234,
            following_count=5,
            online_viewers_total=10,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=25,
            video_online_items=[],
        )
        with (
            patch.object(
                db, "get_public_settings", wraps=db.get_public_settings
            ) as spy_pub_settings,
            patch.object(
                db, "get_latest_two_snapshots", wraps=db.get_latest_two_snapshots
            ) as spy_latest_two,
        ):
            dash_resp = client.get("/api/dashboard?hours=24")
            assert dash_resp.status_code == 200
            assert dash_resp.json()["scheduler"]["last_crawl_status"] == "success"
            assert spy_pub_settings.call_count == 1
            assert spy_latest_two.call_count == 1

    # 4) SchedulerManager tracked background tasks, retention job, and get_status deduplication
    svc = CrawlService(db)
    mgr = SchedulerManager(db, svc)

    pre_pub = db.get_public_settings(uid="100001001")
    with (
        patch.object(
            db,
            "get_public_settings",
            side_effect=AssertionError("pub_settings was already provided"),
        ),
        patch.object(
            db,
            "get_latest_two_snapshots",
            side_effect=AssertionError("latest_snapshot was already provided"),
        ),
    ):
        st_none = mgr.get_status(uid="100001001", pub_settings=pre_pub, latest_snapshot=None)
        assert st_none["last_crawl_at"] is None
        assert st_none["last_crawl_status"] is None

        st_snap = mgr.get_status(
            uid="100001001",
            pub_settings=pre_pub,
            latest_snapshot={"collected_at": "2026-11-18T12:00:00+08:00", "status": "partial"},
        )
        assert st_snap["last_crawl_at"] == "2026-11-18T12:00:00+08:00"
        assert st_snap["last_crawl_status"] == "partial"

    async def sched_scenario():
        mgr.start()
        try:
            assert mgr.scheduler.get_job(RETENTION_JOB_ID) is not None

            # Normal task tracked while running and removed on completion
            gate = asyncio.Event()

            async def _short_task() -> str:
                await gate.wait()
                return "done"

            t_ok = mgr.spawn_background_task(_short_task(), name="test_short_bg")
            assert t_ok in mgr._bg_tasks
            gate.set()
            await t_ok
            await asyncio.sleep(0)
            assert t_ok not in mgr._bg_tasks

            # Failing task logs exception cleanly and removes itself from _bg_tasks
            async def _failing_task() -> None:
                raise RuntimeError("expected test error")

            with patch("app.scheduler.logger.exception") as mock_log_exc:
                t_err = mgr.spawn_background_task(_failing_task(), name="test_fail_bg")
                await asyncio.sleep(0.02)
                assert t_err not in mgr._bg_tasks
                assert mock_log_exc.called

            # Long-running task is cancelled when shutdown() runs
            async def _sleeper() -> None:
                await asyncio.sleep(60)

            t_long = mgr.spawn_background_task(_sleeper(), name="test_long_bg")
            assert t_long in mgr._bg_tasks
        finally:
            mgr.shutdown()

        await asyncio.sleep(0)
        assert t_long.cancelled()
        assert len(mgr._bg_tasks) == 0

    asyncio.run(sched_scenario())


def test_batch_insert_snapshot_play_fallback_effective_cookie_and_settings_prune_dispatch(
    tmp_path: Path,
):
    db = Database(db_path=tmp_path / "batch_and_profile.db")
    uid = "100001001"
    db.create_account(uid, cookie="", enabled=True, set_active=True)
    db.update_settings({"cookie": "SESSDATA=global_profile_cookie_xyz"})

    # 1) insert_snapshot bulk missing play_count lookup from videos + executemany
    db.upsert_videos(
        uid,
        [
            {"bvid": "BV_fallback", "aid": 1, "title": "Fallback Play", "play_count": 4321},
            {"bvid": "BV_explicit", "aid": 2, "title": "Explicit Play", "play_count": 1000},
        ],
    )
    db.insert_snapshot(
        uid=uid,
        follower_count=900,
        following_count=10,
        online_viewers_total=15,
        video_count=2,
        videos_succeeded=2,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=30,
        video_online_items=[
            {"bvid": "BV_fallback", "online_count": 7, "online_raw": "8"},
            {"bvid": "BV_explicit", "online_count": 8, "online_raw": "9", "play_count": 1111},
        ],
    )
    assert db.get_video_history("BV_fallback")[0]["play_count"] == 4321
    assert db.get_video_history("BV_explicit")[0]["play_count"] == 1111

    # 2) CrawlService.fetch_account_profile resolves effective cookie when cookie is None
    seen_cookies: list[str] = []

    def make_profile_client(cookie: str = "", **kwargs):
        seen_cookies.append(cookie)
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_account_card.return_value = {
            "name": "FetchedName",
            "face": "https://example.com/fetched.jpg",
            "follower": 900,
            "archive_count": 2,
        }
        return c

    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_profile_client):
        asyncio.run(svc.fetch_account_profile(uid, cookie=None))
    assert seen_cookies == ["SESSDATA=global_profile_cookie_xyz"]
    assert db.get_public_settings(uid=uid)["account_name"] == "FetchedName"

    # 3) PUT /api/settings spawns prune_old_snapshots when enable_scheduler=True and retention > 0
    app = create_app(database=db, enable_scheduler=False)
    app.state.enable_scheduler = True
    with TestClient(app) as client:
        with patch.object(
            db,
            "prune_old_snapshots",
            wraps=db.prune_old_snapshots,
        ) as spy_prune:
            r_zero = client.put("/api/settings", json={"snapshot_retention_days": 0})
            assert r_zero.status_code == 200
            assert spy_prune.call_count == 0

            r_pos = client.put("/api/settings", json={"snapshot_retention_days": 60})
            assert r_pos.status_code == 200
            assert spy_prune.call_count == 1
            assert spy_prune.call_args.args == (60,)


def test_create_account_bootstrap_and_fetch_profile_empty_cookie_preserve_global_cookie(
    tmp_path: Path,
):
    db = Database(db_path=tmp_path / "bootstrap_global_cookie.db")
    db.update_settings({"cookie": "SESSDATA=global_bootstrap_tok", "request_delay_ms": "50"})

    seen_cookies: list[str] = []

    def make_client(cookie: str = "", **kwargs):
        seen_cookies.append(cookie)
        c = AsyncMock()
        c.__aenter__.return_value = c
        c.__aexit__.return_value = None
        c.get_account_card.return_value = {
            "name": "BootstrapUP",
            "face": "https://example.com/boot.jpg",
            "follower": 777,
            "archive_count": 0,
        }
        c.get_relation_stat.return_value = {"follower": 777, "following": 9}
        c.get_all_videos.return_value = []
        return c

    # 1) Direct fetch_account_profile with "" and whitespace cookie falls back to global cookie
    db.create_account("555111", cookie="", enabled=False, set_active=False)
    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_client):
        asyncio.run(svc.fetch_account_profile("555111", cookie=""))
        asyncio.run(svc.fetch_account_profile("555111", cookie="   "))
    assert seen_cookies == [
        "SESSDATA=global_bootstrap_tok",
        "SESSDATA=global_bootstrap_tok",
    ]

    # 2) POST /api/accounts (omitting cookie -> default "") under enable_scheduler=True
    seen_cookies.clear()
    app = create_app(database=db, enable_scheduler=True)
    with patch("app.bilibili.collectors.BilibiliClient", side_effect=make_client):
        with TestClient(app) as client:
            resp = client.post("/api/accounts", json={"uid": "889900", "enabled": True})
            assert resp.status_code == 200

            # Wait briefly for tracked bootstrap background task to complete inside lifespan
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                accs = {a["uid"]: a for a in client.get("/api/accounts").json()["accounts"]}
                if (
                    accs["889900"]["account_name"] == "BootstrapUP"
                    and accs["889900"]["latest_follower_count"] == 777
                ):
                    break
                time.sleep(0.02)

            accs_final = {a["uid"]: a for a in client.get("/api/accounts").json()["accounts"]}
            assert accs_final["889900"]["account_name"] == "BootstrapUP"
            assert accs_final["889900"]["latest_follower_count"] == 777

    # Both fetch_account_profile and run_crawl used global cookie, never ""
    assert len(seen_cookies) >= 2
    assert all(c == "SESSDATA=global_bootstrap_tok" for c in seen_cookies)


def test_bilibili_client_partial_session_token_healing_and_cookie_isolation():
    import httpx

    BilibiliClient.clear_credential_cache()

    async def scenario():
        cookie_heal = "SESSDATA=heal_session_account"
        spi_attempts = 0
        ticket_attempts = 0

        async def spi_fail_first(url: str, **kwargs):
            nonlocal spi_attempts
            spi_attempts += 1
            req = httpx.Request("GET", url)
            if spi_attempts == 1:
                raise httpx.ConnectError("transient spi failure", request=req)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"b_3": "buvid3_healed", "b_4": "buvid4_healed"}},
                request=req,
            )

        async def ticket_ok_once(url: str, **kwargs):
            nonlocal ticket_attempts
            ticket_attempts += 1
            if ticket_attempts > 1:
                raise AssertionError("GenWebTicket should be reused from cache without re-fetching")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"ticket": "ticket_initial_ok"}},
                request=httpx.Request("POST", url),
            )

        # 1) Client 1: finger/spi fails transiently while GenWebTicket succeeds
        async with BilibiliClient(cookie=cookie_heal) as c1:
            with (
                patch.object(c1._client, "get", side_effect=spi_fail_first),
                patch.object(c1._client, "post", side_effect=ticket_ok_once),
            ):
                await c1.ensure_session_tokens()
            assert c1._client.cookies.get("bili_ticket") == "ticket_initial_ok"
            assert c1._client.cookies.get("buvid3") is None
        assert spi_attempts == 1
        assert ticket_attempts == 1

        # 2) Client 2 (same cookie): reuses cached bili_ticket (0 POSTs) and heals missing buvid3
        async with BilibiliClient(cookie=cookie_heal) as c2:
            with (
                patch.object(c2._client, "get", side_effect=spi_fail_first),
                patch.object(c2._client, "post", side_effect=ticket_ok_once),
            ):
                await c2.ensure_session_tokens()
            assert c2._client.cookies.get("bili_ticket") == "ticket_initial_ok"
            assert c2._client.cookies.get("buvid3") == "buvid3_healed"
            assert c2._client.cookies.get("buvid4") == "buvid4_healed"
        assert spi_attempts == 2
        assert ticket_attempts == 1

        # 3) Client 3 (same cookie): full cache hit for merged tokens with 0 HTTP calls
        async with BilibiliClient(cookie=cookie_heal) as c3:
            with (
                patch.object(
                    c3._client,
                    "get",
                    side_effect=AssertionError("Expected full session token cache hit"),
                ),
                patch.object(
                    c3._client,
                    "post",
                    side_effect=AssertionError("Expected full session token cache hit"),
                ),
            ):
                await c3.ensure_session_tokens()
            assert c3._client.cookies.get("bili_ticket") == "ticket_initial_ok"
            assert c3._client.cookies.get("buvid3") == "buvid3_healed"

        # 4) Cookie isolation: distinct cookies never leak bili_ticket / buvid3 across accounts
        for label, raw_cookie in (("A", "SESSDATA=acc_a"), ("B", "SESSDATA=acc_b")):
            async with BilibiliClient(cookie=raw_cookie) as client_seed:
                with (
                    patch.object(
                        client_seed._client,
                        "get",
                        new=AsyncMock(
                            return_value=httpx.Response(
                                200,
                                json={
                                    "code": 0,
                                    "data": {"b_3": f"buvid3_{label}", "b_4": f"buvid4_{label}"},
                                },
                                request=httpx.Request("GET", "https://api.bilibili.com/spi"),
                            )
                        ),
                    ),
                    patch.object(
                        client_seed._client,
                        "post",
                        new=AsyncMock(
                            return_value=httpx.Response(
                                200,
                                json={"code": 0, "data": {"ticket": f"ticket_{label}"}},
                                request=httpx.Request("POST", "https://api.bilibili.com/ticket"),
                            )
                        ),
                    ),
                ):
                    await client_seed.ensure_session_tokens()

        async with (
            BilibiliClient(cookie="SESSDATA=acc_a") as ca,
            BilibiliClient(cookie="SESSDATA=acc_b") as cb,
        ):
            for c_inst in (ca, cb):
                c_inst._client.get = AsyncMock(side_effect=AssertionError("unexpected GET"))
                c_inst._client.post = AsyncMock(side_effect=AssertionError("unexpected POST"))
            await ca.ensure_session_tokens()
            await cb.ensure_session_tokens()

            assert ca._client.cookies.get("buvid3") == "buvid3_A"
            assert ca._client.cookies.get("bili_ticket") == "ticket_A"
            assert cb._client.cookies.get("buvid3") == "buvid3_B"
            assert cb._client.cookies.get("bili_ticket") == "ticket_B"

    asyncio.run(scenario())


def test_video_engagement_stats_client_db_collector_and_api(tmp_path: Path):
    # 1) BilibiliClient.get_video_stat parses view/like/coin safely
    async def check_client():
        async with BilibiliClient() as client:
            async def fake_request(url, params=None, referer=None, retries=2):
                assert "web-interface/view" in url
                assert params == {"bvid": "BV1stat"}
                return {
                    "code": 0,
                    "data": {
                        "owner": {"mid": 12345},
                        "stat": {"view": 20000, "like": 1520, "coin": 340},
                    },
                }

            with patch.object(client, "_request_json", side_effect=fake_request):
                stat = await client.get_video_stat("BV1stat")
                owner = await client.get_video_owner_mid("BV1stat")
                assert stat == {"play_count": 20000, "like_count": 1520, "coin_count": 340}
                assert owner == "12345"

    asyncio.run(check_client())

    # 2) Legacy DB schema migration adds like_count, coin_count, stats_synced_at and preserves existing stats
    import sqlite3

    legacy_db_path = tmp_path / "legacy_videos.db"
    conn = sqlite3.connect(str(legacy_db_path))
    conn.executescript(
        """
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE videos (
            bvid TEXT PRIMARY KEY,
            uid TEXT NOT NULL,
            aid INTEGER NOT NULL,
            title TEXT NOT NULL,
            pic TEXT NOT NULL DEFAULT '',
            pubdate INTEGER NOT NULL DEFAULT 0,
            duration INTEGER NOT NULL DEFAULT 0,
            play_count INTEGER NOT NULL DEFAULT 0,
            cids_json TEXT NOT NULL DEFAULT '[]',
            last_online_count INTEGER NOT NULL DEFAULT 0,
            last_online_raw TEXT NOT NULL DEFAULT '',
            last_checked_at TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO videos (bvid, uid, aid, title, play_count, updated_at)
        VALUES ('BV1legacy', '555666', 1, 'Legacy Video', 1000, '2026-01-01T00:00:00+08:00');
        """
    )
    conn.commit()
    conn.close()

    db = Database(db_path=legacy_db_path)
    legacy_vids = db.get_videos("555666")
    assert len(legacy_vids) == 1
    assert legacy_vids[0]["like_count"] == 0
    assert legacy_vids[0]["coin_count"] == 0
    assert legacy_vids[0]["stats_synced_at"] is None

    db.update_video_stats_batch(
        [{"bvid": "BV1legacy", "play_count": 1200, "like_count": 96, "coin_count": 24}]
    )
    # Upsert from list API (without like/coin) must preserve synced stats
    db.upsert_videos(
        "555666",
        [{"bvid": "BV1legacy", "aid": 1, "title": "Legacy Video Updated", "play_count": 1250}],
    )
    preserved = db.get_videos("555666")[0]
    assert preserved["title"] == "Legacy Video Updated"
    assert preserved["play_count"] == 1250
    assert preserved["like_count"] == 96
    assert preserved["coin_count"] == 24
    assert preserved["stats_synced_at"] is not None

    # 3) Collector refreshes stats during sync crawl and skips during non-sync crawl
    uid = "777888"
    db2 = Database(db_path=tmp_path / "stats_crawl.db")
    db2.update_settings({"uid": uid, "request_delay_ms": "50"})

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 9000, "following": 10}
    fake_client.get_account_card.return_value = {
        "name": "StatsUP",
        "face": "",
        "follower": 9000,
        "archive_count": 1,
    }
    fake_client.get_all_videos.return_value = [
        {
            "bvid": "BV1S",
            "aid": 11,
            "title": "Stat Video",
            "pic": "",
            "pubdate": 1000,
            "duration": 120,
            "play_count": 10000,
        }
    ]
    fake_client.get_video_stat.return_value = {
        "play_count": 10200,
        "like_count": 816,
        "coin_count": 204,
    }
    fake_client.get_video_cids.return_value = [{"cid": 901, "page": 1, "part": "P1"}]
    fake_client.get_video_online_total.return_value = (15, "15")

    svc = CrawlService(db2)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        res1 = asyncio.run(svc.run_crawl(force_sync_videos=False))
        assert res1["status"] == "success"
        assert fake_client.get_video_stat.await_count == 1
        snap1 = db2.get_latest_two_snapshots(uid)[0]
        assert snap1["extra_metrics"]["total_video_plays"] == 10200

        # Non-sync crawl should NOT re-fetch get_video_stat
        res2 = asyncio.run(svc.run_crawl(force_sync_videos=False))
        assert res2["status"] == "success"
        assert fake_client.get_video_stat.await_count == 1

    app = create_app(database=db2, enable_scheduler=False)
    with TestClient(app) as client:
        dash = client.get("/api/dashboard?hours=24").json()
        assert len(dash["top_videos"]) == 1
        top_v = dash["top_videos"][0]
        assert top_v["play_count"] == 10200
        assert top_v["like_count"] == 816
        assert top_v["coin_count"] == 204
        assert top_v["stats_synced_at"] is not None

        vids_resp = client.get(f"/api/videos?uid={uid}").json()
        assert vids_resp["videos"][0]["like_count"] == 816
        assert vids_resp["videos"][0]["coin_count"] == 204


def test_tiered_hot_cold_polling_stats_quota_and_cid_reuse(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from app.bilibili.collectors import COLD_POLL_BATCH_SIZE, STATS_REFRESH_QUOTA

    uid = "888000111"
    db = Database(db_path=tmp_path / "tiered.db")
    now_dt = datetime.now(timezone.utc).astimezone()
    now_epoch = int(now_dt.timestamp())
    db.update_settings(
        {
            "uid": uid,
            "request_delay_ms": "50",
            f"last_video_sync_ts_{uid}": str(now_epoch),
            f"last_full_video_sync_ts_{uid}": str(now_epoch),
            f"video_sync_rule_{uid}": VIDEO_SYNC_RULE,
            f"last_archive_count_{uid}": "150",
        }
    )

    # Seed 150 old videos; BV_000..BV_004 are old low-play long-tail videos with online = 5 (T0)
    seeded = []
    for idx in range(150):
        bvid = f"BV_{idx:03d}"
        seeded.append(
            {
                "bvid": bvid,
                "aid": 10000 + idx,
                "title": f"Video {idx}",
                "pubdate": 1000 + idx,
                "play_count": (idx + 1) * 100,
                # Leave BV_149 without CIDs so we can verify get_video_stat CID reuse
                "cids": [] if idx == 149 else [{"cid": 50000 + idx, "page": 1, "part": "P1"}],
            }
        )
    db.upsert_videos(uid, seeded)

    # Mark 5 oldest low-play videos (BV_000..BV_004) as having active online viewers checked 3 min ago (due in T0)
    db.insert_snapshot(
        uid=uid,
        follower_count=5000,
        following_count=10,
        online_viewers_total=25,
        video_count=150,
        videos_succeeded=5,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=100,
        video_online_items=[
            {"bvid": f"BV_{idx:03d}", "online_count": 5, "online_raw": "6"}
            for idx in range(5)
        ],
        collected_at=now_dt - timedelta(minutes=3),
    )

    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 5010, "following": 10}
    fake_client.get_account_card.return_value = {
        "name": "LargeCreator",
        "face": "",
        "follower": 5010,
        "archive_count": 150,
    }

    stat_called_bvids: list[str] = []
    online_polled_bvids: list[str] = []

    async def fake_stat(bvid: str):
        stat_called_bvids.append(bvid)
        idx = int(bvid.split("_")[1])
        res = {
            "play_count": (idx + 1) * 100 + 10,
            "like_count": idx + 10,
            "coin_count": idx + 2,
        }
        if bvid == "BV_149":
            res["cids"] = [{"cid": 50149, "page": 1, "part": "P1"}]
        return res

    async def fake_online(bvid: str, cid: int):
        online_polled_bvids.append(bvid)
        idx = int(bvid.split("_")[1])
        # Keep BV_000..BV_004 at 5 viewers (raw 6), plus cold discovery on BV_005 -> 4 viewers (raw 5)
        if idx < 5:
            return (6, "6")
        if idx == 5:
            return (5, "5")
        return (1, "1")  # adjusted -> 0

    fake_client.get_video_stat.side_effect = fake_stat
    fake_client.get_video_online_total.side_effect = fake_online
    fake_client.get_video_cids.side_effect = AssertionError(
        "Expected CIDs for BV_149 to be populated from get_video_stat pages"
    )

    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        with patch("app.bilibili.collectors.asyncio.sleep", new=AsyncMock()):
            res1 = asyncio.run(svc.run_crawl(force_sync_videos=False))

            assert res1["status"] == "success"
            assert res1["video_count"] == 150
            # T0 due: BV_000..BV_004 (5 active) + background due cap (COLD_POLL_BATCH_SIZE = 30, including BV_149)
            assert res1["videos_succeeded"] == 5 + COLD_POLL_BATCH_SIZE
            # All 5 long-tail active videos were prioritized into T0
            for idx in range(5):
                assert f"BV_{idx:03d}" in online_polled_bvids
            # Stats refresh strictly capped at STATS_REFRESH_QUOTA (30) and prioritized T0/showcase
            assert len(stat_called_bvids) == STATS_REFRESH_QUOTA
            assert "BV_149" in stat_called_bvids
            assert "BV_000" in stat_called_bvids

            # Crawl 2 (immediately after crawl 1): T0 videos just checked -> skipped; background rotates to next 30 unchecked
            stat_called_round1 = set(stat_called_bvids)
            polled_round1 = set(online_polled_bvids)
            stat_called_bvids.clear()
            online_polled_bvids.clear()

            res2 = asyncio.run(svc.run_crawl(force_sync_videos=False))
            assert res2["status"] == "success"
            assert len(stat_called_bvids) == STATS_REFRESH_QUOTA
            # Disjoint unsynced batch of 30 videos refreshed in round 2
            assert stat_called_round1.isdisjoint(set(stat_called_bvids))
            # Cold pool rotated to previously unchecked videos
            newly_polled_cold = set(online_polled_bvids) - polled_round1
            assert len(newly_polled_cold) == COLD_POLL_BATCH_SIZE
            # Carryover preserved online total of unpolled T0 videos (5 * 5 = 25)
            assert res2["online_viewers_total"] == 25


def test_incremental_first_page_sync_vs_full_pagination_on_delete(tmp_path: Path):
    # 1) BilibiliClient.get_all_videos(max_pages=1) stops after page 1
    async def check_max_pages():
        uid = "900100200"
        async with BilibiliClient() as client:
            client._session_bootstrapped = True
            requested_pages: list[int] = []

            async def fake_req(url, params=None, referer=None, retries=2):
                requested_pages.append(params["pn"])
                return {
                    "code": 0,
                    "data": {
                        "page": {"total": 250},
                        "archives": [
                            {
                                "bvid": f"BV_p{params['pn']}",
                                "aid": params["pn"],
                                "title": f"Page {params['pn']}",
                                "upMid": int(uid),
                                "stat": {"view": 100},
                            }
                        ],
                    },
                }

            with patch.object(client, "_request_json", side_effect=fake_req):
                vids = await client.get_all_videos(uid, max_pages=1)
                assert requested_pages == [1]
                assert [v["bvid"] for v in vids] == ["BV_p1"]

    asyncio.run(check_max_pages())

    # 2) Collector uses incremental page-1 sync on routine growth, and full sync on archive deletion
    uid = "900100200"
    db = Database(db_path=tmp_path / "inc_sync.db")
    now_epoch = int(time.time())
    db.update_settings(
        {
            "uid": uid,
            "request_delay_ms": "50",
            # Expired routine sync, but fresh full sync
            f"last_video_sync_ts_{uid}": str(now_epoch - 3600),
            f"last_full_video_sync_ts_{uid}": str(now_epoch - 600),
            f"video_sync_rule_{uid}": VIDEO_SYNC_RULE,
            f"last_archive_count_{uid}": "100",
        }
    )
    db.upsert_videos(
        uid,
        [
            {
                "bvid": f"BV_old_{i:03d}",
                "aid": i + 1,
                "title": f"Old {i}",
                "pubdate": 100 + i,
                "play_count": 50,
                "like_count": 5,
                "coin_count": 1,
                "cids": [{"cid": 7000 + i, "page": 1, "part": "P1"}],
            }
            for i in range(100)
        ],
    )

    calls: list[Optional[int]] = []
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 1000, "following": 5}
    fake_client.get_account_card.return_value = {
        "name": "IncUP",
        "face": "",
        "follower": 1000,
        "archive_count": 101,
    }

    async def fake_get_all_videos(target_uid: str, max_pages: Optional[int] = None):
        calls.append(max_pages)
        if max_pages == 1:
            return [
                {
                    "bvid": "BV_new_100",
                    "aid": 999,
                    "title": "Brand New",
                    "pubdate": 9999,
                    "play_count": 200,
                    "up_mid": uid,
                    "cids": [{"cid": 8999, "page": 1, "part": "P1"}],
                }
            ]
        # Full sync returns only 99 videos (BV_old_000 deleted)
        return [
            {
                "bvid": f"BV_old_{i:03d}",
                "aid": i + 1,
                "title": f"Old {i}",
                "pubdate": 100 + i,
                "play_count": 50,
                "up_mid": uid,
            }
            for i in range(1, 100)
        ]

    fake_client.get_all_videos.side_effect = fake_get_all_videos
    fake_client.get_video_stat.return_value = {
        "play_count": 210,
        "like_count": 20,
        "coin_count": 5,
        "cids": [{"cid": 8999, "page": 1, "part": "P1"}],
    }
    fake_client.get_video_online_total.return_value = (1, "1")

    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        with patch("app.bilibili.collectors.asyncio.sleep", new=AsyncMock()):
            # Routine growth (100 -> 101): incremental 1-page sync, preserves all 100 older cached videos
            res_inc = asyncio.run(svc.run_crawl(force_sync_videos=False))
            assert res_inc["status"] == "success"
            assert calls == [1]
            assert len(db.get_videos(uid)) == 101

            # Archive deletion (101 -> 99): triggers full pagination and purges deleted videos
            fake_client.get_account_card.return_value["archive_count"] = 99
            res_full = asyncio.run(svc.run_crawl(force_sync_videos=False))
            assert res_full["status"] == "success"
            assert calls == [1, None]
            remaining_bvids = {v["bvid"] for v in db.get_videos(uid)}
            assert len(remaining_bvids) == 99
            assert "BV_old_000" not in remaining_bvids


def test_video_history_like_count_snapshots_active_ttl_and_modal_markup(tmp_path: Path):
    import sqlite3
    from datetime import datetime, timedelta, timezone
    from app.bilibili.collectors import STATS_ACTIVE_TTL_SEC, select_stats_refresh_bvids

    # 1) Legacy DB without like_count on video_online_snapshots migrates cleanly with NULL for old rows
    db_path = tmp_path / "like_hist.db"
    with sqlite3.connect(db_path) as raw_conn:
        raw_conn.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO settings VALUES ('online_count_rule', 'minus_1_per_cid_v1', '2026-01-01T00:00:00+08:00');
            CREATE TABLE account_snapshots (
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
            CREATE TABLE videos (
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
                hot_until_ts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE video_online_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER NOT NULL REFERENCES account_snapshots(id) ON DELETE CASCADE,
                bvid TEXT NOT NULL,
                online_count INTEGER NOT NULL,
                online_raw TEXT NOT NULL DEFAULT '',
                play_count INTEGER
            );
            INSERT INTO account_snapshots (id, uid, collected_at, collected_at_ts, status)
            VALUES (1, '777', '2026-01-01T00:00:00+08:00', 1767196800, 'success');
            INSERT INTO video_online_snapshots (snapshot_id, bvid, online_count, online_raw, play_count)
            VALUES (1, 'BV_hist', 10, '11', 500);
            """
        )

    db = Database(db_path=db_path)
    db.update_settings({"uid": "777"})
    # Unsynced video has stats_synced_at=None -> fallback like_count must stay None (no fake 0 baseline)
    db.upsert_videos("777", [{"bvid": "BV_unsynced", "aid": 2, "title": "Unsynced", "play_count": 300}])
    # Synced video with 0 likes has stats_synced_at set -> fallback like_count must record 0
    db.upsert_videos(
        "777",
        [{"bvid": "BV_zero_like", "aid": 3, "title": "Zero Like", "play_count": 50, "like_count": 0}],
    )

    now = datetime.now(timezone.utc).astimezone()
    db.insert_snapshot(
        uid="777",
        follower_count=100,
        following_count=1,
        online_viewers_total=15,
        video_count=3,
        videos_succeeded=3,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=40,
        video_online_items=[
            {"bvid": "BV_hist", "online_count": 12, "online_raw": "13", "play_count": 620, "like_count": 48},
            {"bvid": "BV_unsynced", "online_count": 2, "online_raw": "3"},
            {"bvid": "BV_zero_like", "online_count": 1, "online_raw": "2"},
        ],
        collected_at=now - timedelta(minutes=5),
    )
    db.insert_snapshot(
        uid="777",
        follower_count=102,
        following_count=1,
        online_viewers_total=18,
        video_count=3,
        videos_succeeded=3,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=42,
        video_online_items=[
            {"bvid": "BV_hist", "online_count": 15, "online_raw": "16", "play_count": 780, "like_count": 65},
        ],
        collected_at=now,
    )

    hist_rows = db.get_video_history("BV_hist", limit=10)
    assert len(hist_rows) == 3
    assert hist_rows[0]["play_count"] == 500 and hist_rows[0]["like_count"] is None
    assert hist_rows[1]["play_count"] == 620 and hist_rows[1]["like_count"] == 48
    assert hist_rows[2]["play_count"] == 780 and hist_rows[2]["like_count"] == 65

    assert db.get_video_history("BV_unsynced", limit=10)[0]["like_count"] is None
    assert db.get_video_history("BV_zero_like", limit=10)[0]["like_count"] == 0

    # 2) Active videos refresh stats after STATS_ACTIVE_TTL_SEC (~4 min) while idle showcase waits STATS_HOT_TTL_SEC
    now_ts = now.timestamp()
    five_min_ago_iso = (now - timedelta(seconds=STATS_ACTIVE_TTL_SEC + 30)).isoformat()
    sample_videos = [
        {
            "bvid": "BV_idle_showcase",
            "pubdate": int(now_ts - 30 * 86400),
            "play_count": 90000,
            "last_online_count": 0,
            "hot_until_ts": 0,
            "stats_synced_at": five_min_ago_iso,
        },
        {
            "bvid": "BV_live_active",
            "pubdate": int(now_ts - 10 * 86400),
            "play_count": 5000,
            "last_online_count": 12,
            "hot_until_ts": int(now_ts + 600),
            "stats_synced_at": five_min_ago_iso,
        },
        {
            "bvid": "BV_recent_pub",
            "pubdate": int(now_ts - 3600),
            "play_count": 800,
            "last_online_count": 0,
            "hot_until_ts": 0,
            "stats_synced_at": five_min_ago_iso,
        },
    ]
    refreshed = select_stats_refresh_bvids(
        sample_videos, {"BV_idle_showcase", "BV_live_active", "BV_recent_pub"}, now_ts, quota=10
    )
    assert refreshed == ["BV_live_active", "BV_recent_pub"]

    # 3) API & HTML markup expose like_count and dual interval-delta modal charts
    app = create_app(database=db, enable_scheduler=False)
    with TestClient(app) as client:
        api_hist = client.get("/api/videos/BV_hist/history?hours=24&max_points=2400").json()["history"]
        assert len(api_hist) == 2
        assert [pt["like_count"] for pt in api_hist] == [48, 65]

        html = client.get("/").text
        assert 'id="modalVideoPlayDeltaChart"' in html
        assert 'id="modalVideoLikeDeltaChart"' in html
        assert 'id="modalLikeCount"' in html
        assert 'id="modalLikeDelta"' in html
        assert "每小时播放增量" in html
        assert "每小时点赞增量" in html


def test_adaptive_five_tier_classification_ttls_and_dormant_stats_skip():
    from datetime import datetime, timedelta, timezone
    from app.bilibili.collectors import (
        TIER_ACTIVE,
        TIER_FAST,
        TIER_FROZEN,
        TIER_SLEEP,
        TIER_WARM,
        classify_video_tier,
        is_video_online_due,
        select_stats_refresh_bvids,
    )

    now = datetime.now(timezone.utc).astimezone()
    now_ts = now.timestamp()

    def iso_ago(seconds: float) -> str:
        return (now - timedelta(seconds=seconds)).isoformat()

    # 1) T0 Fast: newly published (<=24h), high online (>=3), surging play rate (>=80/h), or fast_until sticky
    assert classify_video_tier({"bvid": "BV_new", "pubdate": int(now_ts - 3600)}, now_ts) == TIER_FAST
    assert (
        classify_video_tier(
            {"bvid": "BV_high", "pubdate": int(now_ts - 60 * 86400), "last_online_count": 3},
            now_ts,
        )
        == TIER_FAST
    )
    assert (
        classify_video_tier(
            {"bvid": "BV_surge", "pubdate": int(now_ts - 60 * 86400), "play_rate_per_hour": 120.0},
            now_ts,
        )
        == TIER_FAST
    )
    assert (
        classify_video_tier(
            {"bvid": "BV_sticky", "pubdate": int(now_ts - 60 * 86400), "fast_until_ts": int(now_ts + 300)},
            now_ts,
        )
        == TIER_FAST
    )

    # 2) T1 Active: 24-72h pub, 1-2 online, hot_until sticky, or moderate play velocity (25-79/h)
    assert (
        classify_video_tier({"bvid": "BV_2d", "pubdate": int(now_ts - 48 * 3600)}, now_ts)
        == TIER_ACTIVE
    )
    assert (
        classify_video_tier(
            {"bvid": "BV_1on", "pubdate": int(now_ts - 60 * 86400), "last_online_count": 2},
            now_ts,
        )
        == TIER_ACTIVE
    )
    assert (
        classify_video_tier(
            {"bvid": "BV_mod", "pubdate": int(now_ts - 60 * 86400), "play_rate_per_hour": 30.0},
            now_ts,
        )
        == TIER_ACTIVE
    )

    # 3) T2 Warm: 3-14d pub, newest floor, play_rate 5-24/h, or zero_streak < 2
    assert (
        classify_video_tier(
            {"bvid": "BV_7d", "pubdate": int(now_ts - 7 * 86400), "zero_online_streak": 10},
            now_ts,
        )
        == TIER_WARM
    )
    assert (
        classify_video_tier(
            {"bvid": "BV_floor", "pubdate": int(now_ts - 90 * 86400), "zero_online_streak": 10},
            now_ts,
            newest_floor_bvids={"BV_floor"},
        )
        == TIER_WARM
    )

    # 4) T3 Sleep (6h): >14d, zero_streak 2..5 or tiny trickle plays (1..4/h)
    assert (
        classify_video_tier(
            {"bvid": "BV_sleep", "pubdate": int(now_ts - 60 * 86400), "zero_online_streak": 3},
            now_ts,
        )
        == TIER_SLEEP
    )
    assert (
        classify_video_tier(
            {
                "bvid": "BV_trickle",
                "pubdate": int(now_ts - 60 * 86400),
                "zero_online_streak": 12,
                "play_rate_per_hour": 2.0,
            },
            now_ts,
        )
        == TIER_SLEEP
    )

    # 5) T4 Frozen (24h): >14d, zero_streak >= 6, play_rate < 1/h
    assert (
        classify_video_tier(
            {
                "bvid": "BV_frozen",
                "pubdate": int(now_ts - 60 * 86400),
                "zero_online_streak": 6,
                "play_rate_per_hour": 0.2,
            },
            now_ts,
        )
        == TIER_FROZEN
    )

    # 6) TTL due boundaries: T0 ~2m, T1 ~5m, T2 ~30m, T3 ~6h, T4 ~24h
    assert is_video_online_due({"last_checked_at": iso_ago(60)}, TIER_FAST, now_ts) is False
    assert is_video_online_due({"last_checked_at": iso_ago(115)}, TIER_FAST, now_ts) is True
    assert is_video_online_due({"last_checked_at": iso_ago(200)}, TIER_ACTIVE, now_ts) is False
    assert is_video_online_due({"last_checked_at": iso_ago(280)}, TIER_ACTIVE, now_ts) is True
    assert is_video_online_due({"last_checked_at": iso_ago(1200)}, TIER_WARM, now_ts) is False
    assert is_video_online_due({"last_checked_at": iso_ago(1800)}, TIER_WARM, now_ts) is True
    assert is_video_online_due({"last_checked_at": iso_ago(4 * 3600)}, TIER_SLEEP, now_ts) is False
    assert is_video_online_due({"last_checked_at": iso_ago(6 * 3600)}, TIER_SLEEP, now_ts) is True
    assert is_video_online_due({"last_checked_at": iso_ago(18 * 3600)}, TIER_FROZEN, now_ts) is False
    assert is_video_online_due({"last_checked_at": iso_ago(24 * 3600)}, TIER_FROZEN, now_ts) is True

    # 7) T4 frozen videos skip /view when play_count unchanged (until 7d floor), refresh after 12h when changed
    # Include 5 dummy newer videos so test subjects are outside the WARM_NEWEST_FLOOR=5 window
    filler = [
        {
            "bvid": f"BV_fill_{i}",
            "pubdate": int(now_ts - (20 + i) * 86400),
            "play_count": 100,
            "stats_play_count": 100,
            "stats_synced_at": iso_ago(60),
            "zero_online_streak": 10,
        }
        for i in range(5)
    ]
    frozen_unchanged = {
        "bvid": "BV_frz_same",
        "pubdate": int(now_ts - 90 * 86400),
        "play_count": 500,
        "stats_play_count": 500,
        "stats_synced_at": iso_ago(48 * 3600),  # 48h < 7d unchanged floor
        "zero_online_streak": 10,
    }
    frozen_changed = {
        "bvid": "BV_frz_diff",
        "pubdate": int(now_ts - 90 * 86400),
        "play_count": 520,
        "stats_play_count": 500,
        "stats_synced_at": iso_ago(13 * 3600),  # 13h > 12h changed TTL
        "zero_online_streak": 10,
    }
    frozen_7d_floor = {
        "bvid": "BV_frz_7d",
        "pubdate": int(now_ts - 90 * 86400),
        "play_count": 500,
        "stats_play_count": 500,
        "stats_synced_at": iso_ago(8 * 86400),  # 8d > 7d floor
        "zero_online_streak": 10,
    }
    refreshed = select_stats_refresh_bvids(
        [*filler, frozen_unchanged, frozen_changed, frozen_7d_floor],
        set(),
        now_ts,
        quota=10,
    )
    assert "BV_frz_same" not in refreshed
    assert "BV_frz_diff" in refreshed
    assert "BV_frz_7d" in refreshed


def test_play_surge_radar_wakes_frozen_video_404_backoff_and_zero_due_success(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from app.bilibili.collectors import TIER_FAST, TIER_FROZEN, classify_video_tier

    uid = "444555666"
    db = Database(db_path=tmp_path / "radar_backoff.db")
    now_dt = datetime.now(timezone.utc).astimezone()
    now_ts = int(now_dt.timestamp())
    old_pub = now_ts - 90 * 86400
    db.update_settings(
        {
            "uid": uid,
            "request_delay_ms": "50",
            f"last_video_sync_ts_{uid}": str(now_ts),
            f"last_full_video_sync_ts_{uid}": str(now_ts),
            f"video_sync_rule_{uid}": VIDEO_SYNC_RULE,
            f"last_archive_count_{uid}": "7",
        }
    )

    # Seed 7 old videos (5 newest floor + 2 deep-frozen candidates)
    seeded = [
        {
            "bvid": f"BV_old_{i}",
            "aid": 100 + i,
            "title": f"Old {i}",
            "pubdate": old_pub + i * 100,
            "play_count": 1000,
            "like_count": 50,
            "coin_count": 10,
            "cids": [{"cid": 9000 + i, "page": 1, "part": "P1"}],
        }
        for i in range(7)
    ]
    # Insert with timestamp 30 minutes ago
    db.upsert_videos(uid, seeded, now_ts=now_ts - 1800)

    # Mark all 7 videos as recently checked (10 min ago) with 0 online and streak=8
    with db.connection() as conn:
        conn.execute(
            """
            UPDATE videos
            SET last_checked_at = ?, last_online_count = 0, zero_online_streak = 8,
                hot_until_ts = 0, fast_until_ts = 0
            WHERE uid = ?
            """,
            ((now_dt - timedelta(minutes=10)).isoformat(), uid),
        )

    vids_before = {v["bvid"]: v for v in db.get_videos(uid)}
    assert classify_video_tier(vids_before["BV_old_0"], now_ts) == TIER_FROZEN

    # 1) Zero-due sleeping account crawl MUST succeed with 0 online/stats API calls (NOT 'partial'!)
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get_relation_stat.return_value = {"follower": 300, "following": 5}
    fake_client.get_account_card.return_value = {
        "name": "SleepUP",
        "face": "",
        "follower": 300,
        "archive_count": 7,
    }
    fake_client.get_video_online_total.side_effect = AssertionError("No video should be due")
    fake_client.get_video_stat.side_effect = AssertionError("No stats should be due")

    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        res_idle = asyncio.run(svc.run_crawl(force_sync_videos=False))
    assert res_idle["status"] == "success"
    assert res_idle["videos_succeeded"] == 0
    assert res_idle["online_viewers_total"] == 0

    # 2) Play-surge radar via list upsert wakes frozen BV_old_0 (+250 plays in 30 min = 500/h -> T0!)
    #    Also verify stale lower play_count on BV_old_1 is ignored monotonically.
    db.upsert_videos(
        uid,
        [
            {"bvid": "BV_old_0", "aid": 100, "title": "Old 0", "pubdate": old_pub, "play_count": 1250},
            {"bvid": "BV_old_1", "aid": 101, "title": "Old 1", "pubdate": old_pub + 100, "play_count": 900},
        ],
        now_ts=now_ts,
    )
    vids_after = {v["bvid"]: v for v in db.get_videos(uid)}
    assert vids_after["BV_old_0"]["play_count"] == 1250
    assert vids_after["BV_old_0"]["play_rate_per_hour"] >= 400.0
    assert vids_after["BV_old_0"]["zero_online_streak"] == 0
    assert vids_after["BV_old_0"]["fast_until_ts"] > now_ts
    assert classify_video_tier(vids_after["BV_old_0"], now_ts) == TIER_FAST
    assert vids_after["BV_old_1"]["play_count"] == 1000  # monotonic guard

    # 3) /view returning -404 sets stats_backoff_until_ts so subsequent crawl skips it cleanly
    with db.connection() as conn:
        conn.execute(
            "UPDATE videos SET stats_synced_at = NULL WHERE bvid = 'BV_old_0'"
        )
    stat_calls = []

    async def fake_stat_404(bvid: str):
        stat_calls.append(bvid)
        raise BilibiliAPIError("web-interface/view error -404: 啥都木有", code=-404)

    fake_client.get_video_stat.side_effect = fake_stat_404
    fake_client.get_video_online_total.side_effect = None
    fake_client.get_video_online_total.return_value = (1, "1")  # adjusted -> 0

    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        with patch("app.bilibili.collectors.asyncio.sleep", new=AsyncMock()):
            res_err = asyncio.run(svc.run_crawl(force_sync_videos=False))
            assert res_err["status"] == "partial"
            assert stat_calls == ["BV_old_0"]

            v_backed = {v["bvid"]: v for v in db.get_videos(uid)}["BV_old_0"]
            assert v_backed["stats_backoff_until_ts"] > now_ts

            # Next crawl skips BV_old_0's /view call cleanly instead of looping errors
            stat_calls.clear()
            res_clean = asyncio.run(svc.run_crawl(force_sync_videos=False))
            assert res_clean["status"] == "success"
            assert stat_calls == []


def test_fast_pulse_two_minute_channel_history_merge_retention_and_scheduler(tmp_path: Path):
    from datetime import datetime, timedelta, timezone
    from app.scheduler import FAST_PULSE_JOB_ID, JOB_ID, SchedulerManager

    db = Database(db_path=tmp_path / "fast_pulse.db")
    now_dt = datetime.now(timezone.utc).astimezone()
    now_ts = int(now_dt.timestamp())

    db.create_account("1001", enabled=True, set_active=True)
    db.create_account("2002", enabled=True, set_active=False)
    db.update_settings({"interval_minutes": "5", "request_delay_ms": "50"})

    # Account 1001: BV_t0_due (online=12, checked 3m ago -> DUE in T0),
    #               BV_t0_fresh (online=8, checked 30s ago -> NOT due),
    #               BV_t1 (online=1, checked 4m ago -> T1, NOT fast pulse),
    #               BV_t4 (old 0 online streak=8 -> T4)
    db.upsert_videos(
        "1001",
        [
            {
                "bvid": "BV_t0_due",
                "aid": 1,
                "title": "Hot Due",
                "pubdate": now_ts - 3600,
                "play_count": 5000,
                "like_count": 200,
                "coin_count": 50,
                "cids": [{"cid": 11, "page": 1, "part": "P1"}],
            },
            {
                "bvid": "BV_t0_fresh",
                "aid": 2,
                "title": "Hot Fresh",
                "pubdate": now_ts - 3600,
                "play_count": 4000,
                "like_count": 150,
                "coin_count": 30,
                "cids": [{"cid": 12, "page": 1, "part": "P1"}],
            },
            {
                "bvid": "BV_t1",
                "aid": 3,
                "title": "Active T1",
                "pubdate": now_ts - 30 * 86400,
                "play_count": 3000,
                "like_count": 100,
                "coin_count": 20,
                "cids": [{"cid": 13, "page": 1, "part": "P1"}],
            },
            {
                "bvid": "BV_t4",
                "aid": 4,
                "title": "Frozen T4",
                "pubdate": now_ts - 90 * 86400,
                "play_count": 200,
                "like_count": 5,
                "coin_count": 1,
                "cids": [{"cid": 14, "page": 1, "part": "P1"}],
            },
        ],
    )
    # Seed baseline snapshot 5 minutes ago
    db.insert_snapshot(
        uid="1001",
        follower_count=1000,
        following_count=5,
        online_viewers_total=21,
        video_count=4,
        videos_succeeded=4,
        extra_metrics={},
        status="success",
        error_message=None,
        duration_ms=50,
        video_online_items=[
            {"bvid": "BV_t0_due", "online_count": 12, "online_raw": "13", "play_count": 5000, "like_count": 200},
            {"bvid": "BV_t0_fresh", "online_count": 8, "online_raw": "9", "play_count": 4000, "like_count": 150},
            {"bvid": "BV_t1", "online_count": 1, "online_raw": "2", "play_count": 3000, "like_count": 100},
            {"bvid": "BV_t4", "online_count": 0, "online_raw": "1", "play_count": 200, "like_count": 5},
        ],
        collected_at=now_dt - timedelta(minutes=5),
    )
    with db.connection() as conn:
        conn.execute(
            "UPDATE videos SET last_checked_at = ?, stats_synced_at = ? WHERE bvid = 'BV_t0_due'",
            ((now_dt - timedelta(minutes=3)).isoformat(), (now_dt - timedelta(minutes=5)).isoformat()),
        )
        conn.execute(
            "UPDATE videos SET last_checked_at = ? WHERE bvid = 'BV_t0_fresh'",
            ((now_dt - timedelta(seconds=30)).isoformat(),),
        )
        conn.execute(
            "UPDATE videos SET zero_online_streak = 8 WHERE bvid = 'BV_t4'",
        )

    # Account 2002: completely dormant (1 old video, 0 online, streak=8, checked 10m ago)
    db.upsert_videos(
        "2002",
        [
            {
                "bvid": "BV_silent",
                "aid": 20,
                "title": "Silent",
                "pubdate": now_ts - 90 * 86400,
                "play_count": 100,
                "like_count": 2,
                "coin_count": 0,
                "cids": [{"cid": 201, "page": 1, "part": "P1"}],
            }
        ],
    )
    with db.connection() as conn:
        conn.execute(
            "UPDATE videos SET last_checked_at = ?, zero_online_streak = 8 WHERE bvid = 'BV_silent'",
            ((now_dt - timedelta(minutes=10)).isoformat(),),
        )

    polled_bvids: list[str] = []
    stat_bvids: list[str] = []
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None

    async def fake_online(bvid: str, cid: int):
        polled_bvids.append(bvid)
        return (19, "19")  # adjusted -> 18

    async def fake_stat(bvid: str):
        stat_bvids.append(bvid)
        return {"play_count": 5180, "like_count": 215, "coin_count": 55}

    fake_client.get_video_online_total.side_effect = fake_online
    fake_client.get_video_stat.side_effect = fake_stat

    svc = CrawlService(db)
    with patch("app.bilibili.collectors.BilibiliClient", return_value=fake_client):
        with patch("app.bilibili.collectors.asyncio.sleep", new=AsyncMock()):
            pulse_res = asyncio.run(svc.run_fast_pulse())

    assert pulse_res["skipped"] is False
    assert pulse_res["accounts_touched"] == 1  # Silent account 2002 skipped completely!
    assert pulse_res["videos_polled"] == 1
    assert pulse_res["videos_succeeded"] == 1
    assert pulse_res["stats_refreshed"] == 1
    assert polled_bvids == ["BV_t0_due"]
    assert stat_bvids == ["BV_t0_due"]

    # Fast pulse MUST NOT add extra account_snapshots rows, but MUST update videos + merged history
    assert db.count_snapshots("1001") == 1
    updated_v = {v["bvid"]: v for v in db.get_videos("1001")}["BV_t0_due"]
    assert updated_v["last_online_count"] == 18
    assert updated_v["play_count"] == 5180
    assert updated_v["like_count"] == 215

    history = db.get_video_history("BV_t0_due", limit=10)
    assert len(history) == 2
    assert [h["online_count"] for h in history] == [12, 18]
    assert [h["play_count"] for h in history] == [5000, 5180]
    assert [h["like_count"] for h in history] == [200, 215]

    # Retention pruning & account deletion also clean up video_pulse_snapshots
    db.insert_video_pulse(
        "1001",
        [{"bvid": "BV_t0_due", "online_count": 5, "online_raw": "6", "play_count": 4000, "like_count": 100}],
        collected_at=now_dt - timedelta(days=120),
    )
    assert len(db.get_video_history("BV_t0_due", limit=10)) == 3
    db.prune_old_snapshots(retention_days=30)
    assert len(db.get_video_history("BV_t0_due", limit=10)) == 2

    # SchedulerManager registers 2-min fast pulse when interval > 2 and removes it when interval <= 2
    async def check_scheduler():
        mgr = SchedulerManager(db, svc)
        mgr.start()
        try:
            assert mgr.scheduler.get_job(JOB_ID) is not None
            fast_job = mgr.scheduler.get_job(FAST_PULSE_JOB_ID)
            assert fast_job is not None
            assert int(fast_job.trigger.interval.total_seconds()) == 120

            # Lowering interval_minutes to 2 removes redundant fast pulse job
            db.update_settings({"interval_minutes": "2"})
            mgr.reschedule()
            assert mgr.scheduler.get_job(FAST_PULSE_JOB_ID) is None

            # Raising back to 5 restores fast pulse job
            db.update_settings({"interval_minutes": "5"})
            mgr.reschedule()
            assert mgr.scheduler.get_job(FAST_PULSE_JOB_ID) is not None
        finally:
            mgr.shutdown()

    asyncio.run(check_scheduler())

    # Deleting account with delete_data=True purges pulse rows too
    db.delete_account("1001", delete_data=True)
    assert db.get_video_history("BV_t0_due", limit=10) == []


def test_live_event_bus_and_sse_stream(tmp_path: Path):
    import json
    from app.events import LiveEventBus

    # 1. Unit test LiveEventBus pub/sub, bounded overflow, and clean shutdown
    async def verify_bus():
        bus = LiveEventBus(max_queue_size=2)
        assert bus.revision == 1
        async with bus.subscribe() as q:
            assert bus.subscriber_count == 1
            bus.publish("crawl_started", uid="123")
            bus.publish("snapshot", uid="123", snapshot_id=1)
            bus.publish("fast_pulse", uid="123", videos_succeeded=2)
            # Queue size is 2, so oldest event (crawl_started) was dropped to keep newest
            first = q.get_nowait()
            second = q.get_nowait()
            assert first is not None and first["kind"] == "snapshot"
            assert second is not None and second["kind"] == "fast_pulse"
            assert bus.revision == 4
            bus.close_all()
            assert q.get_nowait() is None
        assert bus.subscriber_count == 0

    asyncio.run(verify_bus())

    # 2. Verify /api/dashboard no-store + X-Data-Revision and /api/events SSE stream
    db = Database(db_path=tmp_path / "sse_test.db")
    application = create_app(database=db, enable_scheduler=False)
    with TestClient(application) as client:
        resp1 = client.get("/api/dashboard")
        assert resp1.status_code == 200
        assert resp1.headers.get("cache-control") == "no-store"
        rev1 = int(resp1.headers["x-data-revision"])
        assert rev1 >= 1

        # Mutating accounts publishes an event and bumps revision
        create_resp = client.post("/api/accounts", json={"uid": "998877", "enabled": True})
        assert create_resp.status_code == 200
        resp2 = client.get("/api/dashboard")
        rev2 = int(resp2.headers["x-data-revision"])
        assert rev2 > rev1

        # Test SSE endpoint directly via ASGI app generator (publish + close_all for instant termination)
        async def consume_sse():
            bus = application.state.event_bus
            received_chunks: list[str] = []

            async def send_events_later():
                while bus.subscriber_count == 0:
                    await asyncio.sleep(0.01)
                bus.publish("snapshot", uid="998877", snapshot_id=42, status="success")
                await asyncio.sleep(0.01)
                bus.close_all()

            from app.routers.dashboard import stream_events

            class _FakeRequest:
                def __init__(self, app_obj):
                    self.app = app_obj

                async def is_disconnected(self) -> bool:
                    return False

            streaming = await stream_events(_FakeRequest(application))
            assert streaming.media_type == "text/event-stream"
            assert streaming.headers.get("x-accel-buffering") == "no"
            assert "no-cache" in (streaming.headers.get("cache-control") or "")

            driver = asyncio.create_task(send_events_later())
            async for chunk in streaming.body_iterator:
                received_chunks.append(chunk if isinstance(chunk, str) else chunk.decode("utf-8"))
            await driver
            return "".join(received_chunks)

        raw_sse = asyncio.run(consume_sse())
        assert "event: ready\n" in raw_sse
        assert "event: update\n" in raw_sse
        update_lines = [
            line[len("data: "):]
            for line in raw_sse.splitlines()
            if line.startswith("data: ")
        ]
        assert len(update_lines) >= 2
        ready_obj = json.loads(update_lines[0])
        update_obj = json.loads(update_lines[1])
        assert ready_obj["rev"] == rev2
        assert update_obj["kind"] == "snapshot"
        assert update_obj["uid"] == "998877"
        assert update_obj["snapshot_id"] == 42


def test_crawl_service_emits_live_events(tmp_path: Path):
    from app.events import LiveEventBus

    db = Database(db_path=tmp_path / "crawl_events.db")
    db.create_account(uid="445566", enabled=True, set_active=True)
    bus = LiveEventBus()

    class _FakeCollector:
        name = "fake"

        async def collect(self, ctx: CrawlContext) -> None:
            ctx.follower_count = 1234
            ctx.following_count = 12
            ctx.online_viewers_total = 56
            ctx.video_count = 1
            ctx.videos_succeeded = 1

    svc = CrawlService(db, collectors=[_FakeCollector()], event_bus=bus)

    async def run_and_capture():
        captured: list[dict] = []
        async with bus.subscribe() as q:
            res = await svc.run_crawl(uid="445566")
            assert res["skipped"] is False
            while not q.empty():
                item = q.get_nowait()
                if item is not None:
                    captured.append(item)
        return captured

    events = asyncio.run(run_and_capture())
    kinds = [ev["kind"] for ev in events]
    assert kinds == ["crawl_started", "snapshot"]
    assert events[1]["uid"] == "445566"
    assert events[1]["status"] == "success"
    assert svc.crawling_uid is None


def test_video_play_count_7d_tracking_settlement_and_locking(tmp_path: Path):
    db = Database(db_path=tmp_path / "play7d.db")
    uid = "777001"
    now_ts = int(time.time())
    seven_days = 7 * 86400

    # 1. Video published 3 days ago: stays in 'tracking' and follows play_count updates
    pub_recent = now_ts - 3 * 86400
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1recent7d",
                "aid": 1,
                "title": "Recent Video",
                "pubdate": pub_recent,
                "play_count": 12000,
            }
        ],
        now_ts=now_ts,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=now_ts)}
    assert vmap["BV1recent7d"]["play_count_7d"] == 12000
    assert vmap["BV1recent7d"]["play_count_7d_status"] == "tracking"
    assert vmap["BV1recent7d"]["play_count_7d_locked"] is False

    # Updating stats while still < 7d updates live tracking play_count_7d
    db.update_video_stats_batch(
        [{"bvid": "BV1recent7d", "play_count": 18500, "like_count": 1000, "coin_count": 120}],
        now_ts=now_ts + 3600,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=now_ts + 3600)}
    assert vmap["BV1recent7d"]["play_count_7d"] == 18500
    assert vmap["BV1recent7d"]["play_count_7d_status"] == "tracking"

    # 2. Video crossing pubdate + 7d with bracketed pre & post samples: linearly interpolated & locked
    pub_cross = now_ts - seven_days - 3600
    target_cross = pub_cross + seven_days
    # Pre-7d observation 10 minutes before target_cross
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1cross7d",
                "aid": 2,
                "title": "Crossing Video",
                "pubdate": pub_cross,
                "play_count": 50000,
            }
        ],
        now_ts=target_cross - 600,
    )
    # Post-7d observation 20 minutes after target_cross -> 1/3 between 50,000 and 50,300 = 50,100
    db.update_video_stats_batch(
        [{"bvid": "BV1cross7d", "play_count": 50300, "like_count": 2000, "coin_count": 300}],
        now_ts=target_cross + 1200,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=target_cross + 1200)}
    assert vmap["BV1cross7d"]["play_count_7d"] == 50100
    assert vmap["BV1cross7d"]["play_count_7d_status"] == "locked"
    assert vmap["BV1cross7d"]["play_count_7d_locked"] is True

    # Subsequent huge play growth must NOT mutate locked 7-day play count
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1cross7d",
                "aid": 2,
                "title": "Crossing Video",
                "pubdate": pub_cross,
                "play_count": 999999,
            }
        ],
        now_ts=target_cross + 10 * 86400,
    )
    db.update_video_stats_batch(
        [{"bvid": "BV1cross7d", "play_count": 1200000, "like_count": 50000, "coin_count": 8000}],
        now_ts=target_cross + 11 * 86400,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=target_cross + 11 * 86400)}
    assert vmap["BV1cross7d"]["play_count"] == 1200000
    assert vmap["BV1cross7d"]["play_count_7d"] == 50100
    assert vmap["BV1cross7d"]["play_count_7d_status"] == "locked"

    # 3. Video first observed within 6h after 7d mark -> settles to post sample
    pub_just_past = now_ts - seven_days - 2 * 3600
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1post7d",
                "aid": 3,
                "title": "Just Past 7d",
                "pubdate": pub_just_past,
                "play_count": 42000,
            }
        ],
        now_ts=now_ts,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=now_ts)}
    assert vmap["BV1post7d"]["play_count_7d"] == 42000
    assert vmap["BV1post7d"]["play_count_7d_status"] == "locked"

    # 4. Old video (> 7d + 6h) first observed now -> locked as unavailable
    pub_old = now_ts - 30 * 86400
    db.upsert_videos(
        uid,
        [
            {
                "bvid": "BV1old7d",
                "aid": 4,
                "title": "Old Video",
                "pubdate": pub_old,
                "play_count": 300000,
            }
        ],
        now_ts=now_ts,
    )
    vmap = {v["bvid"]: v for v in db.get_videos(uid, now_ts=now_ts)}
    assert vmap["BV1old7d"]["play_count_7d"] is None
    assert vmap["BV1old7d"]["play_count_7d_status"] == "unavailable"
    assert vmap["BV1old7d"]["play_count_7d_locked"] is True


def test_video_play_count_7d_backfill_migration_and_api(tmp_path: Path):
    import sqlite3
    from datetime import datetime, timezone

    db_path = tmp_path / "legacy_play7d.db"
    db = Database(db_path=db_path)
    uid = "888002"
    db.create_account(uid=uid, enabled=True, set_active=True)

    now_ts = int(time.time())
    seven_days = 7 * 86400
    pub_bracket = now_ts - 10 * 86400
    target_bracket = pub_bracket + seven_days
    pub_tracking = now_ts - 2 * 86400
    pub_ancient = now_ts - 60 * 86400

    # Insert videos & historical snapshots directly, then clear backfill flag and reset columns
    with db.connection() as conn:
        iso_now = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()
        conn.executemany(
            """
            INSERT INTO videos (
                bvid, uid, aid, title, pubdate, play_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("BV1backfill_ok", uid, 11, "Backfill Bracket", pub_bracket, 95000, iso_now),
                ("BV1backfill_track", uid, 12, "Backfill Tracking", pub_tracking, 14000, iso_now),
                ("BV1backfill_old", uid, 13, "Backfill Ancient", pub_ancient, 500000, iso_now),
            ],
        )
        # Pre-7d sample in account_snapshots/video_online_snapshots (15 min before 7d)
        pre_ts = target_bracket - 900
        cur = conn.execute(
            """
            INSERT INTO account_snapshots (
                uid, collected_at, collected_at_ts, follower_count, online_viewers_total, status
            ) VALUES (?, ?, ?, 1000, 10, 'success')
            """,
            (uid, datetime.fromtimestamp(pre_ts, tz=timezone.utc).isoformat(), pre_ts),
        )
        snap_pre_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO video_online_snapshots (
                snapshot_id, bvid, online_count, online_raw, play_count, like_count
            ) VALUES (?, ?, 5, '6', ?, 400)
            """,
            (snap_pre_id, "BV1backfill_ok", 60000),
        )
        # Post-7d sample in video_pulse_snapshots (45 min after 7d) -> 1/4 of 4000 = +1000 -> 61000
        post_ts = target_bracket + 2700
        conn.execute(
            """
            INSERT INTO video_pulse_snapshots (
                uid, bvid, collected_at, collected_at_ts, online_count, online_raw, play_count, like_count
            ) VALUES (?, ?, ?, ?, 4, '5', ?, 450)
            """,
            (
                uid,
                "BV1backfill_ok",
                datetime.fromtimestamp(post_ts, tz=timezone.utc).isoformat(),
                post_ts,
                64000,
            ),
        )
        conn.execute("DELETE FROM settings WHERE key = 'play_count_7d_backfilled_v1'")

    # Re-open Database to trigger one-time backfill migration
    db2 = Database(db_path=db_path)
    vmap = {v["bvid"]: v for v in db2.get_videos(uid)}
    assert vmap["BV1backfill_ok"]["play_count_7d"] == 61000
    assert vmap["BV1backfill_ok"]["play_count_7d_status"] == "locked"
    assert vmap["BV1backfill_track"]["play_count_7d"] == 14000
    assert vmap["BV1backfill_track"]["play_count_7d_status"] == "tracking"
    assert vmap["BV1backfill_old"]["play_count_7d"] is None
    assert vmap["BV1backfill_old"]["play_count_7d_status"] == "unavailable"

    # Locked 7-day play count survives snapshot pruning
    db2.prune_old_snapshots(retention_days=1)
    vmap_after_prune = {v["bvid"]: v for v in db2.get_videos(uid)}
    assert vmap_after_prune["BV1backfill_ok"]["play_count_7d"] == 61000
    assert vmap_after_prune["BV1backfill_ok"]["play_count_7d_status"] == "locked"

    # Verify /api/dashboard and /api/videos expose play_count_7d & play_count_7d_status
    application = create_app(database=db2, enable_scheduler=False)
    with TestClient(application) as client:
        dash_resp = client.get(f"/api/dashboard?uid={uid}")
        assert dash_resp.status_code == 200
        dash_videos = {v["bvid"]: v for v in dash_resp.json()["top_videos"]}
        assert dash_videos["BV1backfill_ok"]["play_count_7d"] == 61000
        assert dash_videos["BV1backfill_ok"]["play_count_7d_status"] == "locked"
        assert dash_videos["BV1backfill_track"]["play_count_7d"] == 14000
        assert dash_videos["BV1backfill_track"]["play_count_7d_status"] == "tracking"
        assert dash_videos["BV1backfill_old"]["play_count_7d"] is None
        assert dash_videos["BV1backfill_old"]["play_count_7d_status"] == "unavailable"

        vid_resp = client.get(f"/api/videos?uid={uid}")
        assert vid_resp.status_code == 200
        api_videos = {v["bvid"]: v for v in vid_resp.json()["videos"]}
        assert api_videos["BV1backfill_ok"]["play_count_7d"] == 61000


def test_db_connection_pragmas_and_httpx_log_level(tmp_path: Path):
    import logging

    import app.main  # noqa: F401

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

    db = Database(db_path=tmp_path / "pragmas.db")
    with db.connection() as conn:
        sync_mode = conn.execute("PRAGMA synchronous;").fetchone()[0]
        temp_store = conn.execute("PRAGMA temp_store;").fetchone()[0]
    assert sync_mode == 1  # NORMAL
    assert temp_store == 2  # MEMORY


def test_batch_crawl_skips_deleted_and_mid_crawl_deleted_accounts(tmp_path: Path):
    db = Database(db_path=tmp_path / "delete_race.db")
    db.create_account("10001", account_name="KeepMe", enabled=True)
    db.create_account("20002", account_name="DeleteMidCrawl", enabled=True)
    db.create_account("30003", account_name="DeleteBeforeTurn", enabled=True)

    crawled_uids: list[str] = []

    class FakeClient:
        def __init__(self, cookie: str = "") -> None:
            self.cookie = cookie

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get_relation_stat(self, uid: str):
            crawled_uids.append(uid)
            if uid == "10001":
                # Delete 30003 before run_crawl_all even reaches it
                db.delete_account("30003", delete_data=True)
            return {"follower": 100, "following": 5}

        async def get_account_card(self, uid: str):
            return {"name": f"UP_{uid}", "face": "", "archive_count": 1}

        async def get_all_videos(self, uid: str, max_pages=None):
            if uid == "20002":
                # Delete 20002 mid-crawl right before videos/snapshot would be persisted
                db.delete_account("20002", delete_data=True)
            return [
                {
                    "bvid": f"BV1race{uid}",
                    "aid": int(uid),
                    "title": f"Video {uid}",
                    "pic": "",
                    "pubdate": int(time.time()) - 3600,
                    "duration": 120,
                    "play_count": 500,
                    "cids": [{"cid": int(uid) + 10, "page": 1, "part": "P1"}],
                    "up_mid": int(uid),
                }
            ]

        async def get_video_stat(self, bvid: str):
            return {"play_count": 500, "like_count": 10, "coin_count": 2}

        async def get_video_online_total(self, bvid: str, cid: int):
            return 3, "3"

    service = CrawlService(db)
    with (
        patch("app.bilibili.collectors.BilibiliClient", FakeClient),
        patch("app.bilibili.collectors.asyncio.sleep", new=AsyncMock()),
    ):
        res = asyncio.run(service.run_crawl_all(force_sync_videos=True))

    assert res["skipped"] is False
    # 30003 was never even contacted; 20002 was aborted before saving anything
    assert crawled_uids == ["10001", "20002"]
    assert res["total"] == 1
    assert db.count_snapshots("10001") == 1
    assert db.count_snapshots("20002") == 0
    assert db.count_snapshots("30003") == 0
    assert db.get_videos("20002") == []
    assert db.get_videos("30003") == []
    assert db.get_setting("last_video_sync_ts_20002", "") == ""
    assert db.get_setting("last_video_sync_ts_30003", "") == ""


def test_chart_snapshots_endpoint_lightweight_windowing_and_downsampling(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    db = Database(db_path=tmp_path / "chart_snaps.db")
    app = create_app(database=db, enable_scheduler=False)

    with TestClient(app) as client:
        # Empty state when no account is configured
        empty_resp = client.get("/api/snapshots?hours=24")
        assert empty_resp.status_code == 200
        assert empty_resp.headers.get("cache-control") == "no-store"
        assert "x-data-revision" in empty_resp.headers
        assert empty_resp.json() == {
            "uid": "",
            "hours": 24,
            "snapshots": [],
            "downsampled": False,
            "total_in_window": 0,
        }

        db.create_account("50001", account_name="UP_A", set_active=True)
        db.create_account("50002", account_name="UP_B", set_active=False)

        now = datetime.now(timezone.utc).astimezone()
        # Insert 80 snapshots over the last 40 hours (30-min spacing), with distinct peaks/troughs
        for i in range(80):
            ts = now - timedelta(minutes=(79 - i) * 30)
            followers = 1000 + i * 5
            online = 20 + (i % 7)
            if i == 15:
                followers = 900  # global min followers
            elif i == 30:
                followers = 5000  # global max followers
            if i == 22:
                online = 0  # global min online
            elif i == 40:
                online = 999  # global max online
            db.insert_snapshot(
                uid="50001",
                follower_count=followers,
                following_count=10,
                online_viewers_total=online,
                video_count=5,
                videos_succeeded=5,
                extra_metrics={"blob": "x" * 200},
                status="success",
                error_message=None,
                duration_ms=40,
                video_online_items=[],
                collected_at=ts,
            )

        db.insert_snapshot(
            uid="50002",
            follower_count=200,
            following_count=1,
            online_viewers_total=3,
            video_count=1,
            videos_succeeded=1,
            extra_metrics={},
            status="success",
            error_message=None,
            duration_ms=10,
            video_online_items=[],
            collected_at=now,
        )

        # 1) 1-hour window returns only recent points, lightweight fields only, not downsampled
        r_1h = client.get("/api/snapshots?hours=1&uid=50001")
        assert r_1h.status_code == 200
        body_1h = r_1h.json()
        assert body_1h["uid"] == "50001"
        assert body_1h["hours"] == 1
        assert body_1h["downsampled"] is False
        assert 1 <= len(body_1h["snapshots"]) <= 4
        assert body_1h["total_in_window"] == len(body_1h["snapshots"])
        assert set(body_1h["snapshots"][0].keys()) == {
            "collected_at",
            "collected_at_ts",
            "follower_count",
            "online_viewers_total",
        }

        # 2) All-time window with max_points=32 downsamples while preserving endpoints & extrema
        r_all = client.get("/api/snapshots?hours=0&uid=50001&max_points=32")
        assert r_all.status_code == 200
        body_all = r_all.json()
        assert body_all["downsampled"] is True
        assert body_all["total_in_window"] == 80
        pts = body_all["snapshots"]
        assert len(pts) <= 36  # max_points plus small extrema budget
        ts_list = [p["collected_at_ts"] for p in pts]
        assert ts_list == sorted(ts_list)
        followers_vals = [p["follower_count"] for p in pts]
        online_vals = [p["online_viewers_total"] for p in pts]
        assert 900 in followers_vals
        assert 5000 in followers_vals
        assert 0 in online_vals
        assert 999 in online_vals

        # 3) Explicit uid isolates account data
        r_b = client.get("/api/snapshots?hours=24&uid=50002")
        assert r_b.status_code == 200
        assert len(r_b.json()["snapshots"]) == 1
        assert r_b.json()["snapshots"][0]["follower_count"] == 200

        # 4) Verify composite index exists and get_accounts_summary matches exact latest/delta
        with db.connection() as conn:
            indexes = {
                row["name"]
                for row in conn.execute("PRAGMA index_list(account_snapshots)").fetchall()
            }
        assert "idx_snapshots_uid_ts_id" in indexes

        summaries = {a["uid"]: a for a in db.get_accounts_summary()}
        assert summaries["50001"]["latest_follower_count"] == 1000 + 79 * 5
        assert summaries["50001"]["follower_delta"] == 5
        assert summaries["50002"]["latest_follower_count"] == 200
        assert summaries["50002"]["follower_delta"] is None


def test_account_switch_fast_dashboard_and_non_invalidating_activate(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    db = Database(db_path=tmp_path / "fast_switch.db")
    app = create_app(database=db, enable_scheduler=False)

    db.create_account("60001", account_name="UP_One", set_active=True)
    db.create_account("60002", account_name="UP_Two", set_active=False)

    # Seed 25 videos where top-5 by online and top-5 by play_count are completely disjoint
    videos_seed = []
    for i in range(25):
        videos_seed.append(
            {
                "bvid": f"BV1fast{i:02d}",
                "aid": 1000 + i,
                "title": f"Video {i}",
                "pubdate": int(time.time()) - (i + 1) * 3600,
                "duration": 120,
                "play_count": 100000 - i * 1000 if i < 5 else 500 + i,
                "cids": [{"cid": i * 10 + 1, "page": 1, "part": "P1"}, {"cid": i * 10 + 2, "page": 2, "part": "P2"}]
                if i % 2 == 0
                else [{"cid": i * 10 + 1, "page": 1, "part": "P1"}],
            }
        )
    db.upsert_videos("60002", videos_seed)
    # Give videos 20..24 the highest online counts so top-5 online != top-5 plays
    now = datetime.now(timezone.utc).astimezone()
    for snap_idx in range(60):
        ts = now - timedelta(minutes=(59 - snap_idx) * 20)
        db.insert_snapshot(
            uid="60002",
            follower_count=2000 + snap_idx * 3,
            following_count=10,
            online_viewers_total=50 + (snap_idx % 10),
            video_count=25,
            videos_succeeded=5,
            extra_metrics={"large_blob": "y" * 200},
            status="success",
            error_message=None,
            duration_ms=30,
            video_online_items=(
                [
                    {"bvid": f"BV1fast{20 + k:02d}", "online_count": 100 + k * 10, "online_raw": str(100 + k * 10)}
                    for k in range(5)
                ]
                if snap_idx == 59
                else []
            ),
            collected_at=ts,
        )

    # Verify new composite video leaderboard indexes exist
    with db.read_connection() as conn:
        vid_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(videos)").fetchall()
        }
    assert "idx_videos_uid_online_play" in vid_indexes
    assert "idx_videos_uid_play_online" in vid_indexes

    with TestClient(app) as client:
        # 1) Lightweight dashboard for account switching: single read connection, capped top_videos, downsampled 4-field snapshots, no accounts scan
        with (
            patch.object(db, "_open_read_connection", wraps=db._open_read_connection) as spy_open,
            patch.object(db, "get_accounts_summary", wraps=db.get_accounts_summary) as spy_accs,
        ):
            resp = client.get(
                "/api/dashboard?hours=0&uid=60002&max_points=24&video_limit=5&include_accounts=false"
            )
            assert resp.status_code == 200
            assert spy_open.call_count == 1
            assert spy_accs.call_count == 0

        body = resp.json()
        assert body["settings"]["uid"] == "60002"
        assert body["accounts"] == []
        assert body["total_videos"] == 25
        assert body["snapshots_downsampled"] is True
        assert len(body["snapshots"]) <= 30
        assert set(body["snapshots"][0].keys()) == {
            "collected_at",
            "collected_at_ts",
            "follower_count",
            "online_viewers_total",
        }
        returned_bvids = {v["bvid"] for v in body["top_videos"]}
        assert len(returned_bvids) == 10
        assert {f"BV1fast{i:02d}" for i in range(5)}.issubset(returned_bvids)
        assert {f"BV1fast{i:02d}" for i in range(20, 25)}.issubset(returned_bvids)
        # cid_count computed accurately via SQL json_array_length
        by_bvid = {v["bvid"]: v for v in body["top_videos"]}
        assert by_bvid["BV1fast00"]["cid_count"] == 2
        assert by_bvid["BV1fast01"]["cid_count"] == 1

        # 2) Lightweight /api/videos expansion returns all 25 videos without cids arrays
        lw_vids = client.get("/api/videos?uid=60002&lightweight=true")
        assert lw_vids.status_code == 200
        lw_list = lw_vids.json()["videos"]
        assert len(lw_list) == 25
        assert "cids" not in lw_list[0]

        # 3) Background activate (?include_accounts=false) skips summary query and does NOT bump revision
        rev_before = int(resp.headers["x-data-revision"])
        with patch.object(db, "get_accounts_summary", wraps=db.get_accounts_summary) as spy_accs2:
            act_lw = client.post("/api/accounts/60002/activate?include_accounts=false")
            assert act_lw.status_code == 200
            assert act_lw.json()["active_uid"] == "60002"
            assert act_lw.json()["accounts"] == []
            assert spy_accs2.call_count == 0

        # Default activate computes get_accounts_summary at most ONCE (no double query) and still keeps revision stable
        with patch.object(db, "get_accounts_summary", wraps=db.get_accounts_summary) as spy_accs3:
            act_full = client.post("/api/accounts/60001/activate")
            assert act_full.status_code == 200
            assert act_full.json()["active_uid"] == "60001"
            assert len(act_full.json()["accounts"]) == 2
            assert spy_accs3.call_count == 1

        after_resp = client.get("/api/dashboard?hours=24&uid=60001&max_points=24&video_limit=5")
        assert after_resp.status_code == 200
        assert int(after_resp.headers["x-data-revision"]) == rev_before





