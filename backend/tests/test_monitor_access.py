import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import worker
from app.api.v1.admin import (
    approve_monitor_access,
    broadcast_site_notification,
    reject_monitor_access,
    send_site_notification,
    update_user_favorite_intervals,
)
from app.api.v1.profile import request_monitor_access
from app.core.config import Settings
from app.errors import AppError
from app.models import (
    AdminAuditLog,
    Base,
    BiliFavorite,
    BiliProduct,
    MonitorAccessRequest,
    NotificationOutbox,
    SiteNotification,
    User,
)
from app.schemas import (
    AdminFavoriteIntervalBatchRequest,
    MonitorAccessRejectRequest,
    MonitorAccessRequestCreate,
    SiteNotificationCreateRequest,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(username: str, *, role: str = "user", status: str = "not_requested", email: str | None = None) -> User:
    now = _now()
    return User(
        id=str(uuid4()),
        username=username,
        username_normalized=username,
        password_hash="hash",
        role=role,
        monitor_access_status=status,
        email=email,
        email_verified_at=now if email else None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_monitor_access_request_approval_and_rejection_lifecycle(db):
    admin = _user("access-admin", role="admin", status="approved")
    user = _user("access-user", email="user@example.com")
    db.add_all([admin, user])
    db.commit()

    submitted = request_monitor_access(MonitorAccessRequestCreate(reason="我需要监控几件常购商品"), db, user)
    assert submitted["status"] == "pending"
    assert db.scalar(select(MonitorAccessRequest).where(MonitorAccessRequest.user_id == user.id)) is not None

    with pytest.raises(AppError) as duplicate:
        request_monitor_access(MonitorAccessRequestCreate(reason="再次提交不同的申请理由"), db, user)
    assert duplicate.value.code == "MONITOR_ACCESS_REQUEST_ALREADY_PENDING"

    approved = approve_monitor_access(user.id, db, admin)
    assert approved["monitor_access_status"] == "approved"
    assert db.scalar(select(SiteNotification).where(SiteNotification.target_user_id == user.id)) is not None
    assert db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "APPROVE_MONITOR_ACCESS")) is not None

    with pytest.raises(AppError) as already_approved:
        request_monitor_access(MonitorAccessRequestCreate(reason="审批后再次提交申请理由"), db, user)
    assert already_approved.value.code == "MONITOR_ACCESS_ALREADY_APPROVED"


def test_rejected_access_can_be_resubmitted_and_request_is_reused(db):
    admin = _user("reject-admin", role="admin", status="approved")
    user = _user("reject-user", email="reject@example.com")
    db.add_all([admin, user])
    db.commit()
    request_monitor_access(MonitorAccessRequestCreate(reason="我需要定期查看目标价格变化"), db, user)
    request_id = db.scalar(select(MonitorAccessRequest.id).where(MonitorAccessRequest.user_id == user.id))
    reject_monitor_access(user.id, MonitorAccessRejectRequest(review_note="请补充更具体的使用场景"), db, admin)
    assert user.monitor_access_status == "rejected"
    request_monitor_access(MonitorAccessRequestCreate(reason="补充说明我会只监控少量商品"), db, user)
    request = db.get(MonitorAccessRequest, request_id)
    assert request is not None
    assert request.id == request_id
    assert request.reviewed_at is None
    assert request.review_note is None
    assert user.monitor_access_status == "pending"


def test_request_uses_fresh_locked_user_state(db):
    user = _user("fresh-user", email="fresh@example.com")
    db.add(user)
    db.commit()
    # Keep the caller's ORM identity map stale while another transaction
    # changes the status; the row-lock query must refresh it before deciding.
    assert user.monitor_access_status == "not_requested"
    with Session(db.get_bind()) as other:
        other_user = other.get(User, user.id)
        other_user.monitor_access_status = "approved"
        other.commit()

    with pytest.raises(AppError) as error:
        request_monitor_access(MonitorAccessRequestCreate(reason="这条申请不应覆盖已审批状态"), db, user)
    assert error.value.code == "MONITOR_ACCESS_ALREADY_APPROVED"


