import json
from datetime import timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import case, delete, desc, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from app.api.deps import get_current_admin
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import utcnow
from app.errors import AppError
from app.models import (
    AdminAuditLog,
    BiliFavorite,
    BiliPriceHistory,
    BiliPriceHistoryRollup,
    BiliProduct,
    BiliRequestEvent,
    NotificationOutbox,
    NotificationRead,
    SiteNotification,
    User,
    UserSession,
    UserUsageDaily,
    WorkerHeartbeat,
)
from app.schemas import SiteNotificationCreateRequest
from app.services.notifications import create_site_notification, render_custom_text, render_template

router = APIRouter(prefix="/admin", tags=["admin"])


def _audit(db: Session, admin: User, action: str, target_type: str, target_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
    db.add(AdminAuditLog(admin_user_id=admin.id, action=action, target_type=target_type, target_id=target_id, metadata_json=json.dumps(metadata or {}, ensure_ascii=False), created_at=utcnow()))


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), admin: User = Depends(get_current_admin), settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    now = utcnow()
    day_ago = now - timedelta(days=1)
    users = db.scalar(select(func.count()).select_from(User)) or 0
    new_users = db.scalar(select(func.count()).select_from(User).where(User.created_at >= day_ago)) or 0
    verified = db.scalar(select(func.count()).select_from(User).where(User.email_verified_at.is_not(None))) or 0
    favorites = db.scalar(select(func.count()).select_from(BiliFavorite)) or 0
    enabled = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.notify_enabled.is_(True))) or 0
    products = db.scalar(select(func.count()).select_from(BiliProduct)) or 0
    request_events = select(BiliRequestEvent).where(BiliRequestEvent.created_at >= day_ago)
    request_count = db.scalar(select(func.count()).select_from(request_events.subquery())) or 0
    successful_requests = db.scalar(select(func.count()).select_from(request_events.where(BiliRequestEvent.success.is_(True)).subquery())) or 0
    failed_429 = db.scalar(select(func.count()).select_from(request_events.where(BiliRequestEvent.status_code == 429).subquery())) or 0
    failed = request_count - successful_requests
    success_rate = round((successful_requests / request_count) * 100, 2) if request_count else None
    average_duration = db.scalar(select(func.avg(BiliRequestEvent.duration_ms)).where(BiliRequestEvent.created_at >= day_ago))
    pending = db.scalar(select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.status == "pending")) or 0
    sending_failed = db.scalar(select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.status == "failed")) or 0
    sent_24h = db.scalar(select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.status == "sent", NotificationOutbox.sent_at >= day_ago)) or 0
    due = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.notify_enabled.is_(True), BiliFavorite.next_check_at <= now)) or 0
    heartbeat = db.scalar(select(WorkerHeartbeat).order_by(desc(WorkerHeartbeat.last_seen_at)).limit(1))
    rollup_counts = {
        int(resolution): int(count)
        for resolution, count in db.execute(
            select(BiliPriceHistoryRollup.resolution_seconds, func.count())
            .group_by(BiliPriceHistoryRollup.resolution_seconds)
        ).all()
    }
    site_notification_count = db.scalar(select(func.count()).select_from(SiteNotification)) or 0
    notification_read_count = db.scalar(select(func.count()).select_from(NotificationRead)) or 0
    return {
        "version": settings.version_value,
        "users": {"total": users, "new_today": new_users, "verified_email": verified},
        "monitoring": {"favorites": favorites, "enabled": enabled, "unique_products": products, "due_favorites": due},
        "bili": {"last_24h_requests": request_count, "last_24h_429": failed_429, "last_24h_failures": failed, "last_24h_success_rate": success_rate, "last_24h_average_duration_ms": round(float(average_duration), 2) if average_duration is not None else None},
        "notifications": {"pending": pending, "failed": sending_failed, "sent_last_24h": sent_24h},
        "worker": {"last_heartbeat": heartbeat.last_seen_at.isoformat() if heartbeat else None, "name": heartbeat.worker_name if heartbeat else None, "metrics": json.loads(heartbeat.metadata_json) if heartbeat and heartbeat.metadata_json else {}},
        "database": {
            "ok": True,
            "price_history_rows": db.scalar(select(func.count()).select_from(BiliPriceHistory)) or 0,
            "price_history_rollup_rows": {
                "60": rollup_counts.get(60, 0),
                "300": rollup_counts.get(300, 0),
                "1800": rollup_counts.get(1800, 0),
            },
            "site_notifications": site_notification_count,
            "notification_reads": notification_read_count,
        },
        "smtp": {"configured": settings.smtp_enabled},
        "global_min_interval_seconds": settings.bili_global_min_interval_seconds,
    }


