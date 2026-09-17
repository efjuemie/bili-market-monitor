from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.v1.admin import dashboard
from app.core.config import Settings
from app.core.security import utcnow
from app.models import Base, BiliRequestEvent, User


def test_dashboard_aggregates_persisted_request_events():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = utcnow()
    with Session(engine) as db:
        admin = User(
            id=str(uuid4()),
            username="metrics-admin",
            username_normalized="metrics-admin",
            password_hash="hash",
            role="admin",
            created_at=now,
            updated_at=now,
        )
        db.add_all([
            admin,
            BiliRequestEvent(id=str(uuid4()), cluster_id=1, status_code=200, success=True, duration_ms=Decimal("10.00"), created_at=now),
            BiliRequestEvent(id=str(uuid4()), cluster_id=1, status_code=429, success=False, duration_ms=Decimal("30.00"), created_at=now),
            BiliRequestEvent(id=str(uuid4()), cluster_id=2, status_code=500, success=False, duration_ms=Decimal("20.00"), created_at=now - timedelta(days=2)),
        ])
        db.commit()

        result = dashboard(db, admin, Settings())

        assert result["bili"] == {
            "last_24h_requests": 2,
            "last_24h_429": 1,
            "last_24h_failures": 1,
            "last_24h_success_rate": 50.0,
            "last_24h_average_duration_ms": 20.0,
        }
