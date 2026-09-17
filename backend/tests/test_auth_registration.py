import pytest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.auth import register
from app.core.config import Settings
from app.errors import AppError
from app.models import Base, User, UserSession
from app.schemas import RegisterRequest


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_register_creates_user_and_session_cookie(db):
    response = Response()
    result = register(RegisterRequest(username="  CaseUser  ", password="test-password-123"), response, db, Settings())

    assert result["user"]["username"] == "CaseUser"
    assert response.headers["set-cookie"].startswith("session=")
    user = db.scalar(select(User).where(User.username_normalized == "caseuser"))
    assert user is not None
    assert db.scalar(select(UserSession).where(UserSession.user_id == user.id)) is not None


def test_register_rejects_case_insensitive_duplicate(db):
    register(RegisterRequest(username="CaseUser", password="test-password-123"), Response(), db, Settings())

    with pytest.raises(AppError) as error:
        register(RegisterRequest(username="caseuser", password="test-password-456"), Response(), db, Settings())

    assert error.value.code == "USERNAME_ALREADY_EXISTS"
    assert error.value.status_code == 409


def test_register_translates_unique_constraint_race_during_flush(db, monkeypatch):
    def raise_unique_error():
        raise IntegrityError("duplicate username", {}, Exception("unique"))

    monkeypatch.setattr(db, "scalar", lambda statement: None)
    monkeypatch.setattr(db, "flush", raise_unique_error)

    with pytest.raises(AppError) as error:
        register(RegisterRequest(username="racing-user", password="test-password-123"), Response(), db, Settings())

    assert error.value.code == "USERNAME_ALREADY_EXISTS"
    assert error.value.message == "用户名已存在"
    assert error.value.status_code == 409


def test_register_translates_unique_constraint_race_during_commit(db, monkeypatch):
    def raise_unique_error():
        raise IntegrityError("duplicate username", {}, Exception("unique"))

    monkeypatch.setattr(db, "commit", raise_unique_error)

    with pytest.raises(AppError) as error:
        register(RegisterRequest(username="racing-user", password="test-password-123"), Response(), db, Settings())

    assert error.value.code == "USERNAME_ALREADY_EXISTS"
    assert error.value.message == "用户名已存在"
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    ("username", "password", "field"),
    [("ab", "test-password-123", "username"), ("valid-user", "short", "password")],
)
def test_register_schema_rejects_short_fields(username, password, field):
    with pytest.raises(ValidationError) as error:
        RegisterRequest(username=username, password=password)
    assert error.value.errors()[0]["loc"] == (field,)


def test_register_schema_rejects_trimmed_empty_username():
    with pytest.raises(ValidationError):
        RegisterRequest(username="   ", password="test-password-123")
