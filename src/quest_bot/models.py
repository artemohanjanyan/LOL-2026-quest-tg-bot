from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    CAPTAIN = "captain"


class CaptainPosition(StrEnum):
    NOT_STARTED = "not_started"
    INTRO = "intro"
    STAGE = "stage"
    FINISHED = "finished"
    TIMED_OUT = "timed_out"

    @property
    def is_active(self) -> bool:
        return self in {CaptainPosition.INTRO, CaptainPosition.STAGE}

    @property
    def is_terminal(self) -> bool:
        return self in {CaptainPosition.FINISHED, CaptainPosition.TIMED_OUT}


class ContentType(StrEnum):
    TEXT = "text"
    PHOTO = "photo"
    STICKER = "sticker"
    VOICE = "voice"
    DOCUMENT = "document"
    VIDEO = "video"
    VIDEO_NOTE = "video_note"


class OutroKind(StrEnum):
    SUCCESS = "success"
    TIMEOUT = "timeout"


def utc_now_ms() -> int:
    """Return the current Unix timestamp in UTC milliseconds."""

    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class User:
    user_id: int
    username: str
    role: UserRole
    active: bool
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class ContentPart:
    content_type: ContentType
    data: str
    caption: str | None = None


@dataclass(frozen=True, slots=True)
class QuestSettings:
    time_limit_minutes: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class Stage:
    stage_number: int
    name: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class Task:
    stage_number: int
    task_number: int
    correct_answer_raw: str
    correct_answer_normalized: str
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class TaskContent:
    task: Task
    prompt_parts: tuple[ContentPart, ...]


@dataclass(frozen=True, slots=True)
class CaptainState:
    user_id: int
    position: CaptainPosition
    started_at_ms: int | None
    current_stage_number: int | None


@dataclass(frozen=True, slots=True)
class CaptainTransition:
    transition_id: int
    user_id: int
    sequence_number: int
    from_position: CaptainPosition
    from_stage_number: int | None
    to_position: CaptainPosition
    to_stage_number: int | None
    event_at_ms: int
    recorded_at_ms: int
    source_update_id: int | None
    skipped_unsolved_tasks: bool


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    attempt_id: int
    user_id: int
    stage_number: int
    task_number: int
    attempt_number: int
    raw_answer: str
    normalized_answer: str
    event_at_ms: int
    recorded_at_ms: int
    source_update_id: int


@dataclass(frozen=True, slots=True)
class TaskProgress:
    task: Task
    solved_attempt_number: int | None
    points: int

    @property
    def solved(self) -> bool:
        return self.solved_attempt_number is not None


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: CaptainState
    transition: CaptainTransition | None
    applied: bool


@dataclass(frozen=True, slots=True)
class AttemptResult:
    attempt: TaskAttempt
    created: bool


@dataclass(frozen=True, slots=True)
class CaptainSummary:
    user: User
    state: CaptainState
    solved_tasks: int
    total_tasks: int
    total_score: int
