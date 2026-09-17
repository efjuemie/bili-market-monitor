import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import desc, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import utcnow
from app.errors import AppError
from app.models import BiliPriceHistory, BiliProduct, BiliRequestEvent
from app.services.bili_market.client import BiliAPIError, BiliMarketClient, BiliRequestAttempt
from app.services.bili_market.parser import BiliProductSnapshot, parse_cluster_info


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def money(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def iso(value: Optional[datetime]) -> Optional[str]:
    value = _aware(value)
    return value.isoformat() if value else None


def product_response(product: BiliProduct) -> Dict[str, Any]:
    return {
        "cluster_id": product.cluster_id,
        "title": product.title,
        "cover_url": product.cover_url,
        "detail_url": product.detail_url,
        "available": product.available,
        "current_price": money(product.current_price),
        "reference_price": money(product.reference_price),
        "purchase_button_text": product.purchase_button_text,
        "delivery_mode": product.delivery_mode,
        "recent_avg_price": money(product.recent_avg_price),
        "recent_deal_price": money(product.recent_deal_price),
        "recent_deal_time_text": product.recent_deal_time_text,
        "last_checked_at": iso(product.last_checked_at),
        "last_success_at": iso(product.last_success_at),
        "last_error": product.last_error,
    }


def _product_from_snapshot(product: BiliProduct, snapshot: BiliProductSnapshot, now: datetime) -> None:
    product.cluster_id = snapshot.cluster_id
    product.title = snapshot.title
    product.cover_url = snapshot.cover_url
    product.detail_url = snapshot.detail_url
    product.available = snapshot.available
    product.current_price = snapshot.current_price
    product.reference_price = snapshot.reference_price
    product.purchase_button_text = snapshot.purchase_button_text
    product.delivery_mode = snapshot.delivery_mode
    product.recent_avg_price = snapshot.recent_avg_price
    product.recent_deal_price = snapshot.recent_deal_price
    product.recent_deal_time_text = snapshot.recent_deal_time_text
    product.last_checked_at = now
    product.last_success_at = now
    product.last_error = None
    product.last_status_code = None
    product.updated_at = now


def maybe_write_history(db: Session, product: BiliProduct, previous_available: Optional[bool], previous_price: Optional[Decimal], now: datetime) -> None:
    previous = db.scalar(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id).order_by(desc(BiliPriceHistory.observed_at)).limit(1))
    should_write = previous is None
    if previous is not None:
        previous_time = _aware(previous.observed_at) or now
        should_write = (
            previous.available != product.available
            or previous.current_price != product.current_price
            or now - previous_time >= timedelta(minutes=30)
        )
    # previous_available/price are included to make a first update explicit and
    # to keep this helper easy to reason about when callers have a snapshot.
    if previous is not None and previous_available is not None:
        should_write = should_write or previous_available != product.available or previous_price != product.current_price
    if should_write:
        db.add(BiliPriceHistory(
            product_id=product.id,
            observed_at=now,
            available=product.available,
            current_price=product.current_price,
            reference_price=product.reference_price,
        ))


