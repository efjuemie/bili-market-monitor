from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import ALLOWED_INTERVALS


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


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
    available: bool
    current_price: Optional[str]
    reference_price: Optional[str]


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
