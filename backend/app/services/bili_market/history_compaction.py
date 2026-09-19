from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import case, delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models import BiliPriceHistory, BiliPriceHistoryRollup

RAW_HISTORY_HOURS = 24
ROLLUP_1M_SECONDS = 60
ROLLUP_5M_SECONDS = 300
ROLLUP_30M_SECONDS = 1800
DEFAULT_BATCH_SIZE = 1000


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _bucket_start(value: datetime, resolution_seconds: int) -> datetime:
    value = _aware(value)
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - epoch % resolution_seconds, tz=timezone.utc)


def _raw_resolution(observed_at: datetime, now: datetime) -> int | None:
    age = now - _aware(observed_at)
    if age < timedelta(days=7):
        return ROLLUP_1M_SECONDS
    if age < timedelta(days=30):
        return ROLLUP_5M_SECONDS
    if age < timedelta(days=90):
        return ROLLUP_30M_SECONDS
    return None


@dataclass
class _RollupAggregate:
    product_id: str
    bucket_start: datetime
    resolution_seconds: int
    bucket_end: datetime
    sample_count: int = 0
    available_count: int = 0
    last_available: bool = False
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    last_price: Decimal | None = None
    last_reference_price: Decimal | None = None

    def add(
        self,
        *,
        sample_count: int,
        available_count: int,
        bucket_end: datetime,
        last_available: bool,
        min_price: Decimal | None,
        max_price: Decimal | None,
        last_price: Decimal | None,
        last_reference_price: Decimal | None,
    ) -> None:
        self.sample_count += sample_count
        self.available_count += available_count
        if min_price is not None and (self.min_price is None or min_price < self.min_price):
            self.min_price = min_price
        if max_price is not None and (self.max_price is None or max_price > self.max_price):
            self.max_price = max_price
        bucket_end = _aware(bucket_end)
        if bucket_end >= self.bucket_end:
            self.bucket_end = bucket_end
            self.last_available = last_available
            self.last_price = last_price
            self.last_reference_price = last_reference_price


def _aggregate_key(product_id: str, observed_at: datetime, resolution_seconds: int) -> tuple[str, datetime, int]:
    return product_id, _bucket_start(observed_at, resolution_seconds), resolution_seconds


def _new_aggregate(product_id: str, bucket_start: datetime, resolution_seconds: int, bucket_end: datetime) -> _RollupAggregate:
    return _RollupAggregate(
        product_id=product_id,
        bucket_start=bucket_start,
        resolution_seconds=resolution_seconds,
        bucket_end=_aware(bucket_end),
    )


def _add_raw(aggregates: dict[tuple[str, datetime, int], _RollupAggregate], row: BiliPriceHistory, resolution_seconds: int) -> None:
    key = _aggregate_key(row.product_id, row.observed_at, resolution_seconds)
    aggregate = aggregates.get(key)
    if aggregate is None:
        aggregate = _new_aggregate(row.product_id, key[1], resolution_seconds, row.observed_at)
        aggregates[key] = aggregate
    price = row.current_price if row.available else None
    aggregate.add(
        sample_count=1,
        available_count=int(row.available),
        bucket_end=row.observed_at,
        last_available=row.available,
        min_price=price,
        max_price=price,
        last_price=price,
        last_reference_price=row.reference_price if row.available else None,
    )


def _add_rollup(aggregates: dict[tuple[str, datetime, int], _RollupAggregate], row: BiliPriceHistoryRollup, resolution_seconds: int) -> None:
    key = _aggregate_key(row.product_id, row.bucket_start, resolution_seconds)
    aggregate = aggregates.get(key)
    if aggregate is None:
        aggregate = _new_aggregate(row.product_id, key[1], resolution_seconds, row.bucket_end)
        aggregates[key] = aggregate
    aggregate.add(
        sample_count=row.sample_count,
        available_count=row.available_count,
        bucket_end=row.bucket_end,
        last_available=row.last_available,
        min_price=row.min_price,
        max_price=row.max_price,
        last_price=row.last_price,
        last_reference_price=row.last_reference_price,
    )


