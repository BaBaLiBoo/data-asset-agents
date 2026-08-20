"""创建用户、会话、消息、任务、结果和反馈表。

Revision ID: 20260729_0001
Revises:
Create Date: 2026-07-29
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260729_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "conversation",
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("preferred_scene", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    op.create_index(
        "ix_conversation_user_updated",
        "conversation",
        ["user_id", "updated_at"],
    )
    op.create_table(
        "task",
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("scene", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "original_request_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("current_result_id", sa.String(length=160), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("summary_override", sa.Text(), nullable=True),
        sa.Column("accepted_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.user_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index("ix_task_conversation", "task", ["conversation_id"])
    op.create_table(
        "task_result",
        sa.Column("result_id", sa.String(length=160), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "response_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint(
            "task_id",
            "version",
            name="uq_task_result_version",
        ),
    )
    op.create_index("ix_task_result_task", "task_result", ["task_id"])
    op.create_table(
        "message",
        sa.Column("message_id", sa.String(length=160), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=True),
        sa.Column("result_id", sa.String(length=160), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversation.conversation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence_number",
            name="uq_message_conversation_sequence",
        ),
    )
    op.create_index(
        "ix_message_conversation_sequence",
        "message",
        ["conversation_id", "sequence_number"],
    )
    op.create_table(
        "feedback_event",
        sa.Column("feedback_id", sa.String(length=160), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("result_id", sa.String(length=160), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column(
            "reason_codes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("edited_content", sa.Text(), nullable=True),
        sa.Column("retry", sa.Boolean(), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["result_id"],
            ["task_result.result_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["task.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
        sa.UniqueConstraint("result_id"),
    )


def downgrade() -> None:
    op.drop_table("feedback_event")
    op.drop_index("ix_message_conversation_sequence", table_name="message")
    op.drop_table("message")
    op.drop_index("ix_task_result_task", table_name="task_result")
    op.drop_table("task_result")
    op.drop_index("ix_task_conversation", table_name="task")
    op.drop_table("task")
    op.drop_index("ix_conversation_user_updated", table_name="conversation")
    op.drop_table("conversation")
    op.drop_table("app_user")
