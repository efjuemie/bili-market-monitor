from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.errors import AppError
from app.models import Base, SiteNotification, User
from app.services.notifications import (
    create_site_notification,
    list_visible_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    prune_notifications,
    render_template,
    unread_count,
)


def _db() -> tuple[Session, str, str]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = utcnow()
    first = User(id=str(uuid4()), username="notice-a", username_normalized="notice-a", password_hash="hash", created_at=now, updated_at=now)
    second = User(id=str(uuid4()), username="notice-b", username_normalized="notice-b", password_hash="hash", created_at=now, updated_at=now)
    first_id, second_id = first.id, second.id
    with Session(engine) as db:
        db.add_all([first, second])
        db.commit()
        db.expunge_all()
    return Session(engine), first_id, second_id


def test_notification_visibility_cursor_and_read_state():
    db, first, second = _db()
    now = utcnow()
    create_site_notification(db, target_user_id=None, kind="maintenance", severity="info", title="公告", body="广播", source="test", source_key="broadcast")
    create_site_notification(db, target_user_id=first, kind="admin", severity="important", title="定向", body="只给甲", source="test", source_key="target")
    create_site_notification(db, target_user_id=second, kind="admin", severity="info", title="其他", body="不应见", source="test", source_key="other")
    create_site_notification(db, target_user_id=first, kind="admin", severity="info", title="未来", body="未发布", source="test", source_key="future", published_at=now + timedelta(hours=1))
    db.commit()

    page = list_visible_notifications(db, first, limit=1)
    assert len(page["items"]) == 1
    assert page["has_more"] is True
    assert unread_count(db, first) == 2
    next_page = list_visible_notifications(db, first, limit=1, cursor=page["next_cursor"])
    assert len(next_page["items"]) == 1
    assert {item["title"] for item in page["items"] + next_page["items"]} == {"公告", "定向"}

    mark_notification_read(db, first, next_page["items"][0]["id"])
    db.commit()
    assert unread_count(db, first) == 1
    assert mark_all_notifications_read(db, first) == 1
    db.commit()
    assert unread_count(db, first) == 0


def test_notification_url_plain_text_dedupe_and_templates():
    db, first, _ = _db()
    with pytest.raises(AppError):
        create_site_notification(db, target_user_id=first, kind="admin", severity="info", title="x", body="y", source="test", source_key="bad-url", action_url="https://evil.test")
    with pytest.raises(AppError):
        create_site_notification(db, target_user_id=first, kind="admin", severity="info", title="<b>x</b>", body="y", source="test", source_key="bad-html")
    created, is_new = create_site_notification(db, target_user_id=first, kind="admin", severity="info", title="x", body="y", source="test", source_key="same")
    duplicate, duplicate_new = create_site_notification(db, target_user_id=first, kind="admin", severity="info", title="changed", body="changed", source="test", source_key="same")
    assert is_new is True and duplicate_new is False and duplicate.id == created.id
    title, body = render_template("usage_notice", {"username": "甲", "estimated_checks_per_day": "100"})
    assert title == "关于监控任务的使用建议" and "监控负载" in body
    with pytest.raises(AppError):
        render_template("usage_notice", {"unsafe": "x"})


def test_notification_prune_keeps_future_unpublished_notification_and_reads_are_idempotent():
    db, first, _ = _db()
    now = utcnow()
    old = now - timedelta(days=200)
    future, _ = create_site_notification(
        db,
        target_user_id=first,
        kind="admin",
        severity="info",
        title="未来",
        body="不要提前删除",
        source="test",
        source_key="future-retain",
        published_at=now + timedelta(days=1),
    )
    future.created_at = old
    published, _ = create_site_notification(
        db,
        target_user_id=first,
        kind="admin",
        severity="info",
        title="旧消息",
        body="应清理",
        source="test",
        source_key="old-delete",
        published_at=old,
    )
    future_id = future.id
    published_id = published.id
    db.commit()

    assert prune_notifications(db, now=now, retention_days=180) == 1
    assert db.get(SiteNotification, future_id) is not None
    assert db.get(SiteNotification, published_id) is None

    visible, _ = create_site_notification(
        db,
        target_user_id=first,
        kind="admin",
        severity="info",
        title="已发布",
        body="可读",
        source="test",
        source_key="visible-read",
        published_at=now,
    )
    mark_notification_read(db, first, visible.id, now=now)
    db.commit()
    mark_notification_read(db, first, visible.id, now=now)
    db.commit()
    assert mark_all_notifications_read(db, first, now=now) == 0


def test_notification_cursor_orders_by_published_at_not_creation_time():
    db, first, _ = _db()
    now = utcnow()
    create_site_notification(
        db,
        target_user_id=first,
        kind="admin",
        severity="info",
        title="较早发布时间",
        body="先发布",
        source="test",
        source_key="published-old",
        published_at=now - timedelta(hours=2),
    )
    create_site_notification(
        db,
        target_user_id=first,
        kind="admin",
        severity="info",
        title="较晚发布时间",
        body="后发布",
        source="test",
        source_key="published-new",
        published_at=now - timedelta(hours=1),
    )
    db.commit()

    page = list_visible_notifications(db, first, limit=1)
    next_page = list_visible_notifications(db, first, limit=1, cursor=page["next_cursor"])
    assert page["items"][0]["title"] == "较晚发布时间"
    assert next_page["items"][0]["title"] == "较早发布时间"
