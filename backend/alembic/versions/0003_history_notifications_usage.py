"""add history rollups, site notifications, and daily usage metrics

Revision ID: 0003_history_notifications_usage
Revises: 0002_bili_request_events
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_history_notifications_usage"
down_revision = "0002_bili_request_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bili_price_history_rollups",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_seconds", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("available_count", sa.Integer(), nullable=False),
        sa.Column("last_available", sa.Boolean(), nullable=False),
        sa.Column("min_price", sa.Numeric(12, 2)),
        sa.Column("max_price", sa.Numeric(12, 2)),
        sa.Column("last_price", sa.Numeric(12, 2)),
        sa.Column("last_reference_price", sa.Numeric(12, 2)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_id", "bucket_start", "resolution_seconds", name="uq_bili_price_history_rollup_bucket"),
        sa.CheckConstraint("resolution_seconds IN (60,300,1800)", name="ck_bili_price_history_rollup_resolution"),
    )
    op.create_index(
        "ix_bili_price_history_rollup_product_resolution_start",
        "bili_price_history_rollups",
        ["product_id", "resolution_seconds", "bucket_start"],
    )

    op.create_table(
        "site_notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("action_url", sa.Text()),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_key", sa.String(255)),
        sa.Column("created_by_admin_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_key", name="uq_site_notifications_source_key"),
    )
    op.create_index("ix_site_notifications_target_published", "site_notifications", ["target_user_id", "published_at"])
    op.create_index("ix_site_notifications_published_at", "site_notifications", ["published_at"])
    op.create_index("ix_site_notifications_expires_at", "site_notifications", ["expires_at"])

    op.create_table(
        "notification_reads",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("notification_id", sa.String(36), sa.ForeignKey("site_notifications.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "notification_id", name="uq_notification_read_user_notification"),
    )
    op.create_index("ix_notification_reads_user_read_at", "notification_reads", ["user_id", "read_at"])

    op.create_table(
        "user_usage_daily",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("usage_date", sa.Date(), primary_key=True),
        sa.Column("monitor_evaluations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("price_alerts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notifications_created", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "usage_date", name="uq_user_usage_daily_user_date"),
    )
    op.create_index("ix_user_usage_daily_usage_date", "user_usage_daily", ["usage_date"])


def downgrade() -> None:
    op.drop_index("ix_user_usage_daily_usage_date", table_name="user_usage_daily")
    op.drop_table("user_usage_daily")
    op.drop_index("ix_notification_reads_user_read_at", table_name="notification_reads")
    op.drop_table("notification_reads")
    op.drop_index("ix_site_notifications_expires_at", table_name="site_notifications")
    op.drop_index("ix_site_notifications_published_at", table_name="site_notifications")
    op.drop_index("ix_site_notifications_target_published", table_name="site_notifications")
    op.drop_table("site_notifications")
    op.drop_index("ix_bili_price_history_rollup_product_resolution_start", table_name="bili_price_history_rollups")
    op.drop_table("bili_price_history_rollups")
