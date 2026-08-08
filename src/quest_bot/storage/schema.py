from enum import StrEnum
from typing import TypeVar

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from quest_bot.models import CaptainPosition, ContentType, UserRole

EnumT = TypeVar("EnumT", bound=StrEnum)


class EnumValue(TypeDecorator[EnumT]):
    """Store a ``StrEnum`` by value while exposing enum members to Python."""

    impl = Text
    cache_ok = True

    def __init__(self, enum_type: type[EnumT]) -> None:
        super().__init__()
        self.enum_type = enum_type

    @property
    def python_type(self) -> type[EnumT]:
        return self.enum_type

    def process_bind_param(self, value: EnumT | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum_type(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> EnumT | None:
        return None if value is None else self.enum_type(value)


class BoolInt(TypeDecorator[bool]):
    """A bool stored as SQLite STRICT-compatible INTEGER."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: bool | None, dialect: Dialect) -> int | None:
        return None if value is None else int(value)

    def process_result_value(self, value: int | None, dialect: Dialect) -> bool | None:
        return None if value is None else bool(value)


NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UserRecord(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("length(trim(username)) > 0", name="username_nonempty"),
        CheckConstraint("role IN ('admin', 'captain')", name="role"),
        CheckConstraint("active IN (0, 1)", name="active"),
        CheckConstraint("updated_at_ms >= 0", name="updated_at_ms"),
        {"sqlite_strict": True},
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(EnumValue(UserRole))
    active: Mapped[bool] = mapped_column(BoolInt, default=True, server_default="1")
    updated_at_ms: Mapped[int] = mapped_column(Integer)

    state: Mapped[CaptainStateRecord | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    transitions: Mapped[list[CaptainTransitionRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    attempts: Mapped[list[TaskAttemptRecord]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


Index("users_username_nocase_idx", UserRecord.username.collate("NOCASE"))


class QuestSettingsRecord(Base):
    __tablename__ = "quest_settings"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="singleton"),
        CheckConstraint("time_limit_minutes > 0", name="time_limit_minutes"),
        {"sqlite_strict": True},
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    time_limit_minutes: Mapped[int] = mapped_column(Integer, default=80, server_default="80")


class ScoreStepRecord(Base):
    __tablename__ = "score_steps"
    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="attempt_number"),
        CheckConstraint("points >= 0", name="points"),
        {"sqlite_strict": True},
    )

    attempt_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    points: Mapped[int] = mapped_column(Integer)


class GlobalContentPartRecord(Base):
    __tablename__ = "global_content_parts"
    __table_args__ = (
        CheckConstraint("content_kind IN ('intro', 'success', 'timeout')", name="content_kind"),
        CheckConstraint("part_number > 0", name="part_number"),
        CheckConstraint(
            "content_type IN "
            "('text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note')",
            name="content_type",
        ),
        CheckConstraint("length(data) > 0", name="data_nonempty"),
        {"sqlite_strict": True, "sqlite_with_rowid": False},
    )

    content_kind: Mapped[str] = mapped_column(Text, primary_key=True)
    part_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[ContentType] = mapped_column(EnumValue(ContentType))
    data: Mapped[str] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)


class StageRecord(Base):
    __tablename__ = "stages"
    __table_args__ = (
        CheckConstraint("stage_number > 0", name="stage_number"),
        CheckConstraint("length(trim(name)) > 0", name="name_nonempty"),
        {"sqlite_strict": True},
    )

    stage_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)

    tasks: Mapped[list[TaskRecord]] = relationship(
        back_populates="stage",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskRecord.task_number",
    )


class TaskRecord(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("stage_number > 0", name="stage_number"),
        CheckConstraint("task_number > 0", name="task_number"),
        CheckConstraint("length(trim(correct_answer_raw)) > 0", name="answer_raw_nonempty"),
        CheckConstraint("length(correct_answer_normalized) > 0", name="answer_normalized_nonempty"),
        {"sqlite_strict": True, "sqlite_with_rowid": False},
    )

    stage_number: Mapped[int] = mapped_column(
        Integer, ForeignKey("stages.stage_number", ondelete="CASCADE"), primary_key=True
    )
    task_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    correct_answer_raw: Mapped[str] = mapped_column(Text)
    correct_answer_normalized: Mapped[str] = mapped_column(Text)

    stage: Mapped[StageRecord] = relationship(back_populates="tasks")
    prompt_parts: Mapped[list[TaskPromptPartRecord]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskPromptPartRecord.part_number",
    )


class TaskPromptPartRecord(Base):
    __tablename__ = "task_prompt_parts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["stage_number", "task_number"],
            ["tasks.stage_number", "tasks.task_number"],
            ondelete="CASCADE",
        ),
        CheckConstraint("stage_number > 0", name="stage_number"),
        CheckConstraint("task_number > 0", name="task_number"),
        CheckConstraint("part_number > 0", name="part_number"),
        CheckConstraint(
            "content_type IN "
            "('text', 'photo', 'sticker', 'voice', 'document', 'video', 'video_note')",
            name="content_type",
        ),
        CheckConstraint("length(data) > 0", name="data_nonempty"),
        {"sqlite_strict": True, "sqlite_with_rowid": False},
    )

    stage_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    part_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_type: Mapped[ContentType] = mapped_column(EnumValue(ContentType))
    data: Mapped[str] = mapped_column(Text)
    caption: Mapped[str | None] = mapped_column(Text)

    task: Mapped[TaskRecord] = relationship(back_populates="prompt_parts")


class CaptainStateRecord(Base):
    __tablename__ = "captain_state"
    __table_args__ = (
        CheckConstraint(
            "position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')",
            name="position",
        ),
        CheckConstraint("started_at_ms IS NULL OR started_at_ms >= 0", name="started_at_ms"),
        CheckConstraint(
            "current_stage_number IS NULL OR current_stage_number > 0",
            name="current_stage_number",
        ),
        CheckConstraint(
            "(position = 'not_started' AND started_at_ms IS NULL "
            "AND current_stage_number IS NULL) OR "
            "(position = 'intro' AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NULL) OR "
            "(position = 'stage' AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NOT NULL) OR "
            "(position IN ('finished', 'timed_out') AND started_at_ms IS NOT NULL "
            "AND current_stage_number IS NULL)",
            name="position_fields",
        ),
        {"sqlite_strict": True},
    )

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[CaptainPosition] = mapped_column(
        EnumValue(CaptainPosition), default=CaptainPosition.NOT_STARTED
    )
    started_at_ms: Mapped[int | None] = mapped_column(Integer)
    current_stage_number: Mapped[int | None] = mapped_column(Integer)

    user: Mapped[UserRecord] = relationship(back_populates="state")


Index(
    "captain_state_position_idx",
    CaptainStateRecord.position,
    CaptainStateRecord.started_at_ms,
)


class CaptainTransitionRecord(Base):
    __tablename__ = "captain_transitions"
    __table_args__ = (
        UniqueConstraint("user_id", "sequence_number", name="uq_captain_transitions_sequence"),
        UniqueConstraint("source_update_id", name="uq_captain_transitions_source_update_id"),
        CheckConstraint("sequence_number > 0", name="sequence_number"),
        CheckConstraint(
            "from_position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')",
            name="from_position",
        ),
        CheckConstraint(
            "from_stage_number IS NULL OR from_stage_number > 0", name="from_stage_number"
        ),
        CheckConstraint(
            "to_position IN ('not_started', 'intro', 'stage', 'finished', 'timed_out')",
            name="to_position",
        ),
        CheckConstraint("to_stage_number IS NULL OR to_stage_number > 0", name="to_stage_number"),
        CheckConstraint("event_at_ms >= 0", name="event_at_ms"),
        CheckConstraint("recorded_at_ms >= 0", name="recorded_at_ms"),
        CheckConstraint("skipped_unsolved_tasks IN (0, 1)", name="skipped_unsolved_tasks"),
        CheckConstraint(
            "(from_position = 'stage' AND from_stage_number IS NOT NULL) OR "
            "(from_position <> 'stage' AND from_stage_number IS NULL)",
            name="from_position_stage",
        ),
        CheckConstraint(
            "(to_position = 'stage' AND to_stage_number IS NOT NULL) OR "
            "(to_position <> 'stage' AND to_stage_number IS NULL)",
            name="to_position_stage",
        ),
        {"sqlite_strict": True},
    )

    transition_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))
    sequence_number: Mapped[int] = mapped_column(Integer)
    from_position: Mapped[CaptainPosition] = mapped_column(EnumValue(CaptainPosition))
    from_stage_number: Mapped[int | None] = mapped_column(Integer)
    to_position: Mapped[CaptainPosition] = mapped_column(EnumValue(CaptainPosition))
    to_stage_number: Mapped[int | None] = mapped_column(Integer)
    event_at_ms: Mapped[int] = mapped_column(Integer)
    recorded_at_ms: Mapped[int] = mapped_column(Integer)
    source_update_id: Mapped[int | None] = mapped_column(Integer)
    skipped_unsolved_tasks: Mapped[bool] = mapped_column(BoolInt, default=False, server_default="0")

    user: Mapped[UserRecord] = relationship(back_populates="transitions")


class TaskAttemptRecord(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "stage_number",
            "task_number",
            "attempt_number",
            name="uq_task_attempts_sequence",
        ),
        UniqueConstraint("source_update_id", name="uq_task_attempts_source_update_id"),
        CheckConstraint("stage_number > 0", name="stage_number"),
        CheckConstraint("task_number > 0", name="task_number"),
        CheckConstraint("attempt_number > 0", name="attempt_number"),
        CheckConstraint("length(trim(raw_answer)) > 0", name="raw_answer_nonempty"),
        CheckConstraint("length(normalized_answer) > 0", name="normalized_answer_nonempty"),
        CheckConstraint("event_at_ms >= 0", name="event_at_ms"),
        CheckConstraint("recorded_at_ms >= 0", name="recorded_at_ms"),
        {"sqlite_strict": True},
    )

    attempt_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))
    stage_number: Mapped[int] = mapped_column(Integer)
    task_number: Mapped[int] = mapped_column(Integer)
    attempt_number: Mapped[int] = mapped_column(Integer)
    raw_answer: Mapped[str] = mapped_column(Text)
    normalized_answer: Mapped[str] = mapped_column(Text)
    event_at_ms: Mapped[int] = mapped_column(Integer)
    recorded_at_ms: Mapped[int] = mapped_column(Integer)
    source_update_id: Mapped[int] = mapped_column(Integer)

    user: Mapped[UserRecord] = relationship(back_populates="attempts")


Index(
    "task_attempts_answer_idx",
    TaskAttemptRecord.user_id,
    TaskAttemptRecord.stage_number,
    TaskAttemptRecord.task_number,
    TaskAttemptRecord.normalized_answer,
)
