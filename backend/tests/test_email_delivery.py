import smtplib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.api.v1.auth import forgot_password
from app.api.v1.profile import send_email_code, verify_email
from app.core.config import Settings
from app.core.security import hash_password, hash_token
from app.errors import AppError
from app.models import Base, EmailVerificationChallenge, PasswordResetToken, User
from app.schemas import EmailCodeRequest, ForgotPasswordRequest, VerifyEmailRequest


def _db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _user() -> User:
    now = datetime.now(timezone.utc)
    return User(
        id=str(uuid4()),
        username="mail-user",
        username_normalized="mail-user",
        password_hash=hash_password("password-123"),
        created_at=now,
        updated_at=now,
    )


def _smtp_settings(**overrides) -> Settings:
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_username": "sender@example.com",
        "smtp_password": "not-a-real-secret",
        "smtp_from_email": "sender@example.com",
        "smtp_use_ssl": False,
        "smtp_use_tls": True,
    }
    values.update(overrides)
    return Settings(**values)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/forgot-password",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 8000),
            "scheme": "http",
        }
    )


def test_send_code_unconfigured_returns_service_unavailable_without_challenge():
    db = _db()
    try:
        user = _user()
        db.add(user)
        db.commit()

        with pytest.raises(AppError) as error:
            send_email_code(EmailCodeRequest(email="target@example.com"), db, user, Settings())

        assert error.value.code == "EMAIL_SERVICE_UNAVAILABLE"
        assert error.value.message == "验证码暂时无法发送，请稍后重试。"
        assert db.scalar(select(EmailVerificationChallenge)) is None
    finally:
        db.close()


@pytest.mark.parametrize("failure", [False, smtplib.SMTPException("temporary failure")])
def test_send_code_failure_does_not_persist_challenge(monkeypatch, failure):
    db = _db()
    try:
        user = _user()
        db.add(user)
        old_created_at = datetime.now(timezone.utc) - timedelta(seconds=120)
        old_challenge = EmailVerificationChallenge(
            id=str(uuid4()),
            user_id=user.id,
            pending_email="old@example.com",
            code_hash=hash_password("111111"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            attempts=0,
            created_at=old_created_at,
        )
        db.add(old_challenge)
        db.commit()

        if failure is False:
            monkeypatch.setattr("app.api.v1.profile.Mailer.send", lambda *args: False)
        else:

            def raise_smtp(*args):
                raise failure

            monkeypatch.setattr("app.api.v1.profile.Mailer.send", raise_smtp)

        with pytest.raises(AppError) as error:
            send_email_code(EmailCodeRequest(email="target@example.com"), db, user, _smtp_settings())

        assert error.value.code == "EMAIL_SEND_FAILED"
        assert error.value.message == "验证码发送失败，请稍后重试。"
        db.refresh(old_challenge)
        assert old_challenge.used_at is not None
        assert db.scalar(select(EmailVerificationChallenge).where(EmailVerificationChallenge.id != old_challenge.id)) is None
    finally:
        db.close()


def test_successful_send_keeps_challenge_and_verification_succeeds(monkeypatch):
    db = _db()
    try:
        user = _user()
        db.add(user)
        db.commit()
        sent = {}

        def capture_send(self, recipient, subject, text_body, html_body=None):
            sent["text"] = text_body
            return True

        monkeypatch.setattr("app.api.v1.profile.Mailer.send", capture_send)
        settings = _smtp_settings()

        result = send_email_code(EmailCodeRequest(email="Target@example.com"), db, user, settings)
        assert result == {"message": "验证码已发送，请检查邮箱"}
        challenge = db.scalar(select(EmailVerificationChallenge))
        assert challenge is not None
        code = sent["text"].split("你的验证码是：", 1)[1].split("\n", 1)[0]

        verified = verify_email(VerifyEmailRequest(code=code), db, user, settings)

        assert verified["email"] == "target@example.com"
        assert verified["email_verified"] is True
        assert user.email == "target@example.com"
        assert user.email_verified_at is not None
    finally:
        db.close()


@pytest.mark.parametrize("failure", [False, smtplib.SMTPException("temporary failure")])
def test_forgot_password_send_failure_revokes_old_and_rolls_back_new_token(monkeypatch, failure):
    db = _db()
    try:
        user = _user()
        user.email = "target@example.com"
        user.email_verified_at = datetime.now(timezone.utc)
        db.add(user)
        old_token = PasswordResetToken(
            id=str(uuid4()),
            user_id=user.id,
            token_hash=hash_token("old-reset-token"),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        old_token_id = old_token.id
        db.add(old_token)
        db.commit()
        if failure is False:
            monkeypatch.setattr("app.api.v1.auth.Mailer.send", lambda *args: False)
        else:

            def raise_smtp(*args):
                raise failure

            monkeypatch.setattr("app.api.v1.auth.Mailer.send", raise_smtp)

        result = forgot_password(
            _request(),
            ForgotPasswordRequest(username="mail-user", email="target@example.com"),
            db,
            _smtp_settings(),
        )

        assert result["message"].startswith("如果账号信息匹配")
        assert db.get(PasswordResetToken, old_token_id) is None
        assert db.scalar(select(PasswordResetToken)) is None
    finally:
        db.close()
