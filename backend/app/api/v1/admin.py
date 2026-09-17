import json
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import utcnow
from app.errors import AppError
from app.models import (
    AdminAuditLog,
    BiliFavorite,
    BiliPriceHistory,
    BiliProduct,
    BiliRequestEvent,
    NotificationOutbox,
    User,
    UserSession,
    WorkerHeartbeat,
)

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
    return {
        "version": settings.version_value,
        "users": {"total": users, "new_today": new_users, "verified_email": verified},
        "monitoring": {"favorites": favorites, "enabled": enabled, "unique_products": products, "due_favorites": due},
        "bili": {"last_24h_requests": request_count, "last_24h_429": failed_429, "last_24h_failures": failed, "last_24h_success_rate": success_rate, "last_24h_average_duration_ms": round(float(average_duration), 2) if average_duration is not None else None},
        "notifications": {"pending": pending, "failed": sending_failed, "sent_last_24h": sent_24h},
        "worker": {"last_heartbeat": heartbeat.last_seen_at.isoformat() if heartbeat else None, "name": heartbeat.worker_name if heartbeat else None, "metrics": json.loads(heartbeat.metadata_json) if heartbeat and heartbeat.metadata_json else {}},
        "database": {"ok": True, "price_history_rows": db.scalar(select(func.count()).select_from(BiliPriceHistory)) or 0},
        "smtp": {"configured": settings.smtp_enabled},
        "global_min_interval_seconds": settings.bili_global_min_interval_seconds,
    }


@router.get("/users")
def users(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    values = db.scalars(select(User).order_by(User.created_at.desc())).all()
    result = []
    for user in values:
        favorite_count = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.user_id == user.id)) or 0
        enabled_count = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.user_id == user.id, BiliFavorite.notify_enabled.is_(True))) or 0
        result.append({
            "id": user.id, "username": user.username, "email": _mask_email(user.email),
            "email_verified": user.email_verified_at is not None, "role": user.role,
            "is_active": user.is_active, "created_at": user.created_at.isoformat(),
            "favorite_count": favorite_count, "enabled_monitor_count": enabled_count,
        })
    return {"items": result}


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
    favorites = db.scalars(select(BiliFavorite).where(BiliFavorite.user_id == user.id).order_by(BiliFavorite.created_at.desc())).all()
    return {
        "id": user.id,
        "username": user.username,
        "email": _mask_email(user.email),
        "email_verified": user.email_verified_at is not None,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
        "favorites": [{"cluster_id": item.product.cluster_id, "title": item.product.title, "notify_enabled": item.notify_enabled, "target_price": str(item.target_price) if item.target_price is not None else None, "check_interval_seconds": item.check_interval_seconds} for item in favorites],
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
    products = db.scalars(select(BiliProduct).order_by(desc(BiliProduct.last_checked_at))).all()
    items = []
    for product in products:
        subscribers = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.product_id == product.id)) or 0
        fastest = db.scalar(select(func.min(BiliFavorite.check_interval_seconds)).where(BiliFavorite.product_id == product.id, BiliFavorite.notify_enabled.is_(True)))
        items.append({"cluster_id": product.cluster_id, "title": product.title, "available": product.available, "current_price": str(product.current_price) if product.current_price is not None else None, "subscribers": subscribers, "fastest_interval_seconds": fastest, "last_success_at": product.last_success_at.isoformat() if product.last_success_at else None, "last_error": product.last_error})
    return {"items": items}


@router.get("/notifications")
def notifications(db: Session = Depends(get_db), admin: User = Depends(get_current_admin)) -> Dict[str, Any]:
    rows = db.scalars(select(NotificationOutbox).order_by(desc(NotificationOutbox.created_at)).limit(100)).all()
    return {"items": [{"id": row.id, "source": row.source, "status": row.status, "attempts": row.attempts, "next_attempt_at": row.next_attempt_at.isoformat(), "created_at": row.created_at.isoformat(), "sent_at": row.sent_at.isoformat() if row.sent_at else None, "last_error": row.last_error} for row in rows]}


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
