from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.v1.bili_market import product_history
from app.core.security import utcnow
from app.errors import AppError
from app.models import Base, BiliPriceHistory, BiliPriceHistoryRollup, BiliProduct, User
from app.services.bili_market.history import (
    HistoryPointRecord,
    downsample_history,
    downsample_history_stream,
)


@pytest.fixture
def history_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _history_fixture(db: Session) -> tuple[BiliProduct, User]:
    now = utcnow()
    product = BiliProduct(
        id=str(uuid4()),
        cluster_id=10000002733,
        title="历史测试商品",
        detail_url="https://mall.bilibili.com/item",
        available=True,
        current_price=Decimal("100.00"),
        created_at=now,
        updated_at=now,
    )
    user = User(
        id=str(uuid4()),
        username="history",
        username_normalized="history",
        password_hash="hash",
        created_at=now,
        updated_at=now,
    )
    rows = []
    prices = [Decimal("100.00")] * 5 + [Decimal("80.00")] * 5 + [None] * 5 + [Decimal("90.00")] * 5
    for index, price in enumerate(prices):
        available = price is not None
        rows.append(BiliPriceHistory(
            product=product,
            observed_at=now - timedelta(minutes=(len(prices) - index) * 5),
            available=available,
            current_price=price,
            reference_price=Decimal("120.00"),
        ))
    db.add_all([product, user, *rows])
    db.commit()
    return product, user


def test_history_api_supports_all_ranges_and_returns_sampling_metadata(history_db):
    product, user = _history_fixture(history_db)

    for range_name in ("1h", "6h", "24h", "7d", "30d", "90d"):
        result = product_history(product.cluster_id, range_name, 6, history_db, user)
        assert result["range"] == range_name
        assert 0 < result["total_points"] <= 20
        assert result["returned_points"] == len(result["items"])
        assert 1 <= result["returned_points"] <= 6
        assert result["items"] == sorted(result["items"], key=lambda item: item["observed_at"])


def test_history_api_rejects_invalid_range_and_max_points(history_db):
    product, user = _history_fixture(history_db)

    with pytest.raises(AppError, match="历史范围无效") as invalid_range:
        product_history(product.cluster_id, "2h", 6, history_db, user)
    assert invalid_range.value.status_code == 422

    with pytest.raises(AppError, match="返回点数") as too_few:
        product_history(product.cluster_id, "24h", 1, history_db, user)
    assert too_few.value.status_code == 422

    with pytest.raises(AppError, match="返回点数") as too_many:
        product_history(product.cluster_id, "24h", 1001, history_db, user)
    assert too_many.value.status_code == 422


def test_history_api_merges_rollups_and_reports_physical_points_and_samples(history_db):
    product, user = _history_fixture(history_db)
    now = utcnow()
    history_db.add(BiliPriceHistoryRollup(
        product_id=product.id,
        bucket_start=now - timedelta(days=2, minutes=1),
        bucket_end=now - timedelta(days=2),
        resolution_seconds=60,
        sample_count=6,
        available_count=6,
        last_available=True,
        min_price=Decimal("80.00"),
        max_price=Decimal("90.00"),
        last_price=Decimal("86.50"),
        last_reference_price=Decimal("120.00"),
        created_at=now,
        updated_at=now,
    ))
    history_db.commit()

    result = product_history(product.cluster_id, "7d", 100, history_db, user)

    assert result["total_points"] == 21
    assert result["total_samples"] == 26
    rollup = next(item for item in result["items"] if item["resolution_seconds"] == 60)
    assert rollup["sample_count"] == 6
    assert rollup["min_price"] == "80.00"
    assert rollup["max_price"] == "90.00"
    assert rollup["period_end"] is not None


def test_history_api_preserves_rollup_extreme_when_downsampling(history_db):
    product, user = _history_fixture(history_db)
    now = utcnow()
    history_db.add_all([
        BiliPriceHistoryRollup(
            product_id=product.id,
            bucket_start=now - timedelta(days=2, minutes=1000 - index),
            bucket_end=now - timedelta(days=2, minutes=1000 - index) + timedelta(seconds=59),
            resolution_seconds=60,
            sample_count=6,
            available_count=6,
            last_available=True,
            min_price=Decimal("50.00") if index == 555 else Decimal("100.00"),
            max_price=Decimal("100.00"),
            last_price=Decimal("100.00"),
            last_reference_price=Decimal("120.00"),
            created_at=now,
            updated_at=now,
        )
        for index in range(1000)
    ])
    history_db.commit()

    result = product_history(product.cluster_id, "90d", 10, history_db, user)

    assert result["returned_points"] <= 10
    assert any(item["min_price"] == "50.00" for item in result["items"])