def _upsert_aggregates(db: Session, aggregates: Iterable[_RollupAggregate], now: datetime) -> tuple[int, int]:
    values = [
        {
            "product_id": item.product_id,
            "bucket_start": item.bucket_start,
            "bucket_end": item.bucket_end,
            "resolution_seconds": item.resolution_seconds,
            "sample_count": item.sample_count,
            "available_count": item.available_count,
            "last_available": item.last_available,
            "min_price": item.min_price,
            "max_price": item.max_price,
            "last_price": item.last_price,
            "last_reference_price": item.last_reference_price,
            "created_at": now,
            "updated_at": now,
        }
        for item in aggregates
    ]
    if not values:
        return 0, 0
    table = BiliPriceHistoryRollup.__table__
    keys = [(value["product_id"], value["bucket_start"], value["resolution_seconds"]) for value in values]
    existing_keys = {
        tuple(row)
        for row in db.execute(
            select(
                BiliPriceHistoryRollup.product_id,
                BiliPriceHistoryRollup.bucket_start,
                BiliPriceHistoryRollup.resolution_seconds,
            ).where(
                tuple_(
                    BiliPriceHistoryRollup.product_id,
                    BiliPriceHistoryRollup.bucket_start,
                    BiliPriceHistoryRollup.resolution_seconds,
                ).in_(keys)
            )
        ).all()
    }
    created_count = len(values) - len(existing_keys)
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(values)
    elif dialect == "postgresql":
        statement = postgres_insert(table).values(values)
    else:
        for value in values:
            existing = db.scalar(select(BiliPriceHistoryRollup).where(
                BiliPriceHistoryRollup.product_id == value["product_id"],
                BiliPriceHistoryRollup.bucket_start == value["bucket_start"],
                BiliPriceHistoryRollup.resolution_seconds == value["resolution_seconds"],
            ))
            if existing is None:
                db.add(BiliPriceHistoryRollup(**value))
                continue
            existing.sample_count += value["sample_count"]
            existing.available_count += value["available_count"]
            existing.min_price = value["min_price"] if existing.min_price is None else min(existing.min_price, value["min_price"]) if value["min_price"] is not None else existing.min_price
            existing.max_price = value["max_price"] if existing.max_price is None else max(existing.max_price, value["max_price"]) if value["max_price"] is not None else existing.max_price
            if value["bucket_end"] >= existing.bucket_end:
                existing.bucket_end = value["bucket_end"]
                existing.last_available = value["last_available"]
                existing.last_price = value["last_price"]
                existing.last_reference_price = value["last_reference_price"]
            existing.updated_at = now
        db.flush()
        return created_count, len(values)

    excluded = statement.excluded
    table_columns = table.c
    min_price = case(
        (table_columns.min_price.is_(None), excluded.min_price),
        (excluded.min_price.is_(None), table_columns.min_price),
        (excluded.min_price < table_columns.min_price, excluded.min_price),
        else_=table_columns.min_price,
    )
    max_price = case(
        (table_columns.max_price.is_(None), excluded.max_price),
        (excluded.max_price.is_(None), table_columns.max_price),
        (excluded.max_price > table_columns.max_price, excluded.max_price),
        else_=table_columns.max_price,
    )
    latest = excluded.bucket_end >= table_columns.bucket_end
    statement = statement.on_conflict_do_update(
        index_elements=["product_id", "bucket_start", "resolution_seconds"],
        set_={
            "bucket_end": case((latest, excluded.bucket_end), else_=table_columns.bucket_end),
            "sample_count": table_columns.sample_count + excluded.sample_count,
            "available_count": table_columns.available_count + excluded.available_count,
            "last_available": case((latest, excluded.last_available), else_=table_columns.last_available),
            "min_price": min_price,
            "max_price": max_price,
            "last_price": case((latest, excluded.last_price), else_=table_columns.last_price),
            "last_reference_price": case((latest, excluded.last_reference_price), else_=table_columns.last_reference_price),
            "updated_at": now,
        },
    )
    db.execute(statement)
    return created_count, len(values)


