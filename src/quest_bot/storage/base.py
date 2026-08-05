from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from quest_bot.models import (
    AttemptResult,
    CaptainPosition,
    CaptainState,
    CaptainSummary,
    CaptainTransition,
    ContentPart,
    OutroDelivery,
    OutroDeliveryPart,
    OutroKind,
    OutroWorkItem,
    QuestSettings,
    Stage,
    TaskAttempt,
    TaskContent,
    TaskProgress,
    TransitionResult,
    User,
)


class StoreError(RuntimeError):
    """Base class for expected persistence-layer failures."""


class StoreClosedError(StoreError):
    """Raised when an operation is attempted after closing the store."""


class InstanceAlreadyRunningError(StoreError):
    """Raised when another process owns the database instance lock."""


class RecordNotFoundError(StoreError):
    """Raised when an operation targets a missing persistent entity."""


class StateConflictError(StoreError):
    """Raised when the captain is not in the state required by an operation."""


class DuplicateUpdateError(StoreError):
    """Raised when one Telegram update ID is reused for another action."""


class TaskAlreadySolvedError(StateConflictError):
    """Raised when a captain attempts a currently solved task."""


class QuestStore(Protocol):
    def close(self) -> None: ...

    def ensure_admin(self, user_id: int, username: str, now_ms: int) -> User: ...

    def add_captain(self, user_id: int, username: str, now_ms: int) -> User: ...

    def deactivate_captain(self, user_id: int, now_ms: int) -> bool: ...

    def get_user(self, user_id: int) -> User | None: ...

    def get_user_by_username(self, username: str) -> User | None: ...

    def list_users(self, *, include_inactive: bool = True) -> tuple[User, ...]: ...

    def get_settings(self) -> QuestSettings: ...

    def set_time_limit(self, minutes: int, now_ms: int) -> QuestSettings: ...

    def get_score_steps(self) -> tuple[int, ...]: ...

    def set_score_steps(self, points: Sequence[int]) -> tuple[int, ...]: ...

    def get_intro_parts(self) -> tuple[ContentPart, ...]: ...

    def replace_intro_parts(self, parts: Sequence[ContentPart]) -> None: ...

    def get_outro_parts(self, kind: OutroKind) -> tuple[ContentPart, ...]: ...

    def replace_outro_parts(self, kind: OutroKind, parts: Sequence[ContentPart]) -> None: ...

    def set_stage(self, stage_number: int, name: str, now_ms: int) -> Stage: ...

    def get_stage(self, stage_number: int) -> Stage | None: ...

    def list_stages(self) -> tuple[Stage, ...]: ...

    def delete_stage(self, stage_number: int) -> bool: ...

    def set_task(
        self,
        stage_number: int,
        task_number: int,
        correct_answer: str,
        prompt_parts: Sequence[ContentPart],
        now_ms: int,
    ) -> TaskContent: ...

    def get_task(self, stage_number: int, task_number: int) -> TaskContent | None: ...

    def list_stage_tasks(self, stage_number: int) -> tuple[TaskContent, ...]: ...

    def delete_task(self, stage_number: int, task_number: int) -> bool: ...

    def get_captain_state(self, user_id: int) -> CaptainState | None: ...

    def ensure_captain_state(self, user_id: int, now_ms: int) -> CaptainState: ...

    def start_captain(
        self,
        user_id: int,
        *,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int,
    ) -> TransitionResult: ...

    def transition_captain(
        self,
        user_id: int,
        *,
        expected_position: CaptainPosition,
        expected_stage_number: int | None,
        target_position: CaptainPosition,
        target_stage_number: int | None,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int | None,
        skipped_unsolved_tasks: bool = False,
        timeout_deadline_at_ms: int | None = None,
        timeout_limit_minutes: int | None = None,
    ) -> TransitionResult: ...

    def claim_overdue_captains(self, now_ms: int) -> tuple[TransitionResult, ...]: ...

    def list_captain_transitions(self, user_id: int) -> tuple[CaptainTransition, ...]: ...

    def record_attempt(
        self,
        user_id: int,
        stage_number: int,
        task_number: int,
        raw_answer: str,
        *,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int,
    ) -> AttemptResult: ...

    def list_attempts(
        self,
        user_id: int,
        *,
        stage_number: int | None = None,
        task_number: int | None = None,
    ) -> tuple[TaskAttempt, ...]: ...

    def get_stage_progress(self, user_id: int, stage_number: int) -> tuple[TaskProgress, ...]: ...

    def get_total_score(self, user_id: int) -> int: ...

    def list_captain_summaries(
        self,
        *,
        include_admins: bool = False,
        include_inactive: bool = False,
    ) -> tuple[CaptainSummary, ...]: ...

    def get_outro_delivery(self, delivery_id: int) -> OutroDelivery | None: ...

    def get_outro_delivery_for_user(self, user_id: int) -> OutroDelivery | None: ...

    def get_outro_delivery_parts(self, delivery_id: int) -> tuple[OutroDeliveryPart, ...]: ...

    def list_ready_outro_work(
        self, now_ms: int, *, max_attempts: int = 5, limit: int = 100
    ) -> tuple[OutroWorkItem, ...]: ...

    def mark_outro_part_sending(
        self, delivery_id: int, part_number: int, attempted_at_ms: int
    ) -> bool: ...

    def mark_outro_part_delivered(
        self,
        delivery_id: int,
        part_number: int,
        *,
        telegram_message_id: int | None,
        delivered_at_ms: int,
    ) -> None: ...

    def mark_outro_part_failed(
        self,
        delivery_id: int,
        part_number: int,
        *,
        error: str,
        failed_at_ms: int,
        next_attempt_at_ms: int | None,
        max_attempts: int = 5,
    ) -> None: ...

    def recover_interrupted_outro_deliveries(self, now_ms: int) -> int: ...

    def retry_outro_for_user(self, user_id: int, now_ms: int) -> bool: ...
