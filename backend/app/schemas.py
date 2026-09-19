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

    @model_validator(mode="after")
    def require_content(self) -> "SiteNotificationCreateRequest":
        if self.template is None and (not self.title or not self.body):
            raise ValueError("通知标题和内容不能为空")
        if self.template == "custom" and (not self.title or not self.body):
            raise ValueError("自定义通知标题和内容不能为空")
        return self
