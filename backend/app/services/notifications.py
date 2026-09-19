from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.errors import AppError
from app.models import NotificationRead, SiteNotification

NOTIFICATION_KINDS = {
    "price_alert",
    "email_verified",
    "email_changed",
    "admin",
    "version_update",
    "system",
    "maintenance",
    "usage_notice",
}
NOTIFICATION_SEVERITIES = {"info", "success", "warning", "important"}
ALLOWED_TEMPLATE_KEYS = {"version_update", "usage_notice", "load_advice", "maintenance", "email_setup", "custom"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_HTML_TAG_RE = re.compile(r"<[^>]*>")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def validate_action_url(value: Optional[str]) -> Optional[str]:
    """Validate a same-site relative URL before persisting it."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if _URL_CONTROL_RE.search(value) or "\\" in value or not value.startswith("/") or value.startswith("//"):
        raise AppError("INVALID_NOTIFICATION_ACTION_URL", "通知跳转地址必须是站内相对路径", 422)
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise AppError("INVALID_NOTIFICATION_ACTION_URL", "通知跳转地址必须是站内相对路径", 422)
    return value


def _plain_text(value: str, field_name: str, max_length: int) -> str:
    value = value.strip()
    if not value or len(value) > max_length or _CONTROL_RE.search(value) or _HTML_TAG_RE.search(value):
        raise AppError("INVALID_NOTIFICATION_CONTENT", f"通知{field_name}必须是纯文本", 422)
    return value


def create_site_notification(
    db: Session,
    *,
    kind: str,
    severity: str,
    title: str,
    body: str,
    source: str,
    source_key: Optional[str] = None,
    target_user_id: Optional[str] = None,
    action_url: Optional[str] = None,
    published_at: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
    created_by_admin_id: Optional[str] = None,
) -> tuple[SiteNotification, bool]:
    if kind not in NOTIFICATION_KINDS:
        raise AppError("INVALID_NOTIFICATION_KIND", "通知类型无效", 422)
    if severity not in NOTIFICATION_SEVERITIES:
        raise AppError("INVALID_NOTIFICATION_SEVERITY", "通知级别无效", 422)
    if not source or len(source) > 50 or _CONTROL_RE.search(source):
        raise AppError("INVALID_NOTIFICATION_SOURCE", "通知来源无效", 422)
    if source_key is not None:
        source_key = source_key.strip()
        if not source_key or len(source_key) > 255:
            raise AppError("INVALID_NOTIFICATION_SOURCE_KEY", "通知去重键无效", 422)
        existing = db.scalar(select(SiteNotification).where(SiteNotification.source_key == source_key))
        if existing is not None:
            return existing, False
    now = utcnow()
    published_at = _aware(published_at or now)
    expires_at = _aware(expires_at) if expires_at is not None else None
    if expires_at is not None and expires_at <= published_at:
        raise AppError("INVALID_NOTIFICATION_EXPIRY", "通知过期时间必须晚于发布时间", 422)
    notification = SiteNotification(
        id=str(uuid4()),
        target_user_id=target_user_id,
        kind=kind,
        severity=severity,
        title=_plain_text(title, "标题", 200),
        body=_plain_text(body, "内容", 5000),
        action_url=validate_action_url(action_url),
        source=source,
        source_key=source_key,
        created_by_admin_id=created_by_admin_id,
        published_at=published_at,
        expires_at=expires_at,
        created_at=now,
    )
    if source_key is None:
        db.add(notification)
        return notification, True
    values = {
        "id": notification.id,
        "target_user_id": notification.target_user_id,
        "kind": notification.kind,
        "severity": notification.severity,
        "title": notification.title,
        "body": notification.body,
        "action_url": notification.action_url,
        "source": notification.source,
        "source_key": notification.source_key,
        "created_by_admin_id": notification.created_by_admin_id,
        "published_at": notification.published_at,
        "expires_at": notification.expires_at,
        "created_at": notification.created_at,
    }
    table = SiteNotification.__table__
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(values).on_conflict_do_nothing(index_elements=["source_key"])
        created = bool(db.execute(statement).rowcount)
    elif dialect == "postgresql":
        statement = postgres_insert(table).values(values).on_conflict_do_nothing(index_elements=["source_key"])
        created = bool(db.execute(statement).rowcount)
    else:
        try:
            with db.begin_nested():
                db.add(notification)
                db.flush()
            created = True
        except IntegrityError:
            created = False
    if created:
        return db.get(SiteNotification, notification.id) or notification, True
    existing = db.scalar(select(SiteNotification).where(SiteNotification.source_key == source_key))
    if existing is None:
        raise AppError("NOTIFICATION_DEDUPE_FAILED", "通知去重失败，请稍后重试", 409)
    return existing, False


def visible_notification_filter(user_id: str, now: Optional[datetime] = None):
    now = now or utcnow()
    return and_(
        or_(SiteNotification.target_user_id == user_id, SiteNotification.target_user_id.is_(None)),
        SiteNotification.published_at <= now,
        or_(SiteNotification.expires_at.is_(None), SiteNotification.expires_at > now),
    )


def _cursor_encode(published_at: datetime, notification_id: str) -> str:
    payload = f"{published_at.isoformat()}|{notification_id}".encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_decode(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        created_at, notification_id = decoded.rsplit("|", 1)
        return datetime.fromisoformat(created_at), notification_id
    except (ValueError, UnicodeDecodeError, TypeError, binascii.Error) as exc:
        raise AppError("INVALID_NOTIFICATION_CURSOR", "通知游标无效", 422) from exc


def notification_item(notification: SiteNotification, read: bool = False) -> dict[str, Any]:
    return {
        "id": notification.id,
        "kind": notification.kind,
        "severity": notification.severity,
        "title": notification.title,
        "body": notification.body,
        "action_url": notification.action_url,
        "source": notification.source,
        "published_at": notification.published_at.isoformat(),
        "expires_at": notification.expires_at.isoformat() if notification.expires_at else None,
        "created_at": notification.created_at.isoformat(),
        "read": read,
    }


def list_visible_notifications(db: Session, user_id: str, *, limit: int = 20, cursor: Optional[str] = None) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise AppError("INVALID_NOTIFICATION_LIMIT", "通知页大小必须在 1 到 100 之间", 422)
    now = utcnow()
    query = select(SiteNotification, NotificationRead.notification_id.is_not(None)).outerjoin(
        NotificationRead,
        and_(NotificationRead.notification_id == SiteNotification.id, NotificationRead.user_id == user_id),
    ).where(visible_notification_filter(user_id, now))
    if cursor:
        cursor_published_at, cursor_id = _cursor_decode(cursor)
        query = query.where(
            or_(
                SiteNotification.published_at < cursor_published_at,
                and_(SiteNotification.published_at == cursor_published_at, SiteNotification.id < cursor_id),
            )
        )
    query = query.order_by(SiteNotification.published_at.desc(), SiteNotification.id.desc()).limit(limit + 1)
    rows = db.execute(query).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _cursor_encode(rows[-1][0].published_at, rows[-1][0].id) if has_more and rows else None
    return {
        "items": [notification_item(notification, bool(read)) for notification, read in rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def unread_count(db: Session, user_id: str) -> int:
    query = select(func.count()).select_from(SiteNotification).outerjoin(
        NotificationRead,
        and_(NotificationRead.notification_id == SiteNotification.id, NotificationRead.user_id == user_id),
    ).where(visible_notification_filter(user_id), NotificationRead.notification_id.is_(None))
    return int(db.scalar(query) or 0)


def mark_notification_read(db: Session, user_id: str, notification_id: str, now: Optional[datetime] = None) -> bool:
    now = now or utcnow()
    notification = db.scalar(select(SiteNotification).where(SiteNotification.id == notification_id, visible_notification_filter(user_id, now)))
    if notification is None:
        raise AppError("NOTIFICATION_NOT_FOUND", "通知不存在或无权访问", 404)
    _insert_reads(db, [(user_id, notification_id, now)])
    return True


def mark_all_notifications_read(db: Session, user_id: str, now: Optional[datetime] = None) -> int:
    now = now or utcnow()
    notifications = db.scalars(select(SiteNotification.id).where(visible_notification_filter(user_id, now))).all()
    return _insert_reads(db, [(user_id, notification_id, now) for notification_id in notifications])


def _insert_reads(db: Session, values: list[tuple[str, str, datetime]]) -> int:
    if not values:
        return 0
    table = NotificationRead.__table__
    rows = [{"user_id": user_id, "notification_id": notification_id, "read_at": read_at} for user_id, notification_id, read_at in values]
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(rows).on_conflict_do_nothing(index_elements=["user_id", "notification_id"])
        return int(db.execute(statement).rowcount or 0)
    if dialect == "postgresql":
        statement = postgres_insert(table).values(rows).on_conflict_do_nothing(index_elements=["user_id", "notification_id"])
        return int(db.execute(statement).rowcount or 0)
    inserted = 0
    for row in rows:
        try:
            with db.begin_nested():
                db.add(NotificationRead(**row))
                db.flush()
            inserted += 1
        except IntegrityError:
            continue
    return inserted


def prune_notifications(db: Session, *, now: Optional[datetime] = None, retention_days: int = 180) -> int:
    now = now or utcnow()
    cutoff = now - timedelta(days=retention_days)
    old_query = select(SiteNotification.id).where(
        or_(
            SiteNotification.expires_at < now,
            and_(SiteNotification.published_at <= now, SiteNotification.published_at < cutoff),
        )
    )
    ids = db.scalars(old_query).all()
    if not ids:
        return 0
    db.execute(delete(NotificationRead).where(NotificationRead.notification_id.in_(ids)))
    db.execute(delete(SiteNotification).where(SiteNotification.id.in_(ids)))
    return len(ids)


def render_template(template: str, values: dict[str, str]) -> tuple[str, str]:
    templates = {
        "version_update": (
            "Bili Market Monitor版本更新",
            "B站市集好价提示系统已完成版本更新。本次包含功能优化和稳定性改进，如遇异常可重新登录或稍后重试。",
        ),
        "usage_notice": (
            "关于监控任务的使用建议",
            "系统检测到你的监控商品数量较多或部分商品使用了较高刷新频率，可能产生较高的监控负载。为保证长期稳定使用，建议根据实际需要适当减少同时监控的商品，或将非紧急商品调整为更低的刷新频率。本消息仅为使用建议，不会自动修改你的任何设置。",
        ),
        "load_advice": (
            "关于监控任务的使用建议",
            "系统检测到你的监控商品数量较多或部分商品使用了较高刷新频率，可能产生较高的监控负载。为保证长期稳定使用，建议根据实际需要适当减少同时监控的商品，或将非紧急商品调整为更低的刷新频率。本消息仅为使用建议，不会自动修改你的任何设置。",
        ),
        "maintenance": (
            "系统维护通知",
            "系统计划进行短时维护，维护期间商品检查或邮件提醒可能出现延迟。维护完成后服务会自动恢复。",
        ),
        "email_setup": (
            "请检查你的通知邮箱设置",
            "为了正常接收低价邮件提醒，请确认已在“个人资料”中绑定并验证可用的通知邮箱。",
        ),
    }
    if template == "custom":
        raise AppError("INVALID_NOTIFICATION_TEMPLATE", "自定义通知必须直接提供标题和内容", 422)
    if template not in ALLOWED_TEMPLATE_KEYS:
        raise AppError("INVALID_NOTIFICATION_TEMPLATE", "通知模板无效", 422)
    title, body = templates[template]
    return render_custom_text(title, body, values)


def render_custom_text(title: str, body: str, values: dict[str, str]) -> tuple[str, str]:
    allowed = {"username", "version", "favorite_count", "enabled_monitor_count", "estimated_checks_per_day"}
    unknown = set(values) - allowed
    if unknown:
        raise AppError("INVALID_NOTIFICATION_PLACEHOLDER", "通知占位符无效", 422)
    for key, value in values.items():
        title = title.replace("{" + key + "}", value)
        body = body.replace("{" + key + "}", value)
    return title, body
