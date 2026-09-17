import asyncio
import json
import logging
import time
from datetime import timedelta
from typing import Dict, List, Tuple

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import utcnow
from app.models import (
    BiliFavorite,
    BiliPriceHistory,
    BiliProduct,
    BiliRequestEvent,
    NotificationOutbox,
    User,
    WorkerHeartbeat,
)
from app.services.bili_market.client import BiliMarketClient
from app.services.bili_market.service import ProductService
from app.services.mailer import Mailer, price_alert_email

logger = logging.getLogger(__name__)
settings = get_settings()


def _lock_cycle(db: Session) -> bool:
    if not settings.database_url.startswith("postgresql"):
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_lock(81423701)")).scalar())


def _unlock_cycle(db: Session) -> None:
    if settings.database_url.startswith("postgresql"):
        db.execute(text("SELECT pg_advisory_unlock(81423701)"))


def _heartbeat(db: Session, client: BiliMarketClient, cycle_duration_ms: float | None = None) -> None:
    row = db.get(WorkerHeartbeat, "monitor")
    now = utcnow()
    metrics = {"tick_seconds": 1, **client.metrics()}
    if cycle_duration_ms is not None:
        metrics["last_cycle_duration_ms"] = round(cycle_duration_ms, 2)
    metadata = json.dumps(metrics)
    if row is None:
        row = WorkerHeartbeat(worker_name="monitor", last_seen_at=now, version=settings.version_value, metadata_json=metadata)
        db.add(row)
    else:
        row.last_seen_at = now
        row.version = settings.version_value
        row.metadata_json = metadata
    db.commit()


def _evaluate_favorite(db: Session, favorite: BiliFavorite, product: BiliProduct, now) -> None:
    user = favorite.user
    condition = bool(user.is_active and favorite.notify_enabled and user.email_verified_at and favorite.target_price and product.available and product.current_price is not None and product.current_price <= favorite.target_price)
    if condition and not favorite.last_condition_met:
        checked = product.last_success_at.isoformat() if product.last_success_at else now.isoformat()
        subject, text_body, html_body = price_alert_email(product, f"{favorite.target_price:.2f}", checked)
        db.add(NotificationOutbox(
            id=__import__("uuid").uuid4().hex,
            user_id=user.id,
            source="price_alert",
            dedupe_key=f"price-alert:{favorite.id}:{now.isoformat()}",
            recipient_email=user.email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        ))
        favorite.last_alert_price = product.current_price
        favorite.last_alert_at = now
    favorite.last_condition_met = condition


async def _run_product_group(
    service: ProductService,
    product_id: str,
    cluster_id: int,
    favorite_ids: List[str],
    session_factory,
    semaphore: asyncio.Semaphore,
    now,
) -> None:
    async with semaphore:
        product_db = session_factory()
        try:
            product = await service.fetch_and_persist(product_db, cluster_id)
            favorites = product_db.scalars(select(BiliFavorite).where(BiliFavorite.id.in_(favorite_ids))).all()
            if product.last_error is None:
                for favorite in favorites:
                    _evaluate_favorite(product_db, favorite, product, now)
            for favorite in favorites:
                favorite.last_evaluated_at = now
                effective = max(favorite.check_interval_seconds, settings.bili_global_min_interval_seconds)
                favorite.next_check_at = now + timedelta(seconds=effective)
                favorite.updated_at = now
            product_db.commit()
        except Exception:
            logger.exception("monitor cycle failed product_id=%s", product_id)
            product_db.rollback()
            failed_favorites = product_db.scalars(select(BiliFavorite).where(BiliFavorite.id.in_(favorite_ids))).all()
            for favorite in failed_favorites:
                favorite.last_evaluated_at = now
                effective = max(favorite.check_interval_seconds, settings.bili_global_min_interval_seconds)
                favorite.next_check_at = now + timedelta(seconds=effective)
                favorite.updated_at = now
            product_db.commit()
        finally:
            product_db.close()


