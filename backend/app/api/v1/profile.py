import logging
import smtplib
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import hash_password, new_code, utcnow, verify_password
from app.errors import AppError
from app.models import EmailVerificationChallenge, NotificationOutbox, User
from app.schemas import EmailCodeRequest, VerifyEmailRequest
from app.services.mailer import Mailer, verification_email
from app.services.notifications import create_site_notification

router = APIRouter(prefix="/profile", tags=["profile"])
logger = logging.getLogger(__name__)

EMAIL_SERVICE_UNAVAILABLE = "验证码暂时无法发送，请稍后重试。"
EMAIL_SEND_FAILED = "验证码发送失败，请稍后重试。"


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


@router.get("")
def profile(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "email_verified": user.email_verified_at is not None,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat(),
    }


@router.post("/email/send-code")
def send_email_code(payload: EmailCodeRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> dict:
    email = str(payload.email).strip().lower()
    if not settings.smtp_enabled:
        raise AppError("EMAIL_SERVICE_UNAVAILABLE", EMAIL_SERVICE_UNAVAILABLE, 503)
    now = utcnow()
    hourly_count = db.scalar(select(func.count()).select_from(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id, EmailVerificationChallenge.created_at >= now - timedelta(hours=1))) or 0
    if hourly_count >= 5:
        raise AppError("EMAIL_CODE_RATE_LIMIT", "验证码发送次数已达上限，请稍后再试", 429)
    latest = db.scalar(select(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id).order_by(desc(EmailVerificationChallenge.created_at)).limit(1))
    if latest and (now - (latest.created_at.replace(tzinfo=now.tzinfo) if latest.created_at.tzinfo is None else latest.created_at)).total_seconds() < settings.email_verify_resend_cooldown_seconds:
        raise AppError("EMAIL_CODE_RESEND_COOLDOWN", "验证码发送过于频繁，请稍后再试", 429)
    # Invalidate prior challenges in a separate transaction. If delivery fails,
    # the old code must stay invalid while the new, unsent challenge rolls back.
    db.execute(update(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id, EmailVerificationChallenge.used_at.is_(None)).values(used_at=now))
    db.commit()
    code = new_code()
    challenge = EmailVerificationChallenge(
        id=str(uuid4()), user_id=user.id, pending_email=email, code_hash=hash_password(code),
        expires_at=now + timedelta(minutes=settings.email_verify_code_ttl_minutes),
        attempts=0, created_at=now,
    )
    db.add(challenge)
    subject, text, html = verification_email(settings, email, code)
    try:
        sent = Mailer(settings).send(email, subject, text, html)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        db.rollback()
        logger.warning(
            "Email verification delivery failed recipient=%s exception=%s",
            _mask_email(email),
            type(exc).__name__,
        )
        raise AppError("EMAIL_SEND_FAILED", EMAIL_SEND_FAILED, 502) from exc
    if not sent:
        db.rollback()
        logger.warning("Email verification delivery skipped recipient=%s reason=mailer_false", _mask_email(email))
        raise AppError("EMAIL_SEND_FAILED", EMAIL_SEND_FAILED, 502)
    db.commit()
    return {"message": "验证码已发送，请检查邮箱"}


@router.post("/email/verify")
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> dict:
    challenge = db.scalar(select(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id, EmailVerificationChallenge.used_at.is_(None)).order_by(desc(EmailVerificationChallenge.created_at)).limit(1))
    now = utcnow()
    if not challenge:
        raise AppError("EMAIL_CODE_INVALID", "验证码无效", 400)
    expires_at = challenge.expires_at.replace(tzinfo=now.tzinfo) if challenge.expires_at.tzinfo is None else challenge.expires_at
    if expires_at <= now:
        # Keep the explicit branch readable and avoid making an expired code look like a mismatch.
        challenge.used_at = now
        db.commit()
        raise AppError("EMAIL_CODE_EXPIRED", "验证码已过期", 400)
    if challenge.attempts >= settings.email_verify_max_attempts:
        raise AppError("EMAIL_CODE_TOO_MANY_ATTEMPTS", "验证码错误次数过多，请重新发送", 429)
    if not verify_password(payload.code, challenge.code_hash):
        challenge.attempts += 1
        if challenge.attempts >= settings.email_verify_max_attempts:
            challenge.used_at = now
        db.commit()
        raise AppError("EMAIL_CODE_INVALID", "验证码无效", 400)
    was_verified = user.email_verified_at is not None
    user.email = challenge.pending_email
    user.email_verified_at = now
    user.updated_at = now
    challenge.used_at = now
    db.query(NotificationOutbox).filter(NotificationOutbox.user_id == user.id, NotificationOutbox.status == "pending").update({"recipient_email": user.email}, synchronize_session=False)
    kind = "email_changed" if was_verified else "email_verified"
    title = "通知邮箱已更换" if was_verified else "通知邮箱绑定成功"
    body = f"新的通知邮箱{_mask_email(user.email)}已验证并生效。" if was_verified else f"{_mask_email(user.email)}已完成验证，低价邮件提醒现在可以正常发送。"
    create_site_notification(
        db,
        target_user_id=user.id,
        kind=kind,
        severity="success",
        title=title,
        body=body,
        action_url="/profile",
        source="profile",
        source_key=f"email-{'changed' if was_verified else 'verified'}:{challenge.id}",
    )
    db.commit()
    return {"message": "邮箱验证成功", "email": user.email, "email_verified": True}
