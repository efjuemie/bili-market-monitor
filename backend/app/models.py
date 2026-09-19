from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sessions: Mapped[List["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    favorites: Mapped[List["BiliFavorite"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notification_reads: Mapped[List["NotificationRead"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    usage_daily: Mapped[List["UserUsageDaily"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user: Mapped[User] = relationship(back_populates="sessions")


class EmailVerificationChallenge(Base):
    __tablename__ = "email_verification_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    pending_email: Mapped[str] = mapped_column(String(320), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class BiliProduct(Base):
    __tablename__ = "bili_products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    cover_url: Mapped[Optional[str]] = mapped_column(Text)
    detail_url: Mapped[str] = mapped_column(Text, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    reference_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    purchase_button_text: Mapped[Optional[str]] = mapped_column(Text)
    delivery_mode: Mapped[Optional[str]] = mapped_column(Text)
    recent_avg_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    recent_deal_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    recent_deal_time_text: Mapped[Optional[str]] = mapped_column(Text)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    favorites: Mapped[List["BiliFavorite"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    history: Mapped[List["BiliPriceHistory"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    history_rollups: Mapped[List["BiliPriceHistoryRollup"]] = relationship(back_populates="product", cascade="all, delete-orphan")


ALLOWED_INTERVALS = (10, 30, 60, 180, 300, 600, 1800, 3600)


class BiliFavorite(Base):
    __tablename__ = "bili_favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_bili_favorite_user_product"),
        CheckConstraint("check_interval_seconds IN (10,30,60,180,300,600,1800,3600)", name="ck_bili_favorite_interval"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False, index=True)
    target_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    notify_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    next_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_condition_met: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_alert_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    last_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_manual_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="favorites")
    product: Mapped[BiliProduct] = relationship(back_populates="favorites")


class BiliPriceHistory(Base):
    __tablename__ = "bili_price_history"
    __table_args__ = (Index("ix_bili_price_history_product_observed", "product_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    reference_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    product: Mapped[BiliProduct] = relationship(back_populates="history")


class BiliPriceHistoryRollup(Base):
    __tablename__ = "bili_price_history_rollups"
    __table_args__ = (
        UniqueConstraint("product_id", "bucket_start", "resolution_seconds", name="uq_bili_price_history_rollup_bucket"),
        CheckConstraint("resolution_seconds IN (60,300,1800)", name="ck_bili_price_history_rollup_resolution"),
        Index("ix_bili_price_history_rollup_product_resolution_start", "product_id", "resolution_seconds", "bucket_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolution_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_count: Mapped[int] = mapped_column(Integer, nullable=False)
    last_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    min_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    max_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    last_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    last_reference_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    product: Mapped[BiliProduct] = relationship(back_populates="history_rollups")


class BiliRequestEvent(Base):
    __tablename__ = "bili_request_events"
    __table_args__ = (Index("ix_bili_request_events_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cluster_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    duration_ms: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    recipient_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SiteNotification(Base):
    __tablename__ = "site_notifications"
    __table_args__ = (
        Index("ix_site_notifications_target_published", "target_user_id", "published_at"),
        Index("ix_site_notifications_published_at", "published_at"),
        Index("ix_site_notifications_expires_at", "expires_at"),
        UniqueConstraint("source_key", name="uq_site_notifications_source_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_user_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action_url: Mapped[Optional[str]] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_key: Mapped[Optional[str]] = mapped_column(String(255))
    created_by_admin_id: Mapped[Optional[str]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    reads: Mapped[List["NotificationRead"]] = relationship(back_populates="notification", cascade="all, delete-orphan")


class NotificationRead(Base):
    __tablename__ = "notification_reads"
    __table_args__ = (
        UniqueConstraint("user_id", "notification_id", name="uq_notification_read_user_notification"),
        Index("ix_notification_reads_user_read_at", "user_id", "read_at"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    notification_id: Mapped[str] = mapped_column(ForeignKey("site_notifications.id", ondelete="CASCADE"), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="notification_reads")
    notification: Mapped[SiteNotification] = relationship(back_populates="reads")


class UserUsageDaily(Base):
    __tablename__ = "user_usage_daily"
    __table_args__ = (
        UniqueConstraint("user_id", "usage_date", name="uq_user_usage_daily_user_date"),
        Index("ix_user_usage_daily_usage_date", "usage_date"),
    )

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    monitor_evaluations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price_alerts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notifications_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    user: Mapped[User] = relationship(back_populates="usage_daily")
