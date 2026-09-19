from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models import Base, BiliPriceHistory, BiliPriceHistoryRollup, BiliProduct
from app.services.bili_market.history_compaction import compact_and_prune_history


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_compaction_aggregates_raw_rows_and_deletes_sources_atomically():
    db = _db()
    now = utcnow().replace(microsecond=0)
    product = BiliProduct(
        id=str(uuid4()),
        cluster_id=9001,
        title="rollup",
        detail_url="https://mall.bilibili.com/item",
        available=True,
        created_at=now,
        updated_at=now,
    )
    bucket_time = now - timedelta(hours=25, minutes=1)
    old_rows = [
        BiliPriceHistory(product=product, observed_at=bucket_time, available=True, current_price=Decimal("100.00")),
        BiliPriceHistory(product=product, observed_at=bucket_time, available=True, current_price=Decimal("80.00")),
        BiliPriceHistory(product=product, observed_at=bucket_time, available=False, current_price=None),
    ]
    fresh = BiliPriceHistory(product=product, observed_at=now - timedelta(hours=1), available=True, current_price=Decimal("70.00"))
    expired = BiliPriceHistory(product=product, observed_at=now - timedelta(days=91), available=True, current_price=Decimal("1.00"))
    db.add_all([product, *old_rows, fresh, expired])
    db.commit()

    stats = compact_and_prune_history(db, now=now, retention_days=90, batch_size=2)

    assert stats["raw_deleted"] == 4
    assert stats["rollup_rows_created"] == 1
    assert stats["rollup_upsert_operations"] == 2
    assert db.query(BiliPriceHistory).count() == 1
    rollups = db.scalars(select(BiliPriceHistoryRollup)).all()
    assert len(rollups) == 1
    rollup = rollups[0]
    assert rollup.resolution_seconds == 60
    assert rollup.sample_count == 3
    assert rollup.available_count == 2
    assert rollup.min_price == Decimal("80.00")
    assert rollup.max_price == Decimal("100.00")
    assert rollup.last_available is False
    assert rollup.last_price is None

    second = compact_and_prune_history(db, now=now, retention_days=90)
    assert second["total_upserted"] == 0
    assert second["total_created"] == 0
    assert db.scalar(select(BiliPriceHistoryRollup.sample_count)) == 3


def test_compaction_cascades_fixed_utc_buckets_and_rollup_levels():
    db = _db()
    now = utcnow().replace(microsecond=0)
    product = BiliProduct(
        id=str(uuid4()),
        cluster_id=9002,
        title="levels",
        detail_url="https://mall.bilibili.com/item",
        available=True,
        created_at=now,
        updated_at=now,
    )
    rows = []
    for index in range(2):
        rows.append(
            BiliPriceHistory(
                product=product,
                observed_at=now - timedelta(days=8, minutes=index),
                available=True,
                current_price=Decimal("10.00") + index,
            )
        )
    db.add_all([product, *rows])
    db.commit()

    compact_and_prune_history(db, now=now, retention_days=90)
    five_minute = db.scalars(select(BiliPriceHistoryRollup)).all()
    assert {row.resolution_seconds for row in five_minute} == {300}

    compact_and_prune_history(db, now=now + timedelta(days=24), retention_days=90)
    thirty_minute = db.scalars(select(BiliPriceHistoryRollup)).all()
    assert {row.resolution_seconds for row in thirty_minute} == {1800}
    assert sum(row.sample_count for row in thirty_minute) == 2
