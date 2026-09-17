from typing import Optional

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import hash_token, utcnow
from app.errors import AppError
from app.models import User, UserSession


def get_current_user_optional(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Optional[User]:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if not session:
        return None
    now = utcnow()
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if expires_at <= now:
        db.delete(session)
        db.commit()
        return None
    if not session.user.is_active:
        return None
    session.last_seen_at = now
    db.commit()
    return session.user


def get_current_user(user: Optional[User] = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise AppError("AUTH_REQUIRED", "请先登录", 401)
    return user


def get_current_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise AppError("ADMIN_REQUIRED", "需要管理员权限", 403)
    return user
