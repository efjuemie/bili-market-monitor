import pytest

from app.api.deps import get_current_admin
from app.errors import AppError
from app.models import User


def test_non_admin_cannot_use_admin_dependency():
    user = User(
        id="user",
        username="user",
        username_normalized="user",
        password_hash="hash",
        role="user",
        is_active=True,
    )
    with pytest.raises(AppError) as error:
        get_current_admin(user)
    assert error.value.code == "ADMIN_REQUIRED"
