"""initial schema

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("users",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("username", sa.String(32), nullable=False),
        sa.Column("username_normalized", sa.String(32), nullable=False), sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email", sa.String(320)), sa.Column("email_verified_at", sa.DateTime(timezone=True)),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("username_normalized"),
    )
    op.create_index("ix_users_username_normalized", "users", ["username_normalized"])
    op.create_table("user_sessions",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_table("email_verification_challenges",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pending_email", sa.String(320), nullable=False), sa.Column("code_hash", sa.Text(), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_email_verification_challenges_user_id", "email_verification_challenges", ["user_id"])
    op.create_table("password_reset_tokens",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_table("bili_products",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("cluster_id", sa.BigInteger(), nullable=False), sa.Column("title", sa.Text(), nullable=False), sa.Column("cover_url", sa.Text()), sa.Column("detail_url", sa.Text(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("current_price", sa.Numeric(12, 2)), sa.Column("reference_price", sa.Numeric(12, 2)), sa.Column("purchase_button_text", sa.Text()), sa.Column("delivery_mode", sa.Text()),
        sa.Column("recent_avg_price", sa.Numeric(12, 2)), sa.Column("recent_deal_price", sa.Numeric(12, 2)), sa.Column("recent_deal_time_text", sa.Text()), sa.Column("last_checked_at", sa.DateTime(timezone=True)), sa.Column("last_success_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text()), sa.Column("last_status_code", sa.Integer()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("cluster_id"),
    )
    op.create_index("ix_bili_products_cluster_id", "bili_products", ["cluster_id"])
    op.create_table("bili_favorites",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("product_id", sa.String(36), sa.ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False), sa.Column("target_price", sa.Numeric(12, 2)), sa.Column("notify_enabled", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("check_interval_seconds", sa.Integer(), nullable=False, server_default="300"), sa.Column("next_check_at", sa.DateTime(timezone=True)), sa.Column("last_evaluated_at", sa.DateTime(timezone=True)), sa.Column("last_condition_met", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("last_alert_price", sa.Numeric(12, 2)), sa.Column("last_alert_at", sa.DateTime(timezone=True)), sa.Column("last_manual_refresh_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("user_id", "product_id", name="uq_bili_favorite_user_product"), sa.CheckConstraint("check_interval_seconds IN (10,30,60,180,300,600,1800,3600)", name="ck_bili_favorite_interval"),
    )
    op.create_index("ix_bili_favorites_user_id", "bili_favorites", ["user_id"])
    op.create_index("ix_bili_favorites_product_id", "bili_favorites", ["product_id"])
    op.create_index("ix_bili_favorites_next_check_at", "bili_favorites", ["next_check_at"])
    op.create_table("bili_price_history", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("product_id", sa.String(36), sa.ForeignKey("bili_products.id", ondelete="CASCADE"), nullable=False), sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False), sa.Column("available", sa.Boolean(), nullable=False), sa.Column("current_price", sa.Numeric(12, 2)), sa.Column("reference_price", sa.Numeric(12, 2)))
    op.create_index("ix_bili_price_history_product_observed", "bili_price_history", ["product_id", "observed_at"])
    op.create_table("notification_outbox", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("source", sa.String(50), nullable=False), sa.Column("dedupe_key", sa.String(255), nullable=False), sa.Column("recipient_email", sa.String(320), nullable=False), sa.Column("subject", sa.Text(), nullable=False), sa.Column("text_body", sa.Text(), nullable=False), sa.Column("html_body", sa.Text()), sa.Column("status", sa.String(20), nullable=False, server_default="pending"), sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"), sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("sent_at", sa.DateTime(timezone=True)), sa.Column("last_error", sa.Text()), sa.UniqueConstraint("dedupe_key"))
    op.create_index("ix_notification_outbox_user_id", "notification_outbox", ["user_id"])
    op.create_index("ix_notification_outbox_status", "notification_outbox", ["status"])
    op.create_table("worker_heartbeats", sa.Column("worker_name", sa.String(100), primary_key=True), sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False), sa.Column("version", sa.String(50), nullable=False), sa.Column("metadata_json", sa.Text()))
    op.create_table("admin_audit_logs", sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True), sa.Column("admin_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("action", sa.String(100), nullable=False), sa.Column("target_type", sa.String(100), nullable=False), sa.Column("target_id", sa.Text(), nullable=False), sa.Column("metadata_json", sa.Text()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade() -> None:
    op.drop_table("admin_audit_logs")
    op.drop_table("worker_heartbeats")
    op.drop_table("notification_outbox")
    op.drop_table("bili_price_history")
    op.drop_table("bili_favorites")
    op.drop_table("bili_products")
    op.drop_table("password_reset_tokens")
    op.drop_table("email_verification_challenges")
    op.drop_table("user_sessions")
    op.drop_table("users")
