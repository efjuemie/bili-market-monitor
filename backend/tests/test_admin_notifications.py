from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.api.v1.admin import (
    broadcast_site_notification,
    list_site_notifications,
    send_site_notification,
)
from app.models import AdminAuditLog, Base, User
from app.schemas import SiteNotificationCreateRequest


def test_admin_site_notifications_record_audit_and_read_counts():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        admin = User(id=str(uuid4()), username="notify-admin", username_normalized="notify-admin", password_hash="hash", role="admin", created_at=now, updated_at=now)
        recipient = User(id=str(uuid4()), username="notify-recipient", username_normalized="notify-recipient", password_hash="hash", role="user", created_at=now, updated_at=now)
        recipient_id = recipient.id
        db.add_all([admin, recipient])
        db.commit()
        targeted = send_site_notification(
            recipient_id,
            SiteNotificationCreateRequest(kind="admin", severity="info", title="标题", body="正文", action_url="/favorites"),
            db,
            admin,
        )
        broadcast = broadcast_site_notification(
            SiteNotificationCreateRequest(template="maintenance", severity="important"),
            db,
            admin,
        )
        result = list_site_notifications(db, admin, page=1, page_size=50)

        actions = {row.action for row in db.scalars(select(AdminAuditLog)).all()}

    assert targeted["item"]["target_user_id"] == recipient_id
    assert broadcast["item"]["target_user_id"] is None
    assert {item["created_by_admin_username"] for item in result["items"]} == {"notify-admin"}
    assert actions == {"SEND_USER_NOTIFICATION", "BROADCAST_NOTIFICATION"}
