import getpass
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.security import hash_password, utcnow
from app.models import User, UserSession

_ACTION_MENU = (
    ("1", "查看管理员列表", "list"),
    ("2", "修改管理员用户名", "username"),
    ("3", "修改管理员密码", "password"),
    ("4", "同时修改用户名和密码", "both"),
)
_ACTION_ALIASES = {number: action for number, _, action in _ACTION_MENU}


def _validate_username(value: str) -> str:
    value = value.strip()
    if len(value) < 3 or len(value) > 32:
        raise ValueError("用户名需为 3-32 个字符")
    return value


def _validate_password(value: str) -> str:
    if len(value) < 8 or len(value) > 128:
        raise ValueError("密码需为 8-128 个字符")
    return value


def list_admins(db: Session) -> list[User]:
    return db.scalars(select(User).where(User.role == "admin").order_by(User.username_normalized)).all()


def update_admin(db: Session, admin: User, *, username: Optional[str] = None, password: Optional[str] = None) -> None:
    if username is None and password is None:
        raise ValueError("至少需要修改用户名或密码")
    now = utcnow()
    if username is not None:
        username = _validate_username(username)
        normalized = username.lower()
        conflict = db.scalar(select(User).where(User.username_normalized == normalized, User.id != admin.id))
        if conflict:
            raise ValueError("用户名已存在")
        admin.username = username
        admin.username_normalized = normalized
    if password is not None:
        admin.password_hash = hash_password(_validate_password(password))
    admin.updated_at = now
    db.execute(delete(UserSession).where(UserSession.user_id == admin.id))
    db.commit()


def _select_admin(db: Session) -> User:
    admins = list_admins(db)
    if not admins:
        raise SystemExit("暂无管理员账号")
    for item in admins:
        print(f"{item.id}  {item.username}  {'启用' if item.is_active else '禁用'}")
    identifier = input("请输入管理员用户名或ID: ").strip()
    admin = next((item for item in admins if item.id == identifier or item.username_normalized == identifier.lower()), None)
    if admin is None:
        raise SystemExit("未找到管理员账号")
    return admin


def _normalize_action(value: str) -> str:
    normalized = value.strip().lower()
    return _ACTION_ALIASES.get(normalized, normalized)


def _print_action_menu() -> None:
    print("请选择管理员操作：")
    for number, label, _ in _ACTION_MENU:
        print(f"{number}. {label}")
    print("也可输入 list/username/password/both")


def main() -> None:
    with SessionLocal() as db:
        _print_action_menu()
        action = _normalize_action(input("请选择操作（1-4）: "))
        if action == "list":
            for item in list_admins(db):
                print(f"{item.id}  {item.username}  {'启用' if item.is_active else '禁用'}")
            return
        if action not in {"username", "password", "both"}:
            raise SystemExit("操作必须是 1-4，或 list、username、password、both")
        admin = _select_admin(db)
        username = None
        password = None
        if action in {"username", "both"}:
            username = input("新用户名: ")
        if action in {"password", "both"}:
            password = getpass.getpass("新密码: ")
            confirmation = getpass.getpass("确认新密码: ")
            if password != confirmation:
                raise SystemExit("两次输入的密码不一致")
        try:
            update_admin(db, admin, username=username, password=password)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    print("管理员信息已更新，原有登录会话已清除")


if __name__ == "__main__":
    main()
