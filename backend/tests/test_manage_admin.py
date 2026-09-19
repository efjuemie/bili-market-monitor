from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.cli.manage_admin import _normalize_action, _print_action_menu, update_admin
from app.core.security import verify_password
from app.models import Base, User, UserSession


def test_manage_admin_menu_maps_numeric_actions_and_prints_labels(capsys):
    assert [_normalize_action(str(index)) for index in range(1, 5)] == ["list", "username", "password", "both"]
    assert [_normalize_action(action) for action in ("list", "username", "password", "both")] == [
        "list",
        "username",
        "password",
        "both",
    ]

    _print_action_menu()
    output = capsys.readouterr().out
    assert "1. 查看管理员列表" in output
    assert "2. 修改管理员用户名" in output
    assert "3. 修改管理员密码" in output
    assert "4. 同时修改用户名和密码" in output


def test_update_admin_changes_argon2_credentials_and_clears_sessions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        admin = User(id=str(uuid4()), username="old-admin", username_normalized="old-admin", password_hash="old", role="admin", created_at=now, updated_at=now)
        db.add(admin)
        db.flush()
        db.add(UserSession(id=str(uuid4()), user_id=admin.id, token_hash="a" * 64, expires_at=now, created_at=now, last_seen_at=now))
        db.commit()

        update_admin(db, admin, username="new-admin", password="new-password-123")

        assert admin.username == "new-admin"
        assert admin.username_normalized == "new-admin"
        assert verify_password("new-password-123", admin.password_hash)
        assert db.scalar(select(UserSession)) is None


def test_update_admin_rejects_duplicate_and_short_password():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        first = User(id=str(uuid4()), username="first-admin", username_normalized="first-admin", password_hash="old", role="admin", created_at=now, updated_at=now)
        second = User(id=str(uuid4()), username="second-admin", username_normalized="second-admin", password_hash="old", role="admin", created_at=now, updated_at=now)
        db.add_all([first, second])
        db.commit()
        with pytest.raises(ValueError, match="用户名已存在"):
            update_admin(db, first, username="second-admin")
        with pytest.raises(ValueError, match="密码需为"):
            update_admin(db, first, password="short")
