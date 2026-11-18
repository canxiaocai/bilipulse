import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.auth import (
    clear_admin_session_cookie,
    get_admin_auth_info,
    get_admin_status_payload,
    set_admin_session_cookie,
    verify_admin_password,
)
from app.models import AdminAuthStatus, AdminVerifyRequest

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/status", response_model=AdminAuthStatus)
async def get_admin_status(request: Request) -> AdminAuthStatus:
    payload = await asyncio.to_thread(get_admin_status_payload, request)
    return AdminAuthStatus(**payload)


@router.post("/verify")
async def verify_admin(
    request: Request, response: Response, body: AdminVerifyRequest
) -> dict[str, Any]:
    db = request.app.state.db
    limiter = request.app.state.admin_rate_limiter
    info = await asyncio.to_thread(get_admin_auth_info, db)

    if not info["enabled"]:
        payload = await asyncio.to_thread(get_admin_status_payload, request)
        return {
            "ok": True,
            **payload,
        }

    limiter.check_allowed(request)

    if not await asyncio.to_thread(verify_admin_password, db, body.password):
        limiter.record_failure(request)
        raise HTTPException(status_code=401, detail="管理员密码错误，请重试")

    limiter.reset(request)
    set_admin_session_cookie(response, db, request)
    return {
        "ok": True,
        "enabled": True,
        "authenticated": True,
        "source": info["source"],
    }


@router.post("/logout")
async def logout_admin(request: Request, response: Response) -> dict[str, Any]:
    clear_admin_session_cookie(response)
    payload = await asyncio.to_thread(
        lambda: get_admin_status_payload(request, force_unauthenticated=True)
    )
    return {
        "ok": True,
        **payload,
    }
