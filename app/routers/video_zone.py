import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.bilibili.client import BilibiliAPIError, BilibiliClient, parse_video_identifier
from app.models import VideoZoneLookupOut

router = APIRouter(prefix="/api", tags=["video-zone"])

_NOT_FOUND_CODE_MESSAGES: dict[int, str] = {
    -404: "未找到该视频，请检查 BV 号或链接是否正确",
    62002: "该视频已不可见或已被删除",
    62004: "该视频正在审核中，暂无法查询分区",
    62012: "该视频仅 UP 主自己可见，暂无法查询",
}


@router.get("/video-zone/lookup", response_model=VideoZoneLookupOut)
async def lookup_video_zone(
    request: Request,
    response: Response,
    q: str = Query(
        ...,
        min_length=1,
        max_length=1000,
        description="Bilibili BV号、av号、视频链接或 b23.tv 短链",
    ),
) -> VideoZoneLookupOut:
    """One-off stateless query for any Bilibili video's partition and metadata."""
    response.headers["Cache-Control"] = "no-store"

    parsed = parse_video_identifier(q)
    if not parsed:
        raise HTTPException(
            status_code=400,
            detail="未能识别有效的 B站视频 BV 号或链接，请输入 BV 号或完整视频链接",
        )

    db = request.app.state.db
    cookie = (await asyncio.to_thread(db.get_setting, "cookie", "")).strip()

    try:
        async with BilibiliClient(cookie=cookie) as client:
            target = parsed
            if target.get("short_url"):
                target = await client.resolve_short_url(target["short_url"])

            info = await client.get_video_zone_info(
                bvid=target.get("bvid"),
                aid=target.get("aid"),
            )
            return VideoZoneLookupOut(**info)
    except BilibiliAPIError as exc:
        if exc.code in _NOT_FOUND_CODE_MESSAGES:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_CODE_MESSAGES[exc.code])
        if exc.code in (-400, 400):
            detail = str(exc) if exc.code == 400 else "无效的视频参数，请检查 BV 号或链接"
            raise HTTPException(status_code=400, detail=detail)
        if exc.code in (412, 429, -799):
            raise HTTPException(
                status_code=503,
                detail="B站接口触发临时风控或限流，请稍后再试",
            )
        raise HTTPException(status_code=502, detail=f"B站接口返回异常：{exc}")
    except httpx.HTTPError:
        raise HTTPException(
            status_code=502,
            detail="请求 B站接口超时或网络异常，请稍后再试",
        )