def test_downsample_keeps_endpoints_and_state_price_changes_without_zero_price():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        product, _ = _history_fixture(db)
        rows = db.scalars(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id).order_by(BiliPriceHistory.observed_at)).all()
        sampled = downsample_history(rows, 6)

    assert len(sampled) <= 6
    assert sampled[0].observed_at == rows[0].observed_at
    assert sampled[-1].observed_at == rows[-1].observed_at
    assert any(row.current_price == Decimal("80.00") for row in sampled)
    assert any(not row.available and row.current_price is None for row in sampled)
    assert all(not (not row.available and row.current_price == Decimal("0")) for row in sampled)


def test_stream_downsample_respects_two_point_minimum():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        product, _ = _history_fixture(db)
        rows = db.scalars(select(BiliPriceHistory).where(BiliPriceHistory.product_id == product.id).order_by(BiliPriceHistory.observed_at)).all()
        sampled = downsample_history_stream(iter(rows), len(rows), 2)

    assert len(sampled) == 2
    assert sampled[0].observed_at == rows[0].observed_at
    assert sampled[-1].observed_at == rows[-1].observed_at


def test_stream_downsample_keeps_same_bucket_state_recovery_and_price_dip():
    now = utcnow()
    rows = []
    for index in range(100):
        available = not (index == 10)
        price = Decimal("40.00") if index == 12 else Decimal("100.00")
        rows.append(BiliPriceHistory(
            observed_at=now + timedelta(seconds=index),
            available=available,
            current_price=price if available else None,
        ))
    rows[11].available = True
    rows[11].current_price = Decimal("100.00")

    sampled = downsample_history_stream(iter(rows), len(rows), 10)
    sampled_indices = {int((row.observed_at - now).total_seconds()) for row in sampled}

    assert len(sampled) <= 10
    assert sampled[0].observed_at == rows[0].observed_at
    assert sampled[-1].observed_at == rows[-1].observed_at
    assert 10 in sampled_indices
    assert 11 in sampled_indices
    assert 12 in sampled_indices
    assert sampled == sorted(sampled, key=lambda row: row.observed_at)


def test_stream_downsample_prioritizes_highest_price_change_when_one_slot_remains():
    now = utcnow()
    rows = []
    for index in range(30):
        if index in {2}:
            available, price = False, None
        elif index == 5:
            available, price = True, Decimal("10.00")
        elif index in {6, 7, 8, 9}:
            available, price = True, Decimal("10.00")
        elif index >= 10:
            available, price = True, Decimal("20.00")
        else:
            available, price = True, Decimal("100.00")
        rows.append(BiliPriceHistory(
            observed_at=now + timedelta(seconds=index),
            available=available,
            current_price=price,
        ))
    rows[3].available = True
    rows[3].current_price = Decimal("100.00")

    sampled = downsample_history_stream(iter(rows), len(rows), 5)
    sampled_indices = {int((row.observed_at - now).total_seconds()) for row in sampled}

    assert len(sampled) <= 5
    assert 5 in sampled_indices
    assert 10 not in sampled_indices


def test_stream_downsample_keeps_rollup_interval_extreme():
    now = utcnow()
    rows = [
        HistoryPointRecord(
            observed_at=now + timedelta(minutes=index),
            period_end=now + timedelta(minutes=index, seconds=59),
            available=True,
            current_price=Decimal("100.00"),
            reference_price=Decimal("120.00"),
            resolution_seconds=60,
            sample_count=6,
            min_price=Decimal("50.00") if index == 555 else Decimal("100.00"),
            max_price=Decimal("100.00"),
        )
        for index in range(1000)
    ]

    sampled = downsample_history_stream(iter(rows), len(rows), 10)

    assert len(sampled) <= 10
    assert any(point.min_price == Decimal("50.00") for point in sampled)
