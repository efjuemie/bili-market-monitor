import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import utcnow
from app.errors import AppError
from app.models import BiliPriceHistory, BiliProduct, BiliRequestEvent
from app.services.bili_market.client import BiliAPIError, BiliMarketClient, BiliRequestAttempt
from app.services.bili_market.parser import BiliProductSnapshot, parse_cluster_info


# A ProductService instance is created per API request, while the Bili client
# intentionally shares in-flight requests. Keep a process-local per-cluster
# lock as well so two callers that await the same upstream request cannot both
# persist the same observation. PostgreSQL's advisory transaction lock remains
# the cross-process coordination mechanism.
class _ProductLockState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_PRODUCT_LOCKS: dict[int, _ProductLockState] = {}


def _product_lock_state(cluster_id: int) -> _ProductLockState:
    state = _PRODUCT_LOCKS.get(cluster_id)
    if state is None:
        state = _ProductLockState()
        _PRODUCT_LOCKS[cluster_id] = state
    state.users += 1
    return state


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


def write_history_observation(db: Session, product: BiliProduct, observed_at: datetime) -> None:
    """Record one observation for one successfully parsed upstream response.

    This intentionally has no change-detection or heartbeat threshold. The
    caller invokes it only after a fresh Bili response has been parsed, so a
    successful same-price check is still meaningful history. Cache hits and
    failure fallbacks never reach this function.
    """
    db.add(BiliPriceHistory(
        product_id=product.id,
        observed_at=observed_at,
        available=product.available,
        current_price=product.current_price if product.available else None,
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
        waited_for_lock = False
        while True:
            acquired = bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:lock_key)"), {"lock_key": -int(cluster_id)}).scalar())
            if acquired:
                if waited_for_lock and allow_cache_reuse:
                    db.expire_all()
                    cached = self.get_product(db, cluster_id)
                    if cached and cached.last_success_at:
                        last_success = _aware(cached.last_success_at)
                        if last_success and (utcnow() - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                            return False, cached
                return True, None
            waited_for_lock = True
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

    async def _fetch_and_persist_locked(self, db: Session, cluster_id: int, force: bool = False) -> BiliProduct:
        now = utcnow()
        product = self.get_product(db, cluster_id)
        if not force and product and product.last_success_at:
            last_success = _aware(product.last_success_at)
            if last_success and (now - last_success).total_seconds() <= self.settings.bili_cache_reuse_seconds:
                return product
        # If this caller had to wait for another caller, a fresh result from
        # that caller is safe to reuse even when this invocation was forced.
        # A caller that acquires the lock immediately still bypasses the cache
        # when force=True.
        locked, cached = await self._try_lock_product(db, cluster_id, allow_cache_reuse=True)
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
        observed_at = utcnow()
        _product_from_snapshot(product, snapshot, observed_at)
        write_history_observation(db, product, observed_at)
        self._record_request_events(db, events)
        db.commit()
        db.refresh(product)
        return product

    async def fetch_and_persist(self, db: Session, cluster_id: int, force: bool = False) -> BiliProduct:
        # The lock also covers the initial cache check. This matters for
        # SQLite/dev deployments where PostgreSQL advisory locks are absent,
        # and prevents duplicate history points for concurrent API callers.
        state = _product_lock_state(cluster_id)
        waited_for_same_product = state.lock.locked()
        try:
            async with state.lock:
                # A forced caller that waited for another same-product caller
                # should reuse that fresh result instead of issuing a second
                # request. If the first request failed, the normal stale-data
                # path will still retry upstream.
                effective_force = force and not waited_for_same_product
                return await self._fetch_and_persist_locked(db, cluster_id, force=effective_force)
        finally:
            state.users -= 1
            if state.users == 0 and _PRODUCT_LOCKS.get(cluster_id) is state:
                _PRODUCT_LOCKS.pop(cluster_id, None)


def validate_cluster_id(cluster_id: int) -> int:
    if cluster_id <= 0 or cluster_id > 9_999_999_999_999:
        raise AppError("INVALID_CLUSTER_ID", "商品 ID 必须是正整数", 422)
    return cluster_id
