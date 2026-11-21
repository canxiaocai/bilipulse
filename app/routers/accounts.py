import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.auth import require_admin
from app.models import (
    AccountCreate,
    AccountReorderRequest,
    AccountSummary,
    AccountsListOut,
    AccountUpdate,
)

router = APIRouter(prefix="/api", tags=["accounts"])


def _validate_uid(raw_uid: str) -> str:
    clean_uid = raw_uid.strip()
    if not clean_uid or not clean_uid.isdigit():
        raise HTTPException(status_code=400, detail="UID 必须是纯数字")
    return clean_uid


def _emit_accounts_event(request: Request, kind: str, uid: Optional[str] = None) -> None:
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is not None:
        event_bus.publish(kind, uid=uid)


@router.get("/accounts", response_model=AccountsListOut)
async def list_accounts(request: Request, response: Response) -> AccountsListOut:
    response.headers["Cache-Control"] = "no-store"
    db = request.app.state.db
    crawl_service = request.app.state.crawl_service
    crawling_uid = crawl_service.crawling_uid

    def _load() -> tuple[str, list[dict[str, Any]]]:
        with db.read_session():
            active_uid = db.get_setting("uid", "").strip()
            accounts = db.get_accounts_summary(crawling_uid=crawling_uid)
            return active_uid, accounts

    active_uid, accounts = await asyncio.to_thread(_load)
    return AccountsListOut(
        active_uid=active_uid,
        accounts=[AccountSummary(**a) for a in accounts],
    )


def _schedule_account_bootstrap(
    request: Request,
    uid: str,
    *,
    cookie: Optional[str] = None,
    run_crawl: bool = False,
) -> None:
    if not getattr(request.app.state, "enable_scheduler", True):
        return
    crawl_service = request.app.state.crawl_service
    scheduler_mgr = request.app.state.scheduler
    effective_cookie = cookie.strip() if cookie and cookie.strip() else None

    async def _bootstrap() -> None:
        await crawl_service.fetch_account_profile(uid, cookie=effective_cookie)
        if run_crawl:
            await crawl_service.run_crawl(uid=uid, force_sync_videos=True, wait=True)

    scheduler_mgr.spawn_background_task(_bootstrap(), name=f"account_bootstrap_{uid}")


@router.post("/accounts", dependencies=[Depends(require_admin)])
async def create_account(request: Request, body: AccountCreate) -> dict[str, Any]:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service

    clean_uid = _validate_uid(body.uid)
    if await asyncio.to_thread(db.get_account, clean_uid) is not None:
        raise HTTPException(status_code=409, detail="该账号已在监控列表中")

    account = await asyncio.to_thread(
        lambda: db.create_account(
            uid=clean_uid,
            cookie=body.cookie,
            enabled=body.enabled,
            set_active=body.set_active,
        )
    )
    scheduler_mgr.reschedule()
    _schedule_account_bootstrap(
        request,
        clean_uid,
        cookie=body.cookie.strip() or None,
        run_crawl=body.enabled,
    )

    crawling_uid = crawl_service.crawling_uid

    def _load_state() -> tuple[str, list[dict[str, Any]]]:
        active_uid = db.get_setting("uid", "").strip()
        accounts = db.get_accounts_summary(crawling_uid=crawling_uid)
        return active_uid, accounts

    active_uid, accounts = await asyncio.to_thread(_load_state)
    _emit_accounts_event(request, "accounts_changed", uid=clean_uid)
    return {
        "active_uid": active_uid,
        "account": account,
        "accounts": accounts,
    }


@router.put("/accounts/reorder", dependencies=[Depends(require_admin)])
async def reorder_accounts(request: Request, body: AccountReorderRequest) -> dict[str, Any]:
    db = request.app.state.db
    crawl_service = request.app.state.crawl_service

    cleaned_uids = [_validate_uid(u) for u in body.uids]
    crawling_uid = crawl_service.crawling_uid

    def _reorder() -> tuple[str, list[dict[str, Any]]]:
        accounts = db.reorder_accounts(cleaned_uids, crawling_uid=crawling_uid)
        active_uid = db.get_setting("uid", "").strip()
        return active_uid, accounts

    active_uid, accounts = await asyncio.to_thread(_reorder)
    _emit_accounts_event(request, "accounts_changed", uid=active_uid)
    return {
        "active_uid": active_uid,
        "accounts": accounts,
    }


@router.put("/accounts/{uid}", dependencies=[Depends(require_admin)])
async def update_account(request: Request, uid: str, body: AccountUpdate) -> dict[str, Any]:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service

    clean_uid = _validate_uid(uid)
    updated = await asyncio.to_thread(
        lambda: db.update_account(
            uid=clean_uid,
            cookie=body.cookie,
            enabled=body.enabled,
        )
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="未找到该监控账号")

    scheduler_mgr.reschedule()
    if body.enabled is True:
        latest_snap, _ = await asyncio.to_thread(db.get_latest_two_snapshots, clean_uid)
        if latest_snap is None:
            _schedule_account_bootstrap(request, clean_uid, run_crawl=True)

    crawling_uid = crawl_service.crawling_uid

    def _load_state() -> tuple[str, list[dict[str, Any]]]:
        active_uid = db.get_setting("uid", "").strip()
        accounts = db.get_accounts_summary(crawling_uid=crawling_uid)
        return active_uid, accounts

    active_uid, accounts = await asyncio.to_thread(_load_state)
    _emit_accounts_event(request, "accounts_changed", uid=clean_uid)
    return {
        "active_uid": active_uid,
        "account": updated,
        "accounts": accounts,
    }


@router.post("/accounts/{uid}/activate", dependencies=[Depends(require_admin)])
async def activate_account(
    request: Request,
    uid: str,
    include_accounts: bool = Query(
        default=True,
        description="Whether to compute and return full accounts summary list",
    ),
) -> dict[str, Any]:
    db = request.app.state.db
    crawl_service = request.app.state.crawl_service

    clean_uid = _validate_uid(uid)
    crawling_uid = crawl_service.crawling_uid

    def _activate() -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
        activated = db.set_active_account(clean_uid, include_summary=False)
        if activated is None:
            return None, []
        if not include_accounts:
            return activated, []
        accounts = db.get_accounts_summary(crawling_uid=crawling_uid)
        summary_acc = next((a for a in accounts if a["uid"] == clean_uid), activated)
        return summary_acc, accounts

    activated, accounts = await asyncio.to_thread(_activate)
    if activated is None:
        raise HTTPException(status_code=404, detail="未找到该监控账号")

    return {
        "active_uid": clean_uid,
        "account": activated,
        "accounts": accounts,
    }


@router.delete("/accounts/{uid}", dependencies=[Depends(require_admin)])
async def delete_account(
    request: Request,
    uid: str,
    delete_data: bool = Query(default=False, description="Also delete snapshots and cached videos"),
) -> dict[str, Any]:
    db = request.app.state.db
    scheduler_mgr = request.app.state.scheduler
    crawl_service = request.app.state.crawl_service

    clean_uid = _validate_uid(uid)
    result = await asyncio.to_thread(
        lambda: db.delete_account(clean_uid, delete_data=delete_data)
    )
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该监控账号")

    scheduler_mgr.reschedule()
    accounts = await asyncio.to_thread(
        lambda: db.get_accounts_summary(crawling_uid=crawl_service.crawling_uid)
    )
    _emit_accounts_event(request, "accounts_changed", uid=clean_uid)
    return {
        **result,
        "accounts": accounts,
    }
