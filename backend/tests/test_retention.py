from datetime import timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import worker
from app.core.config import Settings
from app.core.security import utcnow
from app.models import Base, BiliRequestEvent


def test_prune_history_removes_expired_request_events(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = utcnow()
    with Session(engine) as db:
        old_event = BiliRequestEvent(
            id=str(uuid4()),
            cluster_id=1,
            status_code=429,
            success=False,
            duration_ms=12,
            created_at=now - timedelta(days=8),
        )
        fresh_event = BiliRequestEvent(
            id=str(uuid4()),
            cluster_id=2,
            status_code=200,
            success=True,
            duration_ms=8,
            created_at=now - timedelta(days=1),
        )
        db.add_all([old_event, fresh_event])
        db.commit()
        old_event_id = old_event.id
        fresh_event_id = fresh_event.id
        monkeypatch.setattr(worker, "settings", Settings(bili_request_event_retention_days=7))

        worker.prune_history(db)

        assert db.get(BiliRequestEvent, old_event_id) is None
        assert db.get(BiliRequestEvent, fresh_event_id) is not None
