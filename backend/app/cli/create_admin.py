import getpass
from uuid import uuid4

from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.security import hash_password, utcnow
from app.models import User


def main() -> None:
    username = input("管理员用户名: ").strip()
    password = getpass.getpass("管理员密码: ")
    if len(username) < 3 or len(username) > 32 or len(password) < 8:
        raise SystemExit("用户名需为 3-32 个字符，密码至少 8 个字符")
    with SessionLocal() as db:
        normalized = username.lower()
        if db.scalar(select(User).where(User.username_normalized == normalized)):
            raise SystemExit("用户名已存在")
        now = utcnow()
        db.add(User(id=str(uuid4()), username=username, username_normalized=normalized, password_hash=hash_password(password), role="admin", created_at=now, updated_at=now))
        db.commit()
    print("管理员创建成功")


if __name__ == "__main__":
    main()
