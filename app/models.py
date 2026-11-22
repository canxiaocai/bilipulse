from typing import Any, Optional, Union
from pydantic import BaseModel, Field


class AdminAuthStatus(BaseModel):
    enabled: bool = False
    authenticated: bool = True
    source: str = "none"


class AdminVerifyRequest(BaseModel):
    password: str = Field(default="", description="Admin password")


class SettingsOut(BaseModel):
    uid: str = ""
    has_cookie: bool = False
    cookie_preview: str = ""
    interval_minutes: int = 5
    enabled: bool = True
    request_delay_ms: int = 200
    video_sync_interval_minutes: int = 30
    snapshot_retention_days: int = 90
    account_name: str = ""
    account_face: str = ""
    admin_auth: AdminAuthStatus = Field(default_factory=AdminAuthStatus)


class SettingsUpdate(BaseModel):
    uid: Optional[str] = Field(default=None, description="Bilibili user UID")
    cookie: Optional[str] = Field(
        default=None,
        description="Optional Bilibili cookie string; pass empty string to clear, omit to keep existing",
    )
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    enabled: Optional[bool] = None
    request_delay_ms: Optional[int] = Field(default=None, ge=50, le=5000)
    video_sync_interval_minutes: Optional[int] = Field(default=None, ge=5, le=1440)
    snapshot_retention_days: Optional[int] = Field(
        default=None,
        ge=0,
        le=3650,
        description="Snapshot retention in days; 0 keeps history forever",
    )
    admin_password: Optional[str] = Field(
        default=None,
        description="Optional admin password; pass empty string to clear DB password, omit to keep existing",
    )


class ChartSnapshotOut(BaseModel):
    collected_at: str
    collected_at_ts: int
    follower_count: Optional[int] = None
    online_viewers_total: Optional[int] = None


class SnapshotOut(BaseModel):
    id: int
    uid: str
    collected_at: str
    collected_at_ts: int
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    online_viewers_total: Optional[int] = None
    video_count: int = 0
    videos_succeeded: int = 0
    extra_metrics: dict[str, Any] = Field(default_factory=dict)
    status: str
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None


class VideoOut(BaseModel):
    bvid: str
    uid: str
    aid: int
    title: str
    pic: str = ""
    pubdate: int = 0
    duration: int = 0
    play_count: int = 0
    play_count_7d: Optional[int] = None
    play_count_7d_status: str = "unavailable"
    like_count: int = 0
    coin_count: int = 0
    tid: int = 0
    tid_v2: int = 0
    zone_primary: str = ""
    zone_sub: str = ""
    zone_version: str = ""
    stats_synced_at: Optional[str] = None
    cid_count: int = 1
    last_online_count: int = 0
    last_online_raw: str = ""
    last_checked_at: Optional[str] = None


class AccountCreate(BaseModel):
    uid: str = Field(..., description="Bilibili user UID")
    cookie: str = Field(default="", description="Optional Bilibili cookie string")
    enabled: bool = Field(default=True, description="Enable scheduled crawling for this account")
    set_active: bool = Field(default=True, description="Switch active view to this account")


class AccountUpdate(BaseModel):
    cookie: Optional[str] = Field(
        default=None,
        description="Optional Bilibili cookie string; pass empty string to clear, omit to keep existing",
    )
    enabled: Optional[bool] = None


class AccountReorderRequest(BaseModel):
    uids: list[str] = Field(default_factory=list, description="Ordered list of account UIDs")


class AccountSummary(BaseModel):
    uid: str
    account_name: str = ""
    account_face: str = ""
    has_cookie: bool = False
    cookie_preview: str = ""
    enabled: bool = True
    sort_order: int = 0
    latest_follower_count: Optional[int] = None
    latest_online_viewers_total: Optional[int] = None
    follower_delta: Optional[int] = None
    online_delta: Optional[int] = None
    video_count: int = 0
    last_crawl_at: Optional[str] = None
    last_crawl_status: Optional[str] = None
    is_crawling: bool = False


class AccountsListOut(BaseModel):
    active_uid: str = ""
    accounts: list[AccountSummary] = Field(default_factory=list)


class SchedulerStatus(BaseModel):
    running: bool
    enabled: bool
    interval_minutes: int
    next_run_time: Optional[str] = None
    is_crawling: bool = False
    crawling_uid: Optional[str] = None
    enabled_accounts_count: int = 0
    last_crawl_at: Optional[str] = None
    last_crawl_status: Optional[str] = None


class FollowerDailyChange(BaseModel):
    yesterday: Optional[int] = None
    yesterday_same_time: Optional[int] = None
    yesterday_same_time_at: str = ""
    today: Optional[int] = None
    yesterday_date: str = ""
    today_date: str = ""
    timezone: str = "Asia/Shanghai"


class DashboardSummary(BaseModel):
    settings: SettingsOut
    scheduler: SchedulerStatus
    admin_auth: AdminAuthStatus = Field(default_factory=AdminAuthStatus)
    latest: Optional[SnapshotOut] = None
    previous: Optional[SnapshotOut] = None
    follower_delta: Optional[int] = None
    online_delta: Optional[int] = None
    follower_daily: FollowerDailyChange = Field(default_factory=FollowerDailyChange)
    total_snapshots: int = 0
    total_videos: int = 0
    snapshots: list[Union[SnapshotOut, ChartSnapshotOut]] = Field(default_factory=list)
    snapshots_downsampled: bool = False
    top_videos: list[VideoOut] = Field(default_factory=list)
    accounts: list[AccountSummary] = Field(default_factory=list)
