from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import worker
from app.core.config import Settings
from app.core.security import utcnow
from app.models import Base, BiliFavorite, BiliProduct, NotificationOutbox, User
from app.services.mailer import Mailer


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _price_alert(db: Session):
    now = utcnow()
    user = User(
        id=str(uuid4()),
        username="outbox",
        username_normalized="outbox",
        password_hash="hash",
        email="outbox@example.com",
        email_verified_at=now,
        created_at=now,
        updated_at=now,
    )
    product = BiliProduct(
        id=str(uuid4()),
        cluster_id=456,
        title="触发时商品",
        detail_url="https://mall.bilibili.com/item",
        available=True,
        current_price=Decimal("100.00"),
        created_at=now,
        updated_at=now,
    )
    favorite = BiliFavorite(
        id=str(uuid4()),
        user=user,
        product=product,
        target_price=Decimal("110.00"),
        notify_enabled=True,
        check_interval_seconds=10,
        created_at=now,
        updated_at=now,
    )
    row = NotificationOutbox(
        id=str(uuid4()),
        user_id=user.id,
        source="price_alert",
        dedupe_key=f"price-alert:{favorite.id}:{now.isoformat()}",
        recipient_email=user.email,
        subject="触发时价格提醒",
        text_body="触发时正文",
        html_body="<p>触发时正文</p>",
        status="pending",
        attempts=0,
        next_attempt_at=now,
        created_at=now,
    )
    db.add_all([user, product, favorite, row])
    db.commit()
    return user, product, favorite, row


def test_price_alert_outbox_sends_immutable_trigger_event(monkeypatch):
    db = _db()
    try:
        _, product, _, row = _price_alert(db)
        settings = Settings(
            smtp_host="smtp.example.com",
            smtp_username="user",
            smtp_password="password",
            smtp_from_email="from@example.com",
        )
        monkeypatch.setattr(worker, "settings", settings)
        monkeypatch.setattr(Mailer, "send", lambda self, *args: True)
        product.available = False
        product.current_price = Decimal("200.00")
        db.commit()

        worker.process_outbox(db)

        db.refresh(row)
        assert row.status == "sent"
        assert row.subject == "触发时价格提醒"
        assert row.text_body == "触发时正文"
    finally:
        db.close()


def test_price_alert_outbox_is_cancelled_when_reminder_is_disabled(monkeypatch):
    db = _db()
    try:
        _, _, favorite, row = _price_alert(db)
        favorite.notify_enabled = False
        db.commit()
        monkeypatch.setattr(worker, "settings", Settings())
        monkeypatch.setattr(Mailer, "send", lambda self, *args: True)

        worker.process_outbox(db)

        db.refresh(row)
        assert row.status == "cancelled"
    finally:
        db.close()


def test_unconfigured_smtp_retries_a_finite_number_of_times(monkeypatch):
    db = _db()
    try:
        now = utcnow()
        row = NotificationOutbox(
            id=str(uuid4()),
            user_id=str(uuid4()),
            source="verification",
            dedupe_key=f"verification:{uuid4()}",
            recipient_email="user@example.com",
            subject="subject",
            text_body="text",
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
        # The row only exercises SMTP state handling; no user relationship is
        # needed for non-price-alert messages.
        db.add(row)
        db.commit()
        monkeypatch.setattr(worker, "settings", Settings(smtp_unconfigured_max_attempts=2))

        worker.process_outbox(db)
        db.refresh(row)
        assert row.status == "pending"
        assert row.attempts == 1
        row.next_attempt_at = utcnow() - timedelta(seconds=1)
        db.commit()

        worker.process_outbox(db)

        db.refresh(row)
        assert row.status == "failed"
        assert row.attempts == 2
    finally:
        db.close()
