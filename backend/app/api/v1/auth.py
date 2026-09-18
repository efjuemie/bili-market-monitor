import logging
import smtplib
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.rate_limit import auth_limiter
from app.core.security import hash_password, hash_token, new_token, utcnow, verify_password
from app.errors import AppError
from app.models import PasswordResetToken, User, UserSession
from app.schemas import ForgotPasswordRequest, LoginRequest, RegisterRequest, ResetPasswordRequest
from app.services.mailer import Mailer, reset_email

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def public_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "email_verified": user.email_verified_at is not None,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_ttl_days * 86400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.post("/register")
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    username = payload.username.strip()
    normalized = username.lower()
    if len(username) < 3:
        raise AppError("INVALID_USERNAME", "用户名至少需要 3 个字符", 422)
    if db.scalar(select(User).where(User.username_normalized == normalized)):
        raise AppError("USERNAME_ALREADY_EXISTS", "用户名已存在", 409)
    now = utcnow()
    user = User(
        id=str(uuid4()), username=username, username_normalized=normalized,
        password_hash=hash_password(payload.password), created_at=now, updated_at=now,
    )
    db.add(user)
    try:
        # The pre-check above is only an optimization; concurrent requests can
        # still race on the database unique constraint.
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError("USERNAME_ALREADY_EXISTS", "用户名已存在", 409) from exc
    token = new_token()
    db.add(UserSession(id=str(uuid4()), user_id=user.id, token_hash=hash_token(token), expires_at=now + timedelta(days=settings.session_ttl_days), created_at=now, last_seen_at=now))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise AppError("USERNAME_ALREADY_EXISTS", "用户名已存在", 409) from exc
    set_session_cookie(response, token, settings)
    return {"user": public_user(user)}


@router.post("/login")
def login(request: Request, payload: LoginRequest, response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    normalized = payload.username.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    limit_key = f"login:{client_ip}:{normalized}"
    if not auth_limiter.allow(limit_key, settings.login_rate_limit_max_attempts, settings.login_rate_limit_window_seconds):
        raise AppError("RATE_LIMITED", "登录尝试过于频繁，请稍后再试", 429)
    user = db.scalar(select(User).where(User.username_normalized == normalized))
    if not user or not verify_password(payload.password, user.password_hash):
        raise AppError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
    if not user.is_active:
        raise AppError("ACCOUNT_DISABLED", "账号已被禁用", 403)
    now = utcnow()
    token = new_token()
    db.add(UserSession(id=str(uuid4()), user_id=user.id, token_hash=hash_token(token), expires_at=now + timedelta(days=settings.session_ttl_days), created_at=now, last_seen_at=now))
    db.commit()
    auth_limiter.reset(limit_key)
    set_session_cookie(response, token, settings)
    return {"user": public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(session_token)))
        db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"user": public_user(user)}


@router.post("/forgot-password")
def forgot_password(request: Request, payload: ForgotPasswordRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    generic = {"message": "如果账号信息匹配，我们会向绑定邮箱发送密码重置邮件。"}
    client_ip = request.client.host if request.client else "unknown"
    limit_key = f"forgot:{client_ip}:{payload.username.strip().lower()}"
    if not auth_limiter.allow(limit_key, settings.forgot_password_rate_limit_max_attempts, settings.forgot_password_rate_limit_window_seconds):
        raise AppError("RATE_LIMITED", "请求过于频繁，请稍后再试", 429)
    user = db.scalar(select(User).where(User.username_normalized == payload.username.strip().lower()))
    if not user or not user.email or not user.email_verified_at or user.email.lower() != str(payload.email).lower():
        return generic
    now = utcnow()
    # Revoke prior reset links in a separate transaction so a failed delivery
    # cannot roll that revocation back and revive an old link.
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)))
    db.commit()
    token = new_token()
    db.add(PasswordResetToken(id=str(uuid4()), user_id=user.id, token_hash=hash_token(token), expires_at=now + timedelta(minutes=settings.password_reset_ttl_minutes), created_at=now))
    link = f"{settings.app_base_url.rstrip('/')}/reset-password?token={token}"
    subject, text, html = reset_email(settings, user.email, link)
    try:
        sent = Mailer(settings).send(user.email, subject, text, html)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        db.rollback()
        logger.warning(
            "Password reset delivery failed recipient=%s exception=%s",
            _mask_email(user.email),
            type(exc).__name__,
        )
        return generic
    if not sent:
        db.rollback()
        logger.warning("Password reset delivery skipped recipient=%s reason=mailer_false", _mask_email(user.email))
        return generic
    db.commit()
    return generic


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict:
    token_record = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(payload.token)))
    now = utcnow()
    if not token_record or token_record.used_at or (token_record.expires_at.replace(tzinfo=now.tzinfo) if token_record.expires_at.tzinfo is None else token_record.expires_at) <= now:
        raise AppError("PASSWORD_RESET_EXPIRED", "密码重置链接已失效", 400)
    user = db.get(User, token_record.user_id)
    if not user:
        raise AppError("PASSWORD_RESET_INVALID", "密码重置链接无效", 400)
    user.password_hash = hash_password(payload.password)
    user.updated_at = now
    token_record.used_at = now
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    db.commit()
    return {"message": "密码已重置，请重新登录"}
