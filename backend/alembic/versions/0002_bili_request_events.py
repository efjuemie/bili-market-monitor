"""persist B站 request attempt metrics

Revision ID: 0002_bili_request_events
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa


revision = "0002_bili_request_events"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bili_request_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("cluster_id", sa.BigInteger(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("duration_ms", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_bili_request_events_cluster_id", "bili_request_events", ["cluster_id"])
    op.create_index("ix_bili_request_events_created_at", "bili_request_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_bili_request_events_created_at", table_name="bili_request_events")
    op.drop_index("ix_bili_request_events_cluster_id", table_name="bili_request_events")
    op.drop_table("bili_request_events")