def _compact_raw_batch(db: Session, now: datetime, retention_days: int, batch_size: int, stats: dict[str, int]) -> None:
    cutoff = now - timedelta(hours=RAW_HISTORY_HOURS)
    retention = now - timedelta(days=retention_days)
    last_id = 0
    while True:
        rows = db.scalars(select(BiliPriceHistory).where(
            BiliPriceHistory.id > last_id,
            BiliPriceHistory.observed_at < cutoff,
            BiliPriceHistory.observed_at >= retention,
        ).order_by(BiliPriceHistory.id).limit(batch_size)).all()
        if not rows:
            break
        aggregates: dict[tuple[str, datetime, int], _RollupAggregate] = {}
        ids = []
        for row in rows:
            resolution = _raw_resolution(row.observed_at, now)
            if resolution is not None:
                _add_raw(aggregates, row, resolution)
            ids.append(row.id)
        stats["raw_compacted"] += len(rows)
        created, writes = _upsert_aggregates(db, aggregates.values(), now)
        stats["rollup_rows_created"] += created
        stats["rollup_upsert_operations"] += writes
        stats["rollup_rows_upserted"] += writes
        db.execute(delete(BiliPriceHistory).where(BiliPriceHistory.id.in_(ids)))
        stats["raw_deleted"] += len(ids)
        last_id = rows[-1].id


def _compact_rollup_batch(db: Session, now: datetime, retention_days: int, source_resolution: int, target_resolution: int, age: timedelta, batch_size: int, stats: dict[str, int]) -> None:
    cutoff = now - age
    retention = now - timedelta(days=retention_days)
    last_id = 0
    while True:
        rows = db.scalars(select(BiliPriceHistoryRollup).where(
            BiliPriceHistoryRollup.id > last_id,
            BiliPriceHistoryRollup.resolution_seconds == source_resolution,
            BiliPriceHistoryRollup.bucket_end < cutoff,
            BiliPriceHistoryRollup.bucket_end >= retention,
        ).order_by(BiliPriceHistoryRollup.id).limit(batch_size)).all()
        if not rows:
            break
        aggregates: dict[tuple[str, datetime, int], _RollupAggregate] = {}
        ids = [row.id for row in rows]
        for row in rows:
            _add_rollup(aggregates, row, target_resolution)
        stats["rollup_rows_compacted"] += len(rows)
        created, writes = _upsert_aggregates(db, aggregates.values(), now)
        stats["rollup_rows_created"] += created
        stats["rollup_upsert_operations"] += writes
        stats["rollup_rows_upserted"] += writes
        db.execute(delete(BiliPriceHistoryRollup).where(BiliPriceHistoryRollup.id.in_(ids)))
        stats["rollup_rows_deleted"] += len(ids)
        last_id = rows[-1].id


def compact_and_prune_history(
    db: Session,
    now: datetime | None = None,
    retention_days: int = 90,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, Any]:
    """Compact historical observations and delete data past retention atomically."""
    now = _aware(now or utcnow())
    stats: dict[str, Any] = {
        "started_at": now.isoformat(),
        "raw_compacted": 0,
        "raw_deleted": 0,
        "rollup_rows_compacted": 0,
        "rollup_rows_upserted": 0,
        "rollup_rows_created": 0,
        "rollup_upsert_operations": 0,
        "rollup_rows_deleted": 0,
        "expired_raw_deleted": 0,
        "expired_rollup_deleted": 0,
    }
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    retention = now - timedelta(days=retention_days)
    try:
        _compact_raw_batch(db, now, retention_days, batch_size, stats)
        _compact_rollup_batch(db, now, retention_days, ROLLUP_1M_SECONDS, ROLLUP_5M_SECONDS, timedelta(days=7), batch_size, stats)
        _compact_rollup_batch(db, now, retention_days, ROLLUP_5M_SECONDS, ROLLUP_30M_SECONDS, timedelta(days=30), batch_size, stats)
        expired_raw = db.execute(
            delete(BiliPriceHistory).where(BiliPriceHistory.observed_at < retention).execution_options(synchronize_session=False)
        ).rowcount or 0
        expired_rollup = db.execute(
            delete(BiliPriceHistoryRollup).where(BiliPriceHistoryRollup.bucket_end < retention).execution_options(synchronize_session=False)
        ).rowcount or 0
        stats["expired_raw_deleted"] = max(0, expired_raw)
        stats["expired_rollup_deleted"] = max(0, expired_rollup)
        stats["raw_deleted"] += stats["expired_raw_deleted"]
        stats["rollup_rows_deleted"] += stats["expired_rollup_deleted"]
        db.commit()
    except Exception:
        db.rollback()
        raise
    stats["finished_at"] = utcnow().isoformat()
    stats["total_deleted"] = stats["raw_deleted"] + stats["rollup_rows_deleted"]
    stats["total_upserted"] = stats["rollup_rows_upserted"]
    stats["total_created"] = stats["rollup_rows_created"]
    return stats