class ProductService:
    def __init__(self, settings: Settings, client: BiliMarketClient):
        self.settings = settings
        self.client = client

    def get_product(self, db: Session, cluster_id: int) -> Optional[BiliProduct]:
        return db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == cluster_id))

    async def _try_lock_product(self, db: Session, cluster_id: int, allow_cache_reuse: bool) -> tuple[bool, Optional[BiliProduct]]:
        # PostgreSQL transaction advisory locks coordinate API and worker
        # processes without adding a lock table. Never block the event loop on
        # a synchronous advisory lock; retry with an async bounded backoff.
        if not self.settings.database_url.startswith("postgresql"):
            return True, None
        deadline = time.monotonic() + self.settings.bili_product_lock_timeout_seconds
        delay = 0.05
        while True:
            acquired = bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": -int(cluster_id)}).scalar())
            if acquired:
                return True, None
            db.rollback()
            db.expire_all()
            cached = self.get_product(db, cluster_id)
            if allow_cache_reuse and cached and cached.last_success_at:
                last_success = _aware(cached.last_success_at)
                if last_success and (utcnow() - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                    return False, cached
            if time.monotonic() + delay >= deadline:
                return False, None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.5)

    @staticmethod
    async def _fetch_payload(client: BiliMarketClient, cluster_id: int) -> tuple[Dict[str, Any], list[BiliRequestAttempt]]:
        fetch_with_events = getattr(client, "fetch_with_events", None)
        if fetch_with_events is None:
            return await client.fetch(cluster_id), []
        return await fetch_with_events(cluster_id)

    @staticmethod
    def _record_request_events(db: Session, events: list[BiliRequestAttempt]) -> None:
        for event in events:
            if event.persisted:
                continue
            db.merge(BiliRequestEvent(
                id=event.id,
                cluster_id=event.cluster_id,
                status_code=event.status_code,
                success=event.success,
                duration_ms=Decimal(str(event.duration_ms)),
                created_at=event.created_at,
            ))
            event.persisted = True

    async def fetch_and_persist(self, db: Session, cluster_id: int, force: bool = False) -> BiliProduct:
        now = utcnow()
        product = self.get_product(db, cluster_id)
        if not force and product and product.last_success_at:
            last_success = _aware(product.last_success_at)
            if last_success and (now - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                return product
        locked, cached = await self._try_lock_product(db, cluster_id, allow_cache_reuse=not force)
        if cached is not None:
            return cached
        if not locked:
            db.expire_all()
            product = self.get_product(db, cluster_id)
            if not force and product and product.last_success_at:
                last_success = _aware(product.last_success_at)
                if last_success and (utcnow() - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                    return product
            raise AppError("BILI_PRODUCT_BUSY", "该商品正在更新，请稍后重试", 503)
        db.expire_all()
        product = self.get_product(db, cluster_id)
        if not force and product and product.last_success_at:
            last_success = _aware(product.last_success_at)
            if last_success and (utcnow() - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                return product
        events: list[BiliRequestAttempt] = []
        try:
            payload, events = await self._fetch_payload(self.client, cluster_id)
            snapshot = parse_cluster_info(payload, cluster_id)
        except BiliAPIError as exc:
            events = exc.events or events
            if product and exc.code != "PRODUCT_NOT_FOUND":
                self._record_request_events(db, events)
                product.last_checked_at = now
                product.last_error = exc.message
                product.last_status_code = exc.status_code
                product.updated_at = now
                db.commit()
                return product
            self._record_request_events(db, events)
            db.commit()
            raise AppError(exc.code, exc.message, 429 if exc.status_code == 429 else 502) from exc
        except AppError as exc:
            for event in events:
                event.success = False
            if product and exc.code != "PRODUCT_NOT_FOUND":
                self._record_request_events(db, events)
                product.last_checked_at = now
                product.last_error = exc.message
                product.updated_at = now
                db.commit()
                return product
            self._record_request_events(db, events)
            db.commit()
            raise
        except Exception as exc:
            for event in events:
                event.success = False
            if product:
                self._record_request_events(db, events)
                product.last_checked_at = now
                product.last_error = "B站响应解析失败"
                product.updated_at = now
                db.commit()
                return product
            self._record_request_events(db, events)
            db.commit()
            raise AppError("BILI_API_INVALID_RESPONSE", "B站接口返回格式无效", 502) from exc

        if product is None:
            product = BiliProduct(
                id=str(uuid4()),
                cluster_id=cluster_id,
                title=snapshot.title,
                detail_url=snapshot.detail_url,
                created_at=now,
                updated_at=now,
            )
            db.add(product)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                product = self.get_product(db, cluster_id)
                if product is None:
                    raise
        previous_available = product.available if product.last_success_at else None
        previous_price = product.current_price if product.last_success_at else None
        _product_from_snapshot(product, snapshot, now)
        maybe_write_history(db, product, previous_available, previous_price, now)
        self._record_request_events(db, events)
        db.commit()
        db.refresh(product)
        return product


def validate_cluster_id(cluster_id: int) -> int:
    if cluster_id <= 0 or cluster_id > 9_999_999_999_999:
        raise AppError("INVALID_CLUSTER_ID", "商品 ID 必须是正整数", 422)
    return cluster_id
