from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.admin import bili_products, user_detail, users
from app.models import Base, BiliFavorite, BiliProduct, User, UserUsageDaily


def test_admin_user_list_aggregates_monitoring_fields_without_per_user_queries():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        admin = User(id=str(uuid4()), username="admin-list", username_normalized="admin-list", password_hash="hash", role="admin", created_at=now, updated_at=now)
        user = User(id=str(uuid4()), username="regular-list", username_normalized="regular-list", password_hash="hash", email="regular@example.com", created_at=now, updated_at=now)
        product = BiliProduct(id=str(uuid4()), cluster_id=99001, title="list", detail_url="https://mall.bilibili.com/item", available=True, created_at=now, updated_at=now)
        second_product = BiliProduct(id=str(uuid4()), cluster_id=99002, title="list2", detail_url="https://mall.bilibili.com/item", available=True, created_at=now, updated_at=now)
        db.add_all([
            admin,
            user,
            product,
            second_product,
            BiliFavorite(id=str(uuid4()), user=user, product=product, target_price=Decimal("10.00"), notify_enabled=True, check_interval_seconds=10, next_check_at=now, created_at=now, updated_at=now),
            BiliFavorite(id=str(uuid4()), user=user, product=second_product, target_price=Decimal("10.00"), notify_enabled=False, check_interval_seconds=300, created_at=now, updated_at=now),
            UserUsageDaily(user_id=user.id, usage_date=now.date(), monitor_evaluations=7, price_alerts=2, notifications_created=3),
        ])
        db.commit()

        result = users(db, admin, page=1, page_size=10, q="regular")
        detail = user_detail(user.id, db, admin)
        monitoring = bili_products(db, admin)

    assert result["total"] == 1
    item = result["items"][0]
    assert item["favorite_count"] == 2
    assert item["enabled_monitor_count"] == 1
    assert item["estimated_checks_per_day"] == 8640.0
    assert item["email"] == "r***@example.com"
    assert item["usage_today"]["monitor_evaluations"] == 7
    assert item["usage_today"]["price_alerts"] == 2
    assert item["today_notifications_created"] == 3
    assert detail["usage_today"]["price_alerts"] == 2
    monitored = next(item for item in monitoring["items"] if item["cluster_id"] == 99001)
    assert monitored["enabled_monitors"] == 1
    assert monitored["next_check_at"] is not None
