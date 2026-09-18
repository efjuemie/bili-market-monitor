import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import utcnow
from app.models import Base, BiliPriceHistory, BiliProduct, BiliRequestEvent
from app.services.bili_market.client import BiliAPIError, BiliRequestAttempt
from app.services.bili_market.service import ProductService

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, payload, events=None):
        self.payload = payload
        self.events = events or []

    async def fetch(self, cluster_id):
        return self.payload

    async def fetch_with_events(self, cluster_id):
        return self.payload, self.events


class FailingClient:
    def __init__(self, error):
        self.error = error

    async def fetch_with_events(self, cluster_id):
        raise self.error


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.mark.asyncio
async def test_invalid_bili_business_response_keeps_last_successful_product(db):
    now = utcnow()
    product = BiliProduct(
        id=str(uuid4()),
        cluster_id=10000011281,
        title="已成功保存的商品",
        detail_url="https://mall.bilibili.com/item",
        available=True,
        current_price=Decimal("88.00"),
        last_success_at=now,
        last_checked_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(product)
    db.commit()

    event = BiliRequestAttempt("business-error", product.cluster_id, 200, True, 4.0, now)
    service = ProductService(Settings(), FakeClient(load("error_payload.json"), [event]))
    result = await service.fetch_and_persist(db, product.cluster_id, force=True)

    assert result.available is True
    assert result.current_price == Decimal("88.00")
    assert result.last_success_at is not None
    assert result.last_error
    assert db.scalar(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id)) is None
    request_event = db.query(BiliRequestEvent).one()
    assert request_event.success is False


@pytest.mark.asyncio
async def test_history_records_every_successful_upstream_observation(db):
    settings = Settings()
    client = FakeClient(load("available.json"))
    service = ProductService(settings, client)

    product = await service.fetch_and_persist(db, 10000002733, force=True)
    assert db.scalar(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id)) is not None
    assert db.query(BiliPriceHistory).count() == 1

    await service.fetch_and_persist(db, 10000002733, force=True)
    await service.fetch_and_persist(db, 10000002733, force=True)
    assert db.query(BiliPriceHistory).count() == 3

    failing = ProductService(
        settings,
        FailingClient(BiliAPIError("BILI_API_UNAVAILABLE", "上游暂时不可用", 503)),
    )
    result = await failing.fetch_and_persist(db, 10000002733, force=True)
    assert result.current_price == Decimal("129.00")
    assert db.query(BiliPriceHistory).count() == 3

    client.payload = load("sold_out.json")
    await service.fetch_and_persist(db, 10000002733, force=True)
    assert db.query(BiliPriceHistory).count() == 4
    assert product.available is False
    assert product.current_price is None
    latest = db.scalars(select(BiliPriceHistory).order_by(BiliPriceHistory.observed_at.desc())).first()
    assert latest.available is False
    assert latest.current_price is None


@pytest.mark.asyncio
async def test_cache_hit_does_not_write_history(db):
    service = ProductService(Settings(bili_cache_reuse_seconds=60), FakeClient(load("available.json")))

    product = await service.fetch_and_persist(db, 10000002733, force=True)
    await service.fetch_and_persist(db, 10000002733)

    assert product is not None
    assert db.query(BiliPriceHistory).count() == 1


@pytest.mark.asyncio
async def test_same_product_concurrent_callers_share_one_request_and_history_point(db):
    class CountingClient(FakeClient):
        def __init__(self, payload):
            super().__init__(payload)
            self.calls = 0

        async def fetch_with_events(self, cluster_id):
            self.calls += 1
            await asyncio.sleep(0.01)
            return await super().fetch_with_events(cluster_id)

    client = CountingClient(load("available.json"))
    service = ProductService(Settings(bili_cache_reuse_seconds=60), client)
    await asyncio.gather(
        service.fetch_and_persist(db, 10000002733, force=True),
        service.fetch_and_persist(db, 10000002733, force=True),
    )

    assert client.calls == 1
    assert db.query(BiliPriceHistory).count() == 1


@pytest.mark.asyncio
async def test_product_service_persists_each_http_attempt_event(db):
    now = utcnow()
    events = [
        BiliRequestAttempt("attempt-1", 10000002733, 429, False, 12.3, now),
        BiliRequestAttempt("attempt-2", 10000002733, 200, True, 18.7, now),
    ]
    service = ProductService(Settings(), FakeClient(load("available.json"), events))

    await service.fetch_and_persist(db, 10000002733, force=True)

    rows = db.query(BiliRequestEvent).order_by(BiliRequestEvent.id).all()
    assert [(row.status_code, row.success) for row in rows] == [(429, False), (200, True)]
    assert rows[0].duration_ms == Decimal("12.30")


@pytest.mark.asyncio
async def test_product_service_uses_postgres_try_lock_and_async_backoff(monkeypatch):
    db = Mock()
    db.execute.side_effect = [Mock(scalar=Mock(return_value=False)), Mock(scalar=Mock(return_value=True))]
    db.scalar.return_value = None
    service = ProductService(Settings(database_url="postgresql+psycopg://app:password@localhost/app"), Mock())
    sleep = AsyncMock()
    monkeypatch.setattr("app.services.bili_market.service.asyncio.sleep", sleep)

    locked, cached = await service._try_lock_product(db, 10000002733, allow_cache_reuse=False)

    assert locked is True
    assert cached is None
    assert db.execute.call_count == 2
    statement = db.execute.call_args_list[0].args[0]
    assert "pg_try_advisory_xact_lock" in str(statement)
    assert db.execute.call_args_list[0].args[1] == {"lock_key": -10000002733}
    sleep.assert_awaited_once()
