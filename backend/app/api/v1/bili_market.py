from datetime import timedelta
from typing import Any, Dict
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import utcnow
from app.errors import AppError
from app.models import BiliFavorite, BiliPriceHistory, BiliProduct, NotificationOutbox, User
from app.schemas import FavoriteCreateRequest, FavoriteUpdateRequest
from app.services.bili_market.service import (
    ProductService,
    iso,
    money,
    product_response,
    validate_cluster_id,
)

router = APIRouter(prefix="/bili-market", tags=["bili-market"])


def product_service(request: Request, settings: Settings = Depends(get_settings)) -> ProductService:
    return ProductService(settings, request.app.state.bili_client)


def favorite_response(favorite: BiliFavorite) -> Dict[str, Any]:
    return {
        "id": favorite.id,
        "cluster_id": favorite.product.cluster_id,
        "product": product_response(favorite.product),
        "target_price": money(favorite.target_price),
        "notify_enabled": favorite.notify_enabled,
        "check_interval_seconds": favorite.check_interval_seconds,
        "next_check_at": iso(favorite.next_check_at),
        "last_evaluated_at": iso(favorite.last_evaluated_at),
        "last_condition_met": favorite.last_condition_met,
        "last_alert_price": money(favorite.last_alert_price),
        "last_alert_at": iso(favorite.last_alert_at),
        "last_manual_refresh_at": iso(favorite.last_manual_refresh_at),
    }


@router.get("/products/{cluster_id}")
async def get_product(cluster_id: int, db: Session = Depends(get_db), service: ProductService = Depends(product_service)) -> Dict[str, Any]:
    validate_cluster_id(cluster_id)
    product = await service.fetch_and_persist(db, cluster_id)
    return product_response(product)


@router.get("/favorites")
def list_favorites(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Dict[str, Any]:
    favorites = db.scalars(select(BiliFavorite).where(BiliFavorite.user_id == user.id).order_by(BiliFavorite.created_at.desc())).all()
    return {"items": [favorite_response(item) for item in favorites]}


@router.post("/favorites")
async def create_favorite(payload: FavoriteCreateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user), service: ProductService = Depends(product_service)) -> Dict[str, Any]:
    if payload.notify_enabled and user.email_verified_at is None:
        raise AppError("EMAIL_VERIFICATION_REQUIRED", "开启邮件提醒前请先验证邮箱", 400)
    if payload.notify_enabled and payload.target_price is None:
        raise AppError("INVALID_TARGET_PRICE", "开启邮件提醒必须设置目标价格", 422)
    product = await service.fetch_and_persist(db, validate_cluster_id(payload.cluster_id))
    if db.scalar(select(BiliFavorite).where(BiliFavorite.user_id == user.id, BiliFavorite.product_id == product.id)):
        raise AppError("FAVORITE_ALREADY_EXISTS", "该商品已经收藏", 409)
    # Lock the user row on PostgreSQL so concurrent requests cannot bypass the
    # 20-item limit between the count and insert.
    db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
    favorite_count = db.scalar(select(func.count()).select_from(BiliFavorite).where(BiliFavorite.user_id == user.id)) or 0
    if favorite_count >= 20:
        raise AppError("FAVORITE_LIMIT_REACHED", "每个用户最多收藏 20 件商品", 409)
    now = utcnow()
    favorite = BiliFavorite(
        id=str(uuid4()), user_id=user.id, product_id=product.id,
        target_price=payload.target_price, notify_enabled=payload.notify_enabled,
        check_interval_seconds=payload.check_interval_seconds,
        next_check_at=now + timedelta(seconds=payload.check_interval_seconds) if payload.notify_enabled else None,
        last_condition_met=False, created_at=now, updated_at=now,
    )
    db.add(favorite)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError("FAVORITE_ALREADY_EXISTS", "该商品已经收藏", 409) from exc
    db.refresh(favorite)
    return favorite_response(favorite)


