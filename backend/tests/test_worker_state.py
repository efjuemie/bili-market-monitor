import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import worker
from app.core.config import Settings
from app.models import (
    Base,
    BiliFavorite,
    BiliProduct,
    NotificationOutbox,
    SiteNotification,
    User,
    UserUsageDaily,
)
from app.worker import run_scheduler_cycle


class FakeService:
    def __init__(self, product):
        self.product = product

    async def fetch_and_persist(self, db, cluster_id):
        return db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == cluster_id))


@pytest.mark.asyncio
async def test_price_alert_state_machine_notifies_once_until_condition_exits():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        user = User(id=str(uuid4()), username="state", username_normalized="state", password_hash="hash", email="state@example.com", email_verified_at=now, created_at=now, updated_at=now)
        product = BiliProduct(id=str(uuid4()), cluster_id=123, title="state", detail_url="https://mall.bilibili.com/item", available=True, current_price=Decimal("100.00"), last_success_at=now, last_checked_at=now, created_at=now, updated_at=now)
        favorite = BiliFavorite(id=str(uuid4()), user=user, product=product, target_price=Decimal("110.00"), notify_enabled=True, check_interval_seconds=10, next_check_at=now - timedelta(seconds=1), created_at=now, updated_at=now)
        db.add_all([user, product, favorite])
        db.commit()
        service = FakeService(product)
        await run_scheduler_cycle(db, service)
        assert db.query(NotificationOutbox).count() == 1
        favorite.next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        await run_scheduler_cycle(db, service)
        assert db.query(NotificationOutbox).count() == 1
        product.available = False
        favorite.next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        await run_scheduler_cycle(db, service)
        assert db.get(BiliFavorite, favorite.id).last_condition_met is False
        product.available = True
        product.current_price = Decimal("99.00")
        product.last_success_at = datetime.now(timezone.utc)
        favorite.next_check_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        await run_scheduler_cycle(db, service)
        assert db.query(NotificationOutbox).count() == 2
        usage = db.get(UserUsageDaily, (user.id, datetime.now(timezone.utc).date()))
        assert usage is not None
        assert usage.monitor_evaluations == 4
        assert usage.price_alerts == 2


class ConcurrentService:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def fetch_and_persist(self, db, cluster_id):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == cluster_id))


@pytest.mark.asyncio
async def test_scheduler_fetches_different_products_with_bounded_concurrency(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        for index in range(3):
            user = User(
                id=str(uuid4()),
                username=f"parallel{index}",
                username_normalized=f"parallel{index}",
                password_hash="hash",
                email=f"parallel{index}@example.com",
                email_verified_at=now,
                created_at=now,
                updated_at=now,
            )
            product = BiliProduct(
                id=str(uuid4()),
                cluster_id=900 + index,
                title=f"parallel {index}",
                detail_url="https://mall.bilibili.com/item",
                available=True,
                current_price=Decimal("100.00"),
                last_success_at=now,
                last_checked_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(BiliFavorite(
                id=str(uuid4()),
                user=user,
                product=product,
                target_price=Decimal("110.00"),
                notify_enabled=True,
                check_interval_seconds=10,
                next_check_at=now - timedelta(seconds=1),
                created_at=now,
                updated_at=now,
            ))
        db.commit()
        monkeypatch.setattr(worker, "settings", Settings(bili_max_concurrency=2))
        service = ConcurrentService()

        await run_scheduler_cycle(db, service)

        assert service.max_active == 2
        assert db.query(NotificationOutbox).count() == 3


@pytest.mark.asyncio
async def test_price_alert_site_notification_is_independent_of_email_verification():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        user = User(id=str(uuid4()), username="unverified", username_normalized="unverified", password_hash="hash", created_at=now, updated_at=now)
        product = BiliProduct(id=str(uuid4()), cluster_id=456, title="unverified", detail_url="https://mall.bilibili.com/item", available=True, current_price=Decimal("50.00"), last_success_at=now, last_checked_at=now, created_at=now, updated_at=now)
        favorite = BiliFavorite(id=str(uuid4()), user=user, product=product, target_price=Decimal("60.00"), notify_enabled=True, check_interval_seconds=10, next_check_at=now - timedelta(seconds=1), created_at=now, updated_at=now)
        db.add_all([user, product, favorite])
        db.commit()

        await run_scheduler_cycle(db, FakeService(product))

        assert db.query(NotificationOutbox).count() == 0
        assert db.query(SiteNotification).count() == 1
