import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app.auth import (
    clear_admin_session_cookie,
    get_admin_auth_info,
    get_admin_status_payload,
    hash_password,
    require_admin,
)
from app.bilibili.client import BilibiliAPIError, BilibiliClient
from app.models import AdminAuthStatus, SettingsOut, SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])


class TestConnectionRequest(BaseModel):
    uid: str
    cookie: Optional[str] = None


@router.get("/settings", response_model=SettingsOut, dependencies=[Depends(require_admin)])
async def get_settings(
    request: Request,
    uid: Optional[str] = Query(default=None, description="Optional account UID"),
) -> SettingsOut:
    db = request.app.state.db

    def _load() -> dict[str, Any]:
        pub = db.get_public_settings(uid=uid)
        pub["admin_auth"] = AdminAuthStatus(**get_admin_status_payload(request))
        return pub

    pub = await asyncio.to_thread(_load)
    return SettingsOut(**pub)


@router.put("/settings", response_model=SettingsOut, dependencies=[Depends(require_admin)])
async def update_settings(
    request: Request, response: Response, body: SettingsUpdate
) -> SettingsOut:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service

    old_uid = (await asyncio.to_thread(db.get_setting, "uid", "")).strip()
    updates: dict[str, Any] = {}
    password_changed = False

    if body.uid is not None:
        clean_uid = body.uid.strip()
        if clean_uid and not clean_uid.isdigit():
            raise HTTPException(status_code=400, detail="UID 必须是纯数字")
        updates["uid"] = clean_uid
        if clean_uid != old_uid:
            updates["account_name"] = ""
            updates["account_face"] = ""

    if body.cookie is not None:
        updates["cookie"] = body.cookie.strip()

    if body.interval_minutes is not None:
        updates["interval_minutes"] = str(body.interval_minutes)

    if body.enabled is not None:
        updates["enabled"] = "true" if body.enabled else "false"

    if body.request_delay_ms is not None:
        updates["request_delay_ms"] = str(body.request_delay_ms)

    if body.video_sync_interval_minutes is not None:
        updates["video_sync_interval_minutes"] = str(body.video_sync_interval_minutes)

    if body.snapshot_retention_days is not None:
        updates["snapshot_retention_days"] = str(body.snapshot_retention_days)

    if body.admin_password is not None:
        auth_info = await asyncio.to_thread(get_admin_auth_info, db)
        if auth_info["source"] == "env":
            raise HTTPException(
                status_code=400,
                detail="当前管理员密码由环境变量 BILI_ADMIN_PASSWORD 控制，无法在网页端修改或清除",
            )
        raw_pw = body.admin_password.rstrip("\r\n")
        if raw_pw == "":
            updates["admin_password_hash"] = ""
            password_changed = True
        else:
            if not raw_pw.strip():
                raise HTTPException(status_code=400, detail="管理员密码不能为空白字符")
            updates["admin_password_hash"] = await asyncio.to_thread(hash_password, raw_pw)
            password_changed = True

    pub = await asyncio.to_thread(db.update_settings, updates)
    scheduler_mgr.reschedule()

    if (
        body.snapshot_retention_days is not None
        and body.snapshot_retention_days > 0
        and getattr(request.app.state, "enable_scheduler", True)
        and hasattr(db, "prune_old_snapshots")
    ):
        scheduler_mgr.spawn_background_task(
            asyncio.to_thread(db.prune_old_snapshots, body.snapshot_retention_days),
            name="settings_prune_snapshots",
        )

    if password_changed:
        clear_admin_session_cookie(response)
        status_payload = await asyncio.to_thread(
            lambda: get_admin_status_payload(request, force_unauthenticated=True)
        )
        pub["admin_auth"] = AdminAuthStatus(**status_payload)
    else:
        status_payload = await asyncio.to_thread(get_admin_status_payload, request)
        pub["admin_auth"] = AdminAuthStatus(**status_payload)

    new_uid = pub["uid"]
    if (
        new_uid
        and new_uid != old_uid
        and pub["enabled"]
        and getattr(request.app.state, "enable_scheduler", True)
    ):
        scheduler_mgr.spawn_background_task(
            crawl_service.run_crawl(uid=new_uid, force_sync_videos=True, wait=True),
            name=f"settings_uid_crawl_{new_uid}",
        )

    return SettingsOut(**pub)


@router.post("/test-connection", dependencies=[Depends(require_admin)])
async def test_connection(request: Request, body: TestConnectionRequest) -> dict[str, Any]:
    db = request.app.state.db
    uid = body.uid.strip()
    if not uid or not uid.isdigit():
        raise HTTPException(status_code=400, detail="请输入有效的纯数字 B站 UID")

    if body.cookie is not None:
        cookie = body.cookie.strip()
    else:
        cookie = await asyncio.to_thread(db.get_effective_cookie, uid)

    try:
        async with BilibiliClient(cookie=cookie) as client:
            stat = await client.get_relation_stat(uid)
            card = await client.get_account_card(uid)
            videos, raw_total = await client.get_videos_page_series(uid, pn=1, ps=100)
            owned_videos = [
                v
                for v in videos
                if not (v.get("up_mid") and str(v["up_mid"]).strip() != uid)
            ]
            video_total = len(owned_videos) if raw_total <= 100 else raw_total
            return {
                "ok": True,
                "uid": uid,
                "name": card.get("name", ""),
                "face": card.get("face", ""),
                "follower": stat["follower"],
                "following": stat["following"],
                "video_total": video_total,
                "sample_videos": [
                    {"bvid": v["bvid"], "title": v["title"]} for v in owned_videos[:3]
                ],
            }
    except BilibiliAPIError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"连接失败: {exc}"}


@router.post("/crawl/now", dependencies=[Depends(require_admin)])
async def trigger_crawl_now(
    request: Request,
    force_sync_videos: bool = Query(default=False, description="Force full video list sync"),
    uid: Optional[str] = Query(default=None, description="Optional account UID to crawl"),
    background: bool = Query(default=False, description="Run crawl asynchronously in background"),
) -> dict[str, Any]:
    db = request.app.state.db
    crawl_service = request.app.state.crawl_service
    scheduler_mgr = request.app.state.scheduler

    target_uid = uid.strip() if uid is not None else None
    if target_uid is not None and target_uid and not target_uid.isdigit():
        raise HTTPException(status_code=400, detail="UID 必须是纯数字")

    if background:
        resolved_uid = (
            target_uid
            if target_uid is not None
            else (await asyncio.to_thread(db.get_setting, "uid", "")).strip()
        )
        if not resolved_uid or not resolved_uid.isdigit():
            raise HTTPException(status_code=400, detail="请先配置有效的纯数字 B站 UID")
        if crawl_service.is_crawling:
            raise HTTPException(status_code=409, detail="已有采集任务正在进行中")
        scheduler_mgr.spawn_background_task(
            crawl_service.run_crawl(force_sync_videos=force_sync_videos, uid=resolved_uid),
            name=f"manual_crawl_{resolved_uid}",
        )
        return {
            "accepted": True,
            "background": True,
            "uid": resolved_uid,
        }

    result = await crawl_service.run_crawl(force_sync_videos=force_sync_videos, uid=target_uid)
    if result.get("skipped"):
        raise HTTPException(status_code=409, detail=result.get("reason", "无法执行采集"))
    return result