def test_batch_interval_update_is_atomic_and_noop_is_quiet(db):
    admin = _user("batch-admin", role="admin", status="approved")
    user = _user("batch-user", status="approved")
    other = _user("other-user", status="approved")
    now = _now()
    product_one = BiliProduct(id=str(uuid4()), cluster_id=1001, title="one", detail_url="/one", created_at=now, updated_at=now)
    product_two = BiliProduct(id=str(uuid4()), cluster_id=1002, title="two", detail_url="/two", created_at=now, updated_at=now)
    own_enabled = BiliFavorite(
        id=str(uuid4()), user=user, product=product_one, target_price=Decimal("10"), notify_enabled=True,
        check_interval_seconds=300, next_check_at=now, created_at=now, updated_at=now,
    )
    own_disabled = BiliFavorite(
        id=str(uuid4()), user=user, product=product_two, notify_enabled=False,
        check_interval_seconds=600, created_at=now, updated_at=now,
    )
    foreign_product = BiliProduct(id=str(uuid4()), cluster_id=1003, title="foreign", detail_url="/foreign", created_at=now, updated_at=now)
    foreign = BiliFavorite(
        id=str(uuid4()), user=other, product=foreign_product, notify_enabled=True,
        check_interval_seconds=300, next_check_at=now, created_at=now, updated_at=now,
    )
    db.add_all([admin, user, other, product_one, product_two, foreign_product, own_enabled, own_disabled, foreign])
    db.commit()

    with pytest.raises(AppError) as invalid:
        update_user_favorite_intervals(
            user.id,
            AdminFavoriteIntervalBatchRequest(favorite_ids=[own_enabled.id, foreign.id], check_interval_seconds=60),
            db,
            admin,
        )
    assert invalid.value.code == "INVALID_FAVORITE_SELECTION"
    db.refresh(own_enabled)
    assert own_enabled.check_interval_seconds == 300

    changed = update_user_favorite_intervals(
        user.id,
        AdminFavoriteIntervalBatchRequest(favorite_ids=[own_enabled.id, own_disabled.id], check_interval_seconds=60),
        db,
        admin,
    )
    assert changed["updated_count"] == 2
    assert own_enabled.next_check_at is not None
    assert own_disabled.next_check_at is None
    assert db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "BATCH_UPDATE_MONITOR_INTERVAL")) is not None

    before_notifications = db.query(SiteNotification).count()
    no_op = update_user_favorite_intervals(
        user.id,
        AdminFavoriteIntervalBatchRequest(favorite_ids=[own_enabled.id], check_interval_seconds=60),
        db,
        admin,
    )
    assert no_op["no_op"] is True
    assert db.query(SiteNotification).count() == before_notifications


def test_admin_notification_email_queue_and_broadcast_filter(db):
    admin = _user("notice-admin", role="admin", status="approved")
    verified = _user("verified", email="verified@example.com")
    unverified = _user("unverified")
    inactive = _user("inactive", email="inactive@example.com")
    inactive.is_active = False
    db.add_all([admin, verified, unverified, inactive])
    db.commit()
    settings = Settings(smtp_host="smtp", smtp_username="user", smtp_password="pass", smtp_from_email="from@example.com")

    targeted = send_site_notification(
        verified.id,
        SiteNotificationCreateRequest(title="标题 & 内容", body="正文 <内容", action_url="/profile", send_email=True),
        db,
        admin,
        settings,
    )
    assert targeted["email_queued_count"] == 1
    row = db.scalar(select(NotificationOutbox).where(NotificationOutbox.source == "admin_notification"))
    assert row is not None
    assert "标题 &amp; 内容" in row.html_body
    assert "&lt;内容" in row.html_body

    broadcast = broadcast_site_notification(
        SiteNotificationCreateRequest(title="广播", body="正文", send_email=True),
        db,
        admin,
        settings,
    )
    assert broadcast["email_queued_count"] == 1
    assert broadcast["email_skipped_count"] == 2
    assert db.query(SiteNotification).filter(SiteNotification.target_user_id.is_(None)).count() == 1


