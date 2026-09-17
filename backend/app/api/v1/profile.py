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

router = APIRouter(prefix="/profile", tags=["profile"])


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
    now = utcnow()
    hourly_count = db.scalar(select(func.count()).select_from(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id, EmailVerificationChallenge.created_at >= now - timedelta(hours=1))) or 0
    if hourly_count >= 5:
        raise AppError("EMAIL_CODE_RATE_LIMIT", "验证码发送次数已达上限，请稍后再试", 429)
    latest = db.scalar(select(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id).order_by(desc(EmailVerificationChallenge.created_at)).limit(1))
    if latest and (now - (latest.created_at.replace(tzinfo=now.tzinfo) if latest.created_at.tzinfo is None else latest.created_at)).total_seconds() < settings.email_verify_resend_cooldown_seconds:
        raise AppError("EMAIL_CODE_RESEND_COOLDOWN", "验证码发送过于频繁，请稍后再试", 429)
    # New code immediately invalidates prior challenges for this user.
    db.execute(update(EmailVerificationChallenge).where(EmailVerificationChallenge.user_id == user.id, EmailVerificationChallenge.used_at.is_(None)).values(used_at=now))
    code = new_code()
    challenge = EmailVerificationChallenge(
        id=str(uuid4()), user_id=user.id, pending_email=email, code_hash=hash_password(code),
        expires_at=now + timedelta(minutes=settings.email_verify_code_ttl_minutes),
        attempts=0, created_at=now,
    )
    db.add(challenge)
    db.commit()
    subject, text, html = verification_email(settings, email, code)
    Mailer(settings).send(email, subject, text, html)
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
    user.email = challenge.pending_email
    user.email_verified_at = now
    user.updated_at = now
    challenge.used_at = now
    db.query(NotificationOutbox).filter(NotificationOutbox.user_id == user.id, NotificationOutbox.status == "pending").update({"recipient_email": user.email}, synchronize_session=False)
    db.commit()
    return {"message": "邮箱验证成功", "email": user.email, "email_verified": True}