@router.patch("/favorites/{cluster_id}")
def update_favorite(cluster_id: int, payload: FavoriteUpdateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    favorite = db.scalar(select(BiliFavorite).join(BiliProduct).where(BiliFavorite.user_id == user.id, BiliProduct.cluster_id == validate_cluster_id(cluster_id)))
    if not favorite:
        raise AppError("FAVORITE_NOT_FOUND", "未找到该收藏", 404)
    if "target_price" in payload.model_fields_set:
        favorite.target_price = payload.target_price
    if payload.notify_enabled is not None:
        target_price = payload.target_price if "target_price" in payload.model_fields_set else favorite.target_price
        if payload.notify_enabled and user.email_verified_at is None:
            raise AppError("EMAIL_VERIFICATION_REQUIRED", "开启邮件提醒前请先验证邮箱", 400)
        if payload.notify_enabled and target_price is None:
            raise AppError("INVALID_TARGET_PRICE", "开启邮件提醒必须设置目标价格", 422)
        favorite.notify_enabled = payload.notify_enabled
    if payload.check_interval_seconds is not None:
        favorite.check_interval_seconds = payload.check_interval_seconds
    if favorite.notify_enabled and favorite.target_price is None:
        raise AppError("INVALID_TARGET_PRICE", "开启邮件提醒必须设置目标价格", 422)
    if payload.notify_enabled is False:
        db.query(NotificationOutbox).filter(NotificationOutbox.status == "pending", NotificationOutbox.dedupe_key.like(f"price-alert:{favorite.id}:%")).update({"status": "cancelled", "last_error": "邮件提醒已关闭"}, synchronize_session=False)
    now = utcnow()
    favorite.next_check_at = now + timedelta(seconds=favorite.check_interval_seconds) if favorite.notify_enabled else None
    favorite.updated_at = now
    db.commit()
    db.refresh(favorite)
    return favorite_response(favorite)


@router.delete("/favorites/{cluster_id}")
def delete_favorite(cluster_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    favorite = db.scalar(select(BiliFavorite).join(BiliProduct).where(BiliFavorite.user_id == user.id, BiliProduct.cluster_id == validate_cluster_id(cluster_id)))
    if not favorite:
        raise AppError("FAVORITE_NOT_FOUND", "未找到该收藏", 404)
    db.execute(delete(NotificationOutbox).where(NotificationOutbox.status == "pending", NotificationOutbox.dedupe_key.like(f"price-alert:{favorite.id}:%")))
    db.delete(favorite)
    db.commit()
    return {"ok": True}


@router.post("/favorites/{cluster_id}/refresh")
async def refresh_favorite(cluster_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user), settings: Settings = Depends(get_settings), service: ProductService = Depends(product_service)) -> Dict[str, Any]:
    cluster_id = validate_cluster_id(cluster_id)
    favorite = db.scalar(select(BiliFavorite).join(BiliProduct).where(BiliFavorite.user_id == user.id, BiliProduct.cluster_id == cluster_id))
    if not favorite:
        raise AppError("FAVORITE_NOT_FOUND", "未找到该收藏", 404)
    now = utcnow()
    if favorite.last_manual_refresh_at:
        last = favorite.last_manual_refresh_at.replace(tzinfo=now.tzinfo) if favorite.last_manual_refresh_at.tzinfo is None else favorite.last_manual_refresh_at
        elapsed = (now - last).total_seconds()
        if elapsed < settings.manual_refresh_cooldown_seconds:
            raise AppError("MANUAL_REFRESH_COOLDOWN", "手动刷新仍在冷却中", 429, {"retry_after_seconds": int(settings.manual_refresh_cooldown_seconds - elapsed) + 1})
    favorite.last_manual_refresh_at = now
    db.commit()
    product = await service.fetch_and_persist(db, cluster_id, force=False)
    return product_response(product)


@router.get("/products/{cluster_id}/history")
def product_history(cluster_id: int, range: str = "7d", db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> Dict[str, Any]:
    product = db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == validate_cluster_id(cluster_id)))
    if not product:
        raise AppError("PRODUCT_NOT_FOUND", "未找到该商品", 404)
    if range not in {"24h", "7d", "30d", "90d"}:
        raise AppError("INVALID_HISTORY_RANGE", "历史范围无效", 422)
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "90d": 24 * 90}[range]
    cutoff = utcnow() - timedelta(hours=hours)
    rows = db.scalars(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id, BiliPriceHistory.observed_at >= cutoff).order_by(BiliPriceHistory.observed_at)).all()
    return {"cluster_id": cluster_id, "range": range, "items": [{"observed_at": iso(row.observed_at), "available": row.available, "current_price": money(row.current_price), "reference_price": money(row.reference_price)} for row in rows]}