async def run_scheduler_cycle(db: Session, service: ProductService) -> int:
    now = utcnow()
    due = db.scalars(select(BiliFavorite).where(BiliFavorite.notify_enabled.is_(True), BiliFavorite.next_check_at <= now).order_by(BiliFavorite.next_check_at).limit(200)).all()
    if not due:
        return 0
    groups: Dict[str, Tuple[int, List[str]]] = {}
    for favorite in due:
        group = groups.setdefault(favorite.product_id, (favorite.product.cluster_id, []))
        group[1].append(favorite.id)

    # Each product task owns an independent Session while the shared HTTP
    # client enforces BILI_MAX_CONCURRENCY and same-cluster in-flight reuse.
    bind = db.get_bind()
    db.rollback()
    session_factory = sessionmaker(bind=bind, autoflush=False, expire_on_commit=False)
    semaphore = asyncio.Semaphore(max(1, settings.bili_max_concurrency))
    tasks = [
        _run_product_group(service, product_id, cluster_id, favorite_ids, session_factory, semaphore, now)
        for product_id, (cluster_id, favorite_ids) in groups.items()
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    db.expire_all()
    return len(due)


def process_outbox(db: Session) -> None:
    now = utcnow()
    rows = db.scalars(select(NotificationOutbox).where(NotificationOutbox.status == "pending", NotificationOutbox.next_attempt_at <= now).order_by(NotificationOutbox.created_at).limit(50)).all()
    mailer = Mailer(settings)
    for row in rows:
        try:
            if row.source == "price_alert":
                favorite_id = row.dedupe_key.split(":", 2)[1] if row.dedupe_key.startswith("price-alert:") else ""
                favorite = db.get(BiliFavorite, favorite_id)
                user = db.get(User, row.user_id)
                if not favorite or favorite.user_id != row.user_id or not user or not user.is_active or not user.email_verified_at or not user.email or not favorite.notify_enabled:
                    row.status = "cancelled"
                    row.last_error = "提醒已关闭或收件人状态已失效"
                    db.commit()
                    continue
                # A price alert is an immutable event: keep the subject/body
                # captured at trigger time even if the product later changes.
                # A verified email change may update only the destination.
                row.recipient_email = user.email
            if not mailer.send(row.recipient_email, row.subject, row.text_body, row.html_body):
                row.last_error = "SMTP_NOT_CONFIGURED"
                row.attempts += 1
                if row.attempts >= settings.smtp_unconfigured_max_attempts:
                    row.status = "failed"
                else:
                    delay = (60, 300, 900)[min(row.attempts - 1, 2)]
                    row.next_attempt_at = now + timedelta(seconds=delay)
                db.commit()
                continue
            row.status = "sent"
            row.sent_at = now
            row.last_error = None
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            if row.attempts >= 3:
                row.status = "failed"
            else:
                row.next_attempt_at = now + timedelta(seconds=(60, 300, 900)[row.attempts - 1])
        db.commit()


def prune_history(db: Session) -> None:
    now = utcnow()
    history_cutoff = now - timedelta(days=settings.bili_price_history_retention_days)
    request_event_cutoff = now - timedelta(days=settings.bili_request_event_retention_days)
    db.query(BiliPriceHistory).filter(BiliPriceHistory.observed_at < history_cutoff).delete(synchronize_session=False)
    db.query(BiliRequestEvent).filter(BiliRequestEvent.created_at < request_event_cutoff).delete(synchronize_session=False)
    db.commit()


async def worker_loop() -> None:
    client = BiliMarketClient(settings)
    await client.start()
    service = ProductService(settings, client)
    last_history_prune = 0.0
    try:
        while True:
            with SessionLocal() as db:
                if _lock_cycle(db):
                    try:
                        _heartbeat(db, client)
                        cycle_started = time.perf_counter()
                        try:
                            await run_scheduler_cycle(db, service)
                            process_outbox(db)
                            if time.monotonic() - last_history_prune >= 3600:
                                prune_history(db)
                                last_history_prune = time.monotonic()
                        except Exception:
                            logger.exception("worker cycle failed; continuing next tick")
                            db.rollback()
                        finally:
                            _heartbeat(db, client, (time.perf_counter() - cycle_started) * 1000)
                    finally:
                        _unlock_cycle(db)
            await asyncio.sleep(1)
    finally:
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(worker_loop())
