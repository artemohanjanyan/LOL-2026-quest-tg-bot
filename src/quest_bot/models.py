import time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


class User(FrozenModel):
    user_id: int
    username: str
    role: UserRole
    active: bool


class ContentPart(FrozenModel):
    content_type: ContentType
    data: str
    caption: str | None = None


class Stage(FrozenModel):
    stage_number: int
    name: str


class Task(FrozenModel):
    stage_number: int
    task_number: int
    correct_answer_raw: str
    prompt_parts: tuple[ContentPart, ...]


class CaptainState(FrozenModel):
    user_id: int
    position: CaptainPosition
    started_at_ms: int | None
    current_stage_number: int | None


class CaptainTransition(FrozenModel):
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


class RecordedAttempt(FrozenModel):
    attempt_number: int
    correct: bool


class TaskProgress(FrozenModel):
    stage_number: int
    task_number: int
    solved_attempt_number: int | None
    points: int

    @property
    def solved(self) -> bool:
        return self.solved_attempt_number is not None


class CaptainSummary(FrozenModel):
    user: User
    state: CaptainState
    solved_tasks: int
    total_tasks: int
    total_score: int
