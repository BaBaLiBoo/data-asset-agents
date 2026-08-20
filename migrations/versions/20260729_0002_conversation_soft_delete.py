"""增加会话逻辑删除字段。

Revision ID: 20260729_0002
Revises: 20260729_0001
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0002"
down_revision: str | None = "20260729_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversation",
        sa.Column("deleted_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "conversation",
        sa.Column("delete_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_conversation_user_deleted_updated",
        "conversation",
        ["user_id", "deleted_at", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_user_deleted_updated",
        table_name="conversation",
    )
    op.drop_column("conversation", "delete_reason")
    op.drop_column("conversation", "deleted_by")
    op.drop_column("conversation", "deleted_at")