@router.get("/users")
def users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    page: int = 1,
    page_size: int = 50,
    q: Optional[str] = None,
    status: Optional[str] = None,
    email_verified: Optional[bool] = None,
    role: Optional[str] = None,
    sort: str = "created_at",
) -> Dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise AppError("INVALID_PAGINATION", "分页参数无效", 422)
    if status not in {None, "active", "inactive"}:
        raise AppError("INVALID_USER_STATUS", "用户状态筛选无效", 422)
    minimum_interval = get_settings().bili_global_min_interval_seconds
    usage_date = utcnow().date()
    effective_interval = case(
        (BiliFavorite.check_interval_seconds < minimum_interval, minimum_interval),
        else_=BiliFavorite.check_interval_seconds,
    )
    favorite_stats = (
        select(
            BiliFavorite.user_id.label("user_id"),
            func.count(BiliFavorite.id).label("favorite_count"),
            func.sum(case((BiliFavorite.notify_enabled.is_(True), 1), else_=0)).label("enabled_monitor_count"),
            func.sum(case((BiliFavorite.notify_enabled.is_(True), 86400.0 / effective_interval), else_=0.0)).label("estimated_checks_per_day"),
        )
        .group_by(BiliFavorite.user_id)
        .subquery()
    )
    usage_stats = (
        select(
            UserUsageDaily.user_id.label("user_id"),
            func.coalesce(func.sum(UserUsageDaily.monitor_evaluations), 0).label("monitor_evaluations"),
            func.coalesce(func.sum(UserUsageDaily.price_alerts), 0).label("price_alerts"),
            func.coalesce(func.sum(UserUsageDaily.notifications_created), 0).label("notifications_created"),
        )
        .where(UserUsageDaily.usage_date == usage_date)
        .group_by(UserUsageDaily.user_id)
        .subquery()
    )
    stmt = select(
        User,
        func.coalesce(favorite_stats.c.favorite_count, 0).label("favorite_count"),
        func.coalesce(favorite_stats.c.enabled_monitor_count, 0).label("enabled_monitor_count"),
        func.coalesce(favorite_stats.c.estimated_checks_per_day, 0.0).label("estimated_checks_per_day"),
        func.coalesce(usage_stats.c.monitor_evaluations, 0).label("today_monitor_evaluations"),
        func.coalesce(usage_stats.c.price_alerts, 0).label("today_price_alerts"),
        func.coalesce(usage_stats.c.notifications_created, 0).label("today_notifications_created"),
    ).outerjoin(favorite_stats, favorite_stats.c.user_id == User.id).outerjoin(usage_stats, usage_stats.c.user_id == User.id)
    count_stmt = select(func.count()).select_from(User)
    conditions = []
    if q:
        search_text = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{search_text}%"
        conditions.append(or_(User.username.ilike(pattern, escape="\\"), User.email.ilike(pattern, escape="\\")))
    if status == "active":
        conditions.append(User.is_active.is_(True))
    elif status == "inactive":
        conditions.append(User.is_active.is_(False))
    if email_verified is True:
        conditions.append(User.email_verified_at.is_not(None))
    elif email_verified is False:
        conditions.append(User.email_verified_at.is_(None))
    if role:
        conditions.append(User.role == role)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = db.scalar(count_stmt) or 0
    sort_columns = {
        "created_at": User.created_at,
        "username": User.username,
        "favorite_count": favorite_stats.c.favorite_count,
        "enabled_monitor_count": favorite_stats.c.enabled_monitor_count,
        "estimated_checks_per_day": favorite_stats.c.estimated_checks_per_day,
    }
    if sort not in sort_columns:
        raise AppError("INVALID_USER_SORT", "用户排序字段无效", 422)
    stmt = stmt.order_by(desc(sort_columns[sort]), desc(User.id)).offset((page - 1) * page_size).limit(page_size)
    rows = db.execute(stmt).all()
    items = []
    for user, favorite_count, enabled_count, estimate, monitor_evaluations, price_alerts, notifications_created in rows:
        items.append({
            "id": user.id,
            "username": user.username,
            "email": _mask_email(user.email),
            "email_verified": user.email_verified_at is not None,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat(),
            "favorite_count": int(favorite_count or 0),
            "enabled_monitor_count": int(enabled_count or 0),
            "estimated_checks_per_day": round(float(estimate or 0), 2),
            "estimate_basis": "按每个启用监控项的 86400 / max(监控频率, 全局最小间隔) 求和，仅为理论上限估算",
            "usage_today": {
                "usage_date": usage_date.isoformat(),
                "monitor_evaluations": int(monitor_evaluations or 0),
                "price_alerts": int(price_alerts or 0),
                "notifications_created": int(notifications_created or 0),
                "source": "应用层 Worker 统计",
            },
            "today_monitor_evaluations": int(monitor_evaluations or 0),
            "today_price_alerts": int(price_alerts or 0),
            "today_notifications_created": int(notifications_created or 0),
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size, "has_more": page * page_size < total}


def _mask_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


@router.get("/users/{user_id}")
def user_detail(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    user = db.get(User, user_id)
    if not user:
        raise AppError("USER_NOT_FOUND", "未找到该用户", 404)
    favorites = db.scalars(
        select(BiliFavorite)
        .options(joinedload(BiliFavorite.product))
        .where(BiliFavorite.user_id == user.id)
        .order_by(BiliFavorite.created_at.desc())
    ).all()
    active_session_count = db.scalar(select(func.count()).select_from(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at > utcnow())) or 0
    last_seen_at = db.scalar(select(func.max(UserSession.last_seen_at)).where(UserSession.user_id == user.id))
    minimum_interval = get_settings().bili_global_min_interval_seconds
    estimate = sum(
        86400 / max(item.check_interval_seconds, minimum_interval)
        for item in favorites
        if item.notify_enabled
    )
    usage_date = utcnow().date()
    usage = db.execute(
        select(
            func.coalesce(func.sum(UserUsageDaily.monitor_evaluations), 0),
            func.coalesce(func.sum(UserUsageDaily.price_alerts), 0),
            func.coalesce(func.sum(UserUsageDaily.notifications_created), 0),
        ).where(UserUsageDaily.user_id == user.id, UserUsageDaily.usage_date == usage_date)
    ).one()
    return {
        "id": user.id,
        "username": user.username,
        "email": _mask_email(user.email),
        "email_verified": user.email_verified_at is not None,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "session_monitoring": {
            "active_session_count": active_session_count,
            "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        },
        "monitoring": {
            "favorite_count": len(favorites),
            "enabled_monitor_count": sum(1 for item in favorites if item.notify_enabled),
            "estimated_checks_per_day": round(estimate, 2),
            "estimate_basis": "按每个启用监控项的 86400 / max(监控频率, 全局最小间隔) 求和，仅为理论上限估算",
            "usage_today": {
                "usage_date": usage_date.isoformat(),
                "monitor_evaluations": int(usage[0] or 0),
                "price_alerts": int(usage[1] or 0),
                "notifications_created": int(usage[2] or 0),
                "source": "应用层 Worker 统计",
            },
        },
        "usage_today": {
            "usage_date": usage_date.isoformat(),
            "monitor_evaluations": int(usage[0] or 0),
            "price_alerts": int(usage[1] or 0),
            "notifications_created": int(usage[2] or 0),
            "source": "应用层 Worker 统计",
        },
        "favorites": [
            {
                "id": item.id,
                "cluster_id": item.product.cluster_id,
                "title": item.product.title,
                "notify_enabled": item.notify_enabled,
                "target_price": str(item.target_price) if item.target_price is not None else None,
                "check_interval_seconds": item.check_interval_seconds,
                "next_check_at": item.next_check_at.isoformat() if item.next_check_at else None,
                "last_evaluated_at": item.last_evaluated_at.isoformat() if item.last_evaluated_at else None,
                "last_condition_met": item.last_condition_met,
                "last_alert_price": str(item.last_alert_price) if item.last_alert_price is not None else None,
                "last_alert_at": item.last_alert_at.isoformat() if item.last_alert_at else None,
            }
            for item in favorites
        ],
    }


@router.post("/users/{user_id}/disable")
def disable_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    user = db.get(User, user_id)
    if not user:
        raise AppError("USER_NOT_FOUND", "未找到该用户", 404)
    if user.id == admin.id:
        raise AppError("INVALID_ADMIN_ACTION", "不能禁用当前管理员账号", 400)
    user.is_active = False
    user.updated_at = utcnow()
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    db.query(NotificationOutbox).filter(NotificationOutbox.user_id == user.id, NotificationOutbox.status == "pending").update({"status": "cancelled", "last_error": "账号已禁用"}, synchronize_session=False)
    _audit(db, admin, "DISABLE_USER", "user", user.id)
    db.commit()
    return {"ok": True}


@router.post("/users/{user_id}/enable")
def enable_user(user_id: str, db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    user = db.get(User, user_id)
    if not user:
        raise AppError("USER_NOT_FOUND", "未找到该用户", 404)
    user.is_active = True
    user.updated_at = utcnow()
    _audit(db, admin, "ENABLE_USER", "user", user.id)
    db.commit()
    return {"ok": True}


@router.get("/bili-products")
def bili_products(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    monitor_stats = (
        select(
            BiliFavorite.product_id.label("product_id"),
            func.count(BiliFavorite.id).label("subscribers"),
            func.sum(case((BiliFavorite.notify_enabled.is_(True), 1), else_=0)).label("enabled_monitors"),
            func.min(BiliFavorite.next_check_at).label("next_check_at"),
            func.min(case((BiliFavorite.notify_enabled.is_(True), BiliFavorite.check_interval_seconds), else_=None)).label("fastest_interval_seconds"),
        )
        .group_by(BiliFavorite.product_id)
        .subquery()
    )
    rows = db.execute(
        select(BiliProduct, monitor_stats.c.subscribers, monitor_stats.c.enabled_monitors, monitor_stats.c.next_check_at, monitor_stats.c.fastest_interval_seconds)
        .outerjoin(monitor_stats, monitor_stats.c.product_id == BiliProduct.id)
        .order_by(desc(BiliProduct.last_checked_at))
    ).all()
    items = []
    for product, subscribers, enabled_monitors, next_check_at, fastest in rows:
        items.append({"cluster_id": product.cluster_id, "title": product.title, "available": product.available, "current_price": str(product.current_price) if product.current_price is not None else None, "subscribers": subscribers or 0, "enabled_monitors": int(enabled_monitors or 0), "next_check_at": next_check_at.isoformat() if next_check_at else None, "fastest_interval_seconds": fastest, "last_success_at": product.last_success_at.isoformat() if product.last_success_at else None, "last_error": product.last_error})
    return {"items": items}


@router.get("/notifications")
def notifications(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    rows = db.scalars(select(NotificationOutbox).order_by(desc(NotificationOutbox.created_at)).limit(100)).all()
    return {"items": [{"id": row.id, "source": row.source, "status": row.status, "attempts": row.attempts, "next_attempt_at": row.next_attempt_at.isoformat(), "created_at": row.created_at.isoformat(), "sent_at": row.sent_at.isoformat() if row.sent_at else None, "last_error": row.last_error} for row in rows]}


def _admin_notification_content(payload: SiteNotificationCreateRequest) -> tuple[str, str]:
    if payload.template:
        if payload.template == "custom":
            return render_custom_text(payload.title or "", payload.body or "", payload.template_values)
        return render_template(payload.template, payload.template_values)
    return payload.title or "", payload.body or ""


def _site_notification_response(notification: SiteNotification, read_count: int = 0, created_by_admin_username: Optional[str] = None) -> dict[str, Any]:
    return {
        "id": notification.id,
        "target_user_id": notification.target_user_id,
        "kind": notification.kind,
        "severity": notification.severity,
        "title": notification.title,
        "body": notification.body,
        "action_url": notification.action_url,
        "source": notification.source,
        "created_by_admin_username": created_by_admin_username,
        "published_at": notification.published_at.isoformat(),
        "expires_at": notification.expires_at.isoformat() if notification.expires_at else None,
        "created_at": notification.created_at.isoformat(),
        "read_count": read_count,
    }


@router.post("/site-notifications/users/{user_id}")
def send_site_notification(
    user_id: str,
    payload: SiteNotificationCreateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    if db.get(User, user_id) is None:
        raise AppError("USER_NOT_FOUND", "未找到该用户", 404)
    title, body = _admin_notification_content(payload)
    notification, created = create_site_notification(
        db,
        target_user_id=user_id,
        kind=payload.kind,
        severity=payload.severity,
        title=title,
        body=body,
        action_url=payload.action_url,
        source="admin",
        source_key=f"admin:user:{user_id}:{uuid4().hex}",
        expires_at=payload.expires_at,
        created_by_admin_id=admin.id,
    )
    _audit(db, admin, "SEND_USER_NOTIFICATION", "site_notification", notification.id, {"user_id": user_id})
    db.commit()
    return {"created": created, "item": _site_notification_response(notification)}


@router.post("/site-notifications/broadcast")
def broadcast_site_notification(
    payload: SiteNotificationCreateRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
) -> Dict[str, Any]:
    title, body = _admin_notification_content(payload)
    notification, created = create_site_notification(
        db,
        target_user_id=None,
        kind=payload.kind,
        severity=payload.severity,
        title=title,
        body=body,
        action_url=payload.action_url,
        source="admin",
        source_key=f"admin:broadcast:{uuid4().hex}",
        expires_at=payload.expires_at,
        created_by_admin_id=admin.id,
    )
    _audit(db, admin, "BROADCAST_NOTIFICATION", "site_notification", notification.id)
    db.commit()
    return {"created": created, "item": _site_notification_response(notification)}


@router.get("/site-notifications")
def list_site_notifications(
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
    page: int = 1,
    page_size: int = 50,
) -> Dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise AppError("INVALID_PAGINATION", "分页参数无效", 422)
    read_counts = (
        select(NotificationRead.notification_id, func.count().label("read_count"))
        .group_by(NotificationRead.notification_id)
        .subquery()
    )
    creator = aliased(User)
    rows = db.execute(
        select(SiteNotification, func.coalesce(read_counts.c.read_count, 0), creator.username)
        .outerjoin(read_counts, read_counts.c.notification_id == SiteNotification.id)
        .outerjoin(creator, creator.id == SiteNotification.created_by_admin_id)
        .order_by(SiteNotification.created_at.desc(), SiteNotification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    total = db.scalar(select(func.count()).select_from(SiteNotification)) or 0
    return {
        "items": [_site_notification_response(notification, int(read_count or 0), creator_name) for notification, read_count, creator_name in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    }


@router.get("/system-status")
def system_status(db: Session = Depends(get_db), admin: User = Depends(get_current_admin), settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
    heartbeat = db.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.worker_name == "monitor"))
    online = False
    if heartbeat:
        heartbeat_at = heartbeat.last_seen_at.replace(tzinfo=utcnow().tzinfo) if heartbeat.last_seen_at.tzinfo is None else heartbeat.last_seen_at
        online = (utcnow() - heartbeat_at).total_seconds() < 10
    due = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.notify_enabled.is_(True), BiliFavorite.next_check_at <= utcnow())) or 0
    return {
        "version": settings.version_value,
        "database": {"ok": True},
        "smtp": {"configured": settings.smtp_enabled},
        "worker": {"online": online, "last_heartbeat": heartbeat.last_seen_at.isoformat() if heartbeat else None},
        "outbox": {"pending": db.scalar(select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.status == "pending")) or 0, "failed": db.scalar(select(func.count()).select_from(NotificationOutbox).where(NotificationOutbox.status == "failed")) or 0},
        "monitoring": {"due_favorites": due, "price_history_rows": db.scalar(select(func.count()).select_from(BiliPriceHistory)) or 0},
        "global_min_interval_seconds": settings.bili_global_min_interval_seconds,
    }
