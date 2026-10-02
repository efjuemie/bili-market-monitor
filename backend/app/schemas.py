import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models import ALLOWED_INTERVALS


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class LoginRequest(BaseModel):
    username: str
    password: str


class ForgotPasswordRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=20)
    password: str = Field(min_length=8, max_length=128)


class EmailCodeRequest(BaseModel):
    email: EmailStr


class VerifyEmailRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class FavoriteCreateRequest(BaseModel):
    cluster_id: int = Field(gt=0, le=9_999_999_999_999)
    target_price: Optional[Decimal] = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    notify_enabled: bool = False
    check_interval_seconds: int = Field(default=300)

    @field_validator("check_interval_seconds")
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value not in ALLOWED_INTERVALS:
            raise ValueError("请选择系统支持的监控频率")
        return value


class FavoriteUpdateRequest(BaseModel):
    target_price: Optional[Decimal] = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    notify_enabled: Optional[bool] = None
    check_interval_seconds: Optional[int] = None

    @field_validator("check_interval_seconds")
    @classmethod
    def validate_interval(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value not in ALLOWED_INTERVALS:
            raise ValueError("请选择系统支持的监控频率")
        return value


class ProductResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cluster_id: int
    title: str
    cover_url: Optional[str]
    detail_url: str
    available: bool
    current_price: Optional[str]
    reference_price: Optional[str]
    purchase_button_text: Optional[str]
    delivery_mode: Optional[str]
    recent_avg_price: Optional[str]
    recent_deal_price: Optional[str]
    recent_deal_time_text: Optional[str]
    last_checked_at: Optional[str]
    last_success_at: Optional[str]
    last_error: Optional[str] = None


class HistoryPoint(BaseModel):
    observed_at: str
    period_end: Optional[str] = None
    available: bool
    current_price: Optional[str]
    reference_price: Optional[str]
    resolution_seconds: Optional[int] = None
    sample_count: int = 1
    min_price: Optional[str] = None
    max_price: Optional[str] = None


class FavoriteResponse(BaseModel):
    id: str
    cluster_id: int
    product: ProductResponse
    target_price: Optional[str]
    notify_enabled: bool
    check_interval_seconds: int
    next_check_at: Optional[str]
    last_evaluated_at: Optional[str]
    last_condition_met: bool
    last_alert_price: Optional[str]
    last_alert_at: Optional[str]
    last_manual_refresh_at: Optional[str]


class UserResponse(BaseModel):
    id: str
    username: str
    email: Optional[str]
    email_verified: bool
    role: str
    is_active: bool
    monitor_access_status: str
    created_at: str


class UserProfileResponse(UserResponse):
    pass


class AdminUserResponse(UserResponse):
    favorite_count: int
    enabled_monitor_count: int


class SiteNotificationCreateRequest(BaseModel):
    kind: str = Field(default="admin", min_length=1, max_length=50)
    severity: str = Field(default="info", min_length=1, max_length=20)
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=5000)
    action_url: Optional[str] = Field(default=None, max_length=2000)
    template: Optional[str] = Field(default=None, max_length=50)
    template_values: dict[str, str] = Field(default_factory=dict)
    expires_at: Optional[datetime] = None
    send_email: bool = False

    @model_validator(mode="after")
    def require_content(self) -> "SiteNotificationCreateRequest":
        if self.template is None and (not self.title or not self.body):
            raise ValueError("通知标题和内容不能为空")
        if self.template == "custom" and (not self.title or not self.body):
            raise ValueError("自定义通知标题和内容不能为空")
        return self


_PLAIN_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PLAIN_TEXT_HTML_RE = re.compile(r"<[^>]*>")


def _validate_plain_text(value: object, *, field_name: str, min_length: int = 0, max_length: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name}必须是文本")
    value = value.strip()
    if len(value) < min_length:
        raise ValueError(f"{field_name}至少需要{min_length}个字符")
    if len(value) > max_length:
        raise ValueError(f"{field_name}不能超过{max_length}个字符")
    if _PLAIN_TEXT_CONTROL_RE.search(value) or _PLAIN_TEXT_HTML_RE.search(value):
        raise ValueError(f"{field_name}必须是纯文本")
    return value


class MonitorAccessRequestCreate(BaseModel):
    reason: str

    @field_validator("reason", mode="before")
    @classmethod
    def validate_reason(cls, value: object) -> str:
        return _validate_plain_text(value, field_name="申请理由", min_length=10, max_length=500)


class MonitorAccessRejectRequest(BaseModel):
    review_note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("review_note", mode="before")
    @classmethod
    def validate_review_note(cls, value: object) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("审核说明必须是文本")
        value = value.strip()
        if not value:
            return None
        return _validate_plain_text(value, field_name="审核说明", max_length=500)


class AdminFavoriteIntervalBatchRequest(BaseModel):
    favorite_ids: list[str] = Field(min_length=1, max_length=20)
    check_interval_seconds: int

    @field_validator("favorite_ids")
    @classmethod
    def validate_favorite_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("收藏ID不能重复")
        if any(not item or len(item) > 36 for item in value):
            raise ValueError("收藏ID格式无效")
        return value

    @field_validator("check_interval_seconds")
    @classmethod
    def validate_interval(cls, value: int) -> int:
        if value not in ALLOWED_INTERVALS:
            raise ValueError("请选择系统支持的监控频率")
        return value
