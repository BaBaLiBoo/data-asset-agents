"""PostgreSQL表定义，供仓储和Alembic共同使用。"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB


metadata = MetaData()

app_user = Table(
    "app_user",
    metadata,
    Column("user_id", String(128), primary_key=True),
    Column("display_name", String(256)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

conversation = Table(
    "conversation",
    metadata,
    Column("conversation_id", String(128), primary_key=True),
    Column(
        "user_id",
        String(128),
        ForeignKey("app_user.user_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("title", String(256), nullable=False),
    Column("preferred_scene", String(32), nullable=False),
    Column("deleted_at", DateTime(timezone=True)),
    Column("deleted_by", String(128)),
    Column("delete_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_conversation_user_updated", conversation.c.user_id, conversation.c.updated_at)
Index(
    "ix_conversation_user_deleted_updated",
    conversation.c.user_id,
    conversation.c.deleted_at,
    conversation.c.updated_at,
)

task = Table(
    "task",
    metadata,
    Column("task_id", String(128), primary_key=True),
    Column(
        "conversation_id",
        String(128),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "user_id",
        String(128),
        ForeignKey("app_user.user_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("scene", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("original_request_json", JSONB, nullable=False),
    Column("current_result_id", String(160), nullable=False),
    Column("current_version", Integer, nullable=False),
    Column("summary_override", Text),
    Column("accepted_content", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_task_conversation", task.c.conversation_id)

task_result = Table(
    "task_result",
    metadata,
    Column("result_id", String(160), primary_key=True),
    Column(
        "task_id",
        String(128),
        ForeignKey("task.task_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("response_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("task_id", "version", name="uq_task_result_version"),
)
Index("ix_task_result_task", task_result.c.task_id)

message = Table(
    "message",
    metadata,
    Column("message_id", String(160), primary_key=True),
    Column(
        "conversation_id",
        String(128),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("sequence_number", Integer, nullable=False),
    Column("task_id", String(128), ForeignKey("task.task_id", ondelete="SET NULL")),
    Column("result_id", String(160)),
    Column("metadata_json", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "conversation_id",
        "sequence_number",
        name="uq_message_conversation_sequence",
    ),
)
Index("ix_message_conversation_sequence", message.c.conversation_id, message.c.sequence_number)

feedback_event = Table(
    "feedback_event",
    metadata,
    Column("feedback_id", String(160), primary_key=True),
    Column(
        "task_id",
        String(128),
        ForeignKey("task.task_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "result_id",
        String(160),
        ForeignKey("task_result.result_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("decision", String(32), nullable=False),
    Column("rating", Integer),
    Column("reason_codes_json", JSONB, nullable=False),
    Column("comment", Text),
    Column("edited_content", Text),
    Column("retry", Boolean, nullable=False),
    Column("stored_at", DateTime(timezone=True), nullable=False),
)