def test_worker_rereads_admin_notification_recipient_after_email_change(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'outbox.db'}")
    Base.metadata.create_all(engine)
    now = _now()
    user = _user("worker-user", status="approved", email="old@example.com")
    with Session(engine, expire_on_commit=False) as db:
        db.add(user)
        db.flush()
        for index in range(2):
            db.add(
                NotificationOutbox(
                    id=str(uuid4()),
                    user_id=user.id,
                    source="admin_notification",
                    dedupe_key=f"admin-notification:test:{index}",
                    recipient_email="old@example.com",
                    subject="通知",
                    text_body="正文",
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
        db.commit()
        sent: list[str] = []

        def fake_send(_mailer, recipient, *_args):
            sent.append(recipient)
            if len(sent) == 1:
                with Session(engine) as other:
                    changed = other.get(User, user.id)
                    changed.email = "new@example.com"
                    changed.updated_at = _now()
                    other.commit()
            return True

        monkeypatch.setattr(worker, "settings", Settings(
            smtp_host="smtp", smtp_username="user", smtp_password="pass", smtp_from_email="from@example.com"
        ))
        monkeypatch.setattr(worker.Mailer, "send", fake_send)
        worker.process_outbox(db)
        assert db.query(NotificationOutbox).filter(NotificationOutbox.status == "sent").count() == 2
    assert sent == ["old@example.com", "new@example.com"]


def test_scheduler_refreshes_locked_favorite_after_upstream_interval_change(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    now = _now()
    user = _user("scheduler-user", status="approved")
    product = BiliProduct(
        id=str(uuid4()), cluster_id=9901, title="scheduler", detail_url="/scheduler",
        available=True, current_price=Decimal("10"), last_success_at=now,
        last_checked_at=now, created_at=now, updated_at=now,
    )
    favorite = BiliFavorite(
        id=str(uuid4()), user=user, product=product, target_price=Decimal("20"),
        notify_enabled=True, check_interval_seconds=60, next_check_at=now,
        created_at=now, updated_at=now,
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([user, product, favorite])
        db.commit()

        class Service:
            async def fetch_and_persist(self, product_db, cluster_id):
                with Session(engine) as other:
                    changed = other.get(BiliFavorite, favorite.id)
                    changed.check_interval_seconds = 3600
                    changed.next_check_at = _now()
                    other.commit()
                return product_db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == cluster_id))

        asyncio.run(worker.run_scheduler_cycle(db, Service()))
        db.refresh(favorite)
        assert favorite.check_interval_seconds == 3600
        assert favorite.next_check_at is not None
        refreshed_next_check = favorite.next_check_at.replace(tzinfo=timezone.utc)
        assert (refreshed_next_check - now).total_seconds() >= 3590


def test_scheduler_does_not_restore_next_check_after_reminder_disabled_during_fetch(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler-disabled.db'}")
    Base.metadata.create_all(engine)
    now = _now()
    user = _user("scheduler-disabled", status="approved")
    product = BiliProduct(
        id=str(uuid4()), cluster_id=9902, title="scheduler disabled", detail_url="/scheduler-disabled",
        available=True, current_price=Decimal("10"), last_success_at=now,
        last_checked_at=now, created_at=now, updated_at=now,
    )
    favorite = BiliFavorite(
        id=str(uuid4()), user=user, product=product, target_price=Decimal("20"),
        notify_enabled=True, check_interval_seconds=60, next_check_at=now,
        created_at=now, updated_at=now,
    )
    with Session(engine, expire_on_commit=False) as db:
        db.add_all([user, product, favorite])
        db.commit()
        old_next_check = favorite.next_check_at

        class Service:
            async def fetch_and_persist(self, product_db, cluster_id):
                with Session(engine) as other:
                    changed = other.get(BiliFavorite, favorite.id)
                    changed.notify_enabled = False
                    other.commit()
                return product_db.scalar(select(BiliProduct).where(BiliProduct.cluster_id == cluster_id))

        asyncio.run(worker.run_scheduler_cycle(db, Service()))
        db.refresh(favorite)
        assert favorite.notify_enabled is False
        assert favorite.next_check_at.replace(tzinfo=timezone.utc) == old_next_check
