from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def test_monitor_access_migration_preserves_legacy_user_and_favorite_values(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    command.upgrade(config, "0003_history_notifications_usage")
    before = datetime.now(timezone.utc).replace(microsecond=0)
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, username_normalized, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('legacy-user', 'legacy', 'legacy', 'hash', 'user', 1, :created, :updated)"
            ),
            {"created": before, "updated": before},
        )
        connection.execute(
            text(
                "INSERT INTO bili_products (id, cluster_id, title, detail_url, available, created_at, updated_at) "
                "VALUES ('legacy-product', 1, 'legacy product', '/item', 1, :created, :updated)"
            ),
            {"created": before, "updated": before},
        )
        connection.execute(
            text(
                "INSERT INTO bili_favorites (id, user_id, product_id, target_price, notify_enabled, check_interval_seconds, "
                "next_check_at, last_condition_met, created_at, updated_at) "
                "VALUES ('legacy-favorite', 'legacy-user', 'legacy-product', 12.34, 1, 600, :next_check, 1, :created, :updated)"
            ),
            {"next_check": before, "created": before, "updated": before},
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        user = connection.execute(
            text("SELECT monitor_access_status FROM users WHERE id = 'legacy-user'")
        ).scalar_one()
        favorite = connection.execute(
            text(
                "SELECT target_price, notify_enabled, check_interval_seconds, next_check_at "
                "FROM bili_favorites WHERE id = 'legacy-favorite'"
            )
        ).one()
        users_count = connection.execute(text("SELECT count(*) FROM users")).scalar_one()
        favorites_count = connection.execute(text("SELECT count(*) FROM bili_favorites")).scalar_one()

    assert user == "approved"
    assert Decimal(str(favorite.target_price)) == Decimal("12.34")
    assert favorite.notify_enabled == 1
    assert favorite.check_interval_seconds == 600
    assert favorite.next_check_at is not None
    assert users_count == 1
    assert favorites_count == 1
    assert "monitor_access_requests" in inspect(engine).get_table_names()
