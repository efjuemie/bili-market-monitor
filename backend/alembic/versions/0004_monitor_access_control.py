"""add monitor access control and application records

Revision ID: 0004_monitor_access_control
Revises: 0003_history_notifications_usage
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_monitor_access_control"
down_revision = "0003_history_notifications_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the column as nullable first so this migration works for databases
    # that already contain users. Existing accounts must retain their access.
    op.add_column(
        "users",
        sa.Column("monitor_access_status", sa.String(20), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE users SET monitor_access_status = 'approved' "
            "WHERE monitor_access_status IS NULL"
        )
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "monitor_access_status",
            existing_type=sa.String(20),
            nullable=False,
            server_default=sa.text("'not_requested'"),
        )
        batch_op.create_check_constraint(
            "ck_users_monitor_access_status",
            "monitor_access_status IN ('approved','not_requested','pending','rejected')",
        )

    op.create_table(
        "monitor_access_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "reviewed_by_admin_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("review_note", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_monitor_access_requests_user"),
    )
    op.create_index("ix_monitor_access_requests_user_id", "monitor_access_requests", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_monitor_access_requests_user_id", table_name="monitor_access_requests")
    op.drop_table("monitor_access_requests")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_monitor_access_status", type_="check")
        batch_op.drop_column("monitor_access_status")
