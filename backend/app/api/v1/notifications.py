from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import User
from app.services.notifications import (
    list_visible_notifications,
    mark_all_notifications_read,
    mark_notification_read,
    unread_count,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def list_notifications(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = list_visible_notifications(db, user.id, limit=limit, cursor=cursor)
    result["unread_count"] = unread_count(db, user.id)
    return result


@router.get("/unread-count")
def get_unread_count(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return {"count": unread_count(db, user.id)}


@router.post("/{notification_id}/read")
def read_notification(notification_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    mark_notification_read(db, user.id, notification_id)
    db.commit()
    return {"ok": True}


@router.post("/read-all")
def read_all_notifications(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    count = mark_all_notifications_read(db, user.id)
    db.commit()
    return {"ok": True, "marked_count": count}
