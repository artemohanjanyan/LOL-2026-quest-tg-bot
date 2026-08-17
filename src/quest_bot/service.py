"""Quest rules and orchestration independent of Telegram update objects."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from quest_bot.errors import (
    ContentValidationError,
    InactiveUser,
    InvalidQuestState,
    NotAuthorized,
    NotFound,
    QuestNotReady,
    UnknownUser,
)
from quest_bot.models import (
    CaptainPosition,
    CaptainState,
    CaptainSummary,
    CaptainTransition,
    ContentPart,
    OutroKind,
    Stage,
    Task,
    TaskAttempt,
    TaskProgress,
    User,
    UserRole,
    utc_now_ms,
)
from quest_bot.normalization import normalize_answer
from quest_bot.storage.base import (
    AttemptLimitReachedError,
    RecordNotFoundError,
    StateConflictError,
    TaskAlreadySolvedError,
    TaskLimitExceededError,
)
from quest_bot.storage.sqlite import SQLiteQuestStore


@dataclass(frozen=True, slots=True)
class StartResult:
    state: CaptainState
    intro_parts: tuple[ContentPart, ...]
    started: bool


@dataclass(frozen=True, slots=True)
class StagePresentation:
    stage: Stage
    tasks: tuple[Task, ...]


@dataclass(frozen=True, slots=True)
class AdvanceResult:
    state: CaptainState
    presentation: StagePresentation | None
    unsolved_task_numbers: tuple[int, ...]
    finished: bool
    outro_parts: tuple[ContentPart, ...] = ()
    final_score: int = 0

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.unsolved_task_numbers)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    attempt_number: int
    correct: bool
    points: int
    can_retry: bool


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    user: User
    state: CaptainState
    elapsed_seconds: int
    limit_minutes: int
    total_score: int
    stage: Stage | None = None
    solved_tasks: int = 0
    total_tasks: int = 0


@dataclass(frozen=True, slots=True)
class CaptainProgressReport:
    snapshot: StatusSnapshot
    started_at_ms: int | None
    tasks: tuple[TaskProgress, ...]
    stages: tuple[Stage, ...]


@dataclass(frozen=True, slots=True)
class AnswerActivity:
    attempt: TaskAttempt
    currently_correct: bool
    credited: bool
    points: int


@dataclass(frozen=True, slots=True)
class CaptainActivityReport:
    snapshot: StatusSnapshot
    started_at_ms: int | None
    transitions: tuple[CaptainTransition, ...]
    answers: tuple[AnswerActivity, ...]
    stages: tuple[Stage, ...]


@dataclass(frozen=True, slots=True)
class WrongAnswerStatistics:
    answer: str
    attempt_count: int
    captain_count: int


@dataclass(frozen=True, slots=True)
class TaskAnswerStatistics:
    stage: Stage
    task: Task
    solved_captain_count: int
    wrong_answers: tuple[WrongAnswerStatistics, ...]


@dataclass(frozen=True, slots=True)
class TaskStatisticsReport:
    active_captain_count: int
    tasks: tuple[TaskAnswerStatistics, ...]


@dataclass(frozen=True, slots=True)
class TimeoutSweepResult:
    expired_captains: tuple[tuple[int, int], ...]
    outro_parts: tuple[ContentPart, ...]


@dataclass(frozen=True, slots=True)
class ConfiguredStageSummary:
    stage: Stage
    task_count: int


@dataclass(frozen=True, slots=True)
class QuestSettingsSnapshot:
    time_limit_minutes: int
    score_steps: tuple[int, ...]
    intro_part_count: int
    success_outro_part_count: int
    timeout_outro_part_count: int
    stages: tuple[ConfiguredStageSummary, ...]

    @property
    def ready(self) -> bool:
        return (
            self.intro_part_count > 0
            and self.success_outro_part_count > 0
            and self.timeout_outro_part_count > 0
            and bool(self.stages)
            and all(stage.task_count > 0 for stage in self.stages)
        )


class QuestService:
    """Concrete application service containing all mutable quest policy."""

    def __init__(
        self,
        store: SQLiteQuestStore,
        *,
        clock: Callable[[], int] = utc_now_ms,
        owner_admin_id: int | None = None,
    ) -> None:
        self.store = store
        self._clock = clock
        self._owner_admin_id = owner_admin_id

    # Authorization ---------------------------------------------------------

    def require_user(self, actor_id: int) -> User:
        user = self.store.get_user(actor_id)
        if user is None:
            raise UnknownUser
        if not user.active:
            raise InactiveUser
        return user

    def require_admin(self, actor_id: int) -> User:
        user = self.require_user(actor_id)
        if user.role is not UserRole.ADMIN:
            raise NotAuthorized
        return user

    def require_owner_admin(self, actor_id: int) -> User:
        user = self.require_admin(actor_id)
        if actor_id != self._owner_admin_id:
            raise NotAuthorized
        return user

    def is_owner_admin(self, actor_id: int) -> bool:
        return actor_id == self._owner_admin_id

    # Captain flow ----------------------------------------------------------

    def start(
        self,
        actor_id: int,
        *,
        event_at_ms: int,
        source_update_id: int,
    ) -> StartResult:
        self.require_user(actor_id)
        existing = self.store.get_captain_state(actor_id)
        if existing.position is not CaptainPosition.NOT_STARTED:
            return StartResult(existing, self.store.get_intro_parts(), False)
        self._assert_quest_ready()
        try:
            state = self.store.transition_captain(
                actor_id,
                expected_position=CaptainPosition.NOT_STARTED,
                expected_stage_number=None,
                target_position=CaptainPosition.INTRO,
                target_stage_number=None,
                event_at_ms=event_at_ms,
                recorded_at_ms=self._clock(),
                source_update_id=source_update_id,
            )
        except StateConflictError:
            return StartResult(
                self.store.get_captain_state(actor_id), self.store.get_intro_parts(), False
            )
        return StartResult(state, self.store.get_intro_parts(), True)

    def advance(
        self,
        actor_id: int,
        *,
        event_at_ms: int,
        source_update_id: int,
        confirm_skip: bool = False,
    ) -> AdvanceResult:
        self.require_user(actor_id)
        state = self.store.get_captain_state(actor_id)
        if state.position.is_terminal:
            raise InvalidQuestState("terminal")
        if state.position is CaptainPosition.NOT_STARTED:
            raise InvalidQuestState("not_started")

        stages = self.store.list_stages()
        if not stages:
            raise QuestNotReady("no stages")

        skipped = False
        next_stage: Stage | None
        if state.position is CaptainPosition.INTRO:
            next_stage = stages[0]
        else:
            assert state.current_stage_number is not None
            progress = self.store.list_task_progress(actor_id)
            score_steps = self.store.get_score_steps()
            unsolved = tuple(
                item
                for item in progress
                if item.stage_number == state.current_stage_number and not item.solved
            )
            retryable_unsolved = tuple(
                item.task_number
                for item in unsolved
                if item.attempt_count < len(score_steps) and score_steps[item.attempt_count] > 0
            )
            if retryable_unsolved and not confirm_skip:
                return AdvanceResult(state, None, retryable_unsolved, False)
            skipped = bool(unsolved)
            next_stage = next(
                (stage for stage in stages if stage.stage_number > state.current_stage_number),
                None,
            )

        if next_stage is None:
            target_position = CaptainPosition.FINISHED
            target_stage_number = None
        else:
            target_position = CaptainPosition.STAGE
            target_stage_number = next_stage.stage_number

        try:
            actual_state = self.store.transition_captain(
                actor_id,
                expected_position=state.position,
                expected_stage_number=state.current_stage_number,
                target_position=target_position,
                target_stage_number=target_stage_number,
                event_at_ms=event_at_ms,
                recorded_at_ms=self._clock(),
                source_update_id=source_update_id,
                skipped_unsolved_tasks=skipped,
            )
        except StateConflictError as error:
            raise InvalidQuestState("position changed") from error

        presentation = (
            self._presentation(actual_state.current_stage_number)
            if actual_state.position is CaptainPosition.STAGE
            and actual_state.current_stage_number is not None
            else None
        )
        finished = actual_state.position is CaptainPosition.FINISHED
        return AdvanceResult(
            state=actual_state,
            presentation=presentation,
            unsolved_task_numbers=(),
            finished=finished,
            outro_parts=(self.store.get_outro_parts(OutroKind.SUCCESS) if finished else ()),
            final_score=(
                sum(item.points for item in self.store.list_task_progress(actor_id))
                if finished
                else 0
            ),
        )

    def answer(
        self,
        actor_id: int,
        task_number: int,
        raw_answer: str,
        *,
        event_at_ms: int,
        source_update_id: int,
    ) -> AnswerResult:
        self.require_user(actor_id)
        if task_number <= 0 or not raw_answer.strip():
            raise ContentValidationError("invalid answer")
        state = self.store.get_captain_state(actor_id)
        if state.position.is_terminal:
            raise InvalidQuestState("terminal")
        if state.position is not CaptainPosition.STAGE or state.current_stage_number is None:
            raise InvalidQuestState("answers require a stage")
        try:
            stored = self.store.record_attempt(
                actor_id,
                state.current_stage_number,
                task_number,
                raw_answer,
                event_at_ms=event_at_ms,
                recorded_at_ms=self._clock(),
                source_update_id=source_update_id,
            )
        except TaskAlreadySolvedError as error:
            raise InvalidQuestState("task already solved") from error
        except AttemptLimitReachedError as error:
            raise InvalidQuestState("attempts exhausted") from error
        except RecordNotFoundError as error:
            raise NotFound("task") from error
        except StateConflictError as error:
            raise InvalidQuestState("position changed") from error

        score_steps = self.store.get_score_steps()
        index = stored.attempt_number - 1
        points = score_steps[index] if stored.correct and index < len(score_steps) else 0
        next_attempt_index = stored.attempt_number
        can_retry = (
            not stored.correct
            and next_attempt_index < len(score_steps)
            and score_steps[next_attempt_index] > 0
        )
        return AnswerResult(stored.attempt_number, stored.correct, points, can_retry)

    def get_stage(self, actor_id: int) -> StagePresentation:
        self.require_user(actor_id)
        state = self.store.get_captain_state(actor_id)
        if state.position is not CaptainPosition.STAGE or state.current_stage_number is None:
            raise InvalidQuestState("no current stage")
        return self._presentation(state.current_stage_number)

    def get_intro(self, actor_id: int) -> tuple[ContentPart, ...]:
        self.require_user(actor_id)
        state = self.store.get_captain_state(actor_id)
        if state.position is not CaptainPosition.INTRO:
            raise InvalidQuestState("not at intro")
        return self.store.get_intro_parts()

    def status(self, actor_id: int, *, now_ms: int | None = None) -> StatusSnapshot:
        user = self.require_user(actor_id)
        return self._status_snapshot(user, now_ms=now_ms)

    def _status_snapshot(self, user: User, *, now_ms: int | None = None) -> StatusSnapshot:
        now = self._clock() if now_ms is None else now_ms
        state = self.store.get_captain_state(user.user_id)
        progress = self.store.list_task_progress(user.user_id)
        elapsed = 0
        if state.position.is_active and state.started_at_ms is not None:
            elapsed = max(0, (now - state.started_at_ms) // 1_000)
        stage = None
        solved = 0
        total = 0
        if state.position is CaptainPosition.STAGE and state.current_stage_number is not None:
            stage = self.store.get_stage(state.current_stage_number)
            stage_progress = tuple(
                item for item in progress if item.stage_number == state.current_stage_number
            )
            solved = sum(item.solved for item in stage_progress)
            total = len(stage_progress)
        return StatusSnapshot(
            user=user,
            state=state,
            elapsed_seconds=elapsed,
            limit_minutes=self.store.get_time_limit(),
            total_score=sum(item.points for item in progress),
            stage=stage,
            solved_tasks=solved,
            total_tasks=total,
        )

    # Administration --------------------------------------------------------

    def add_admin(self, actor_id: int, user_id: int, display_name: str) -> User:
        self.require_owner_admin(actor_id)
        self._validate_identity(user_id, display_name)
        return self.store.ensure_admin(user_id, display_name.strip(), self._clock())

    def add_captain(
        self,
        actor_id: int,
        user_id: int,
        display_name: str | None = None,
    ) -> User:
        self.require_admin(actor_id)
        if display_name is None:
            existing = self.store.get_user(user_id)
            if existing is None:
                raise ContentValidationError("display name required for new captain")
            display_name = existing.display_name
        self._validate_identity(user_id, display_name)
        return self.store.add_captain(user_id, display_name.strip(), self._clock())

    def remove_captain(self, actor_id: int, user_id: int) -> bool:
        self.require_admin(actor_id)
        target = self.store.get_user(user_id)
        if target is None or target.role is not UserRole.CAPTAIN:
            return False
        return self.store.deactivate_captain(user_id, self._clock())

    def captain_reset_target(self, actor_id: int, reference: str) -> tuple[User, CaptainState]:
        self.require_admin(actor_id)
        target = self.resolve_user(reference)
        return target, self.store.get_captain_state(target.user_id)

    def reset_captain(
        self,
        actor_id: int,
        expected_state: CaptainState,
        *,
        event_at_ms: int,
        source_update_id: int,
    ) -> User:
        self.require_admin(actor_id)
        target = self.store.get_user(expected_state.user_id)
        if target is None:
            raise NotFound("captain")
        try:
            self.store.reset_captain(
                expected_state,
                event_at_ms=event_at_ms,
                recorded_at_ms=self._clock(),
                source_update_id=source_update_id,
            )
        except StateConflictError as error:
            raise InvalidQuestState("captain progress changed") from error
        return target

    def list_users(self, actor_id: int) -> tuple[User, ...]:
        self.require_admin(actor_id)
        return self.store.list_users(include_inactive=True)

    def replace_intro(self, actor_id: int, parts: Sequence[ContentPart]) -> None:
        self.require_admin(actor_id)
        self._validate_parts(parts)
        self.store.replace_intro_parts(parts)

    def replace_outro(self, actor_id: int, kind: OutroKind, parts: Sequence[ContentPart]) -> None:
        self.require_admin(actor_id)
        self._validate_parts(parts)
        self.store.replace_outro_parts(kind, parts)

    def set_stage(
        self,
        actor_id: int,
        stage_number: int,
        name: str,
    ) -> Stage:
        self.require_admin(actor_id)
        if stage_number <= 0 or not name.strip():
            raise ContentValidationError("invalid stage")
        return self.store.set_stage(stage_number, name.strip())

    def set_task(
        self,
        actor_id: int,
        stage_number: int,
        task_number: int,
        correct_answers: Sequence[str],
        prompt_parts: Sequence[ContentPart],
        *,
        name: str | None = None,
    ) -> Task:
        self.require_admin(actor_id)
        answers = tuple(answer.strip() for answer in correct_answers)
        normalized_answers = tuple(normalize_answer(answer) for answer in answers)
        if (
            stage_number <= 0
            or task_number <= 0
            or not answers
            or any(not answer for answer in answers)
            or len(set(normalized_answers)) != len(normalized_answers)
        ):
            raise ContentValidationError("invalid task")
        self._validate_parts(prompt_parts)
        normalized_name = name.strip() or None if name is not None else None
        try:
            return self.store.set_task(
                stage_number,
                task_number,
                answers,
                prompt_parts,
                name=normalized_name,
            )
        except RecordNotFoundError as error:
            raise NotFound("stage") from error
        except TaskLimitExceededError as error:
            raise ContentValidationError("too many tasks") from error

    def delete_stage(self, actor_id: int, stage_number: int) -> bool:
        self.require_admin(actor_id)
        return self.store.delete_stage(stage_number)

    def delete_task(self, actor_id: int, stage_number: int, task_number: int) -> bool:
        self.require_admin(actor_id)
        return self.store.delete_task(stage_number, task_number)

    def list_stages(self, actor_id: int) -> tuple[Stage, ...]:
        self.require_admin(actor_id)
        return self.store.list_stages()

    def show_stage(self, actor_id: int, stage_number: int) -> StagePresentation:
        self.require_admin(actor_id)
        return self._presentation(stage_number)

    def show_task(self, actor_id: int, stage_number: int, task_number: int) -> Task:
        self.require_admin(actor_id)
        task = self.store.get_task(stage_number, task_number)
        if task is None:
            raise NotFound("task")
        return task

    def get_intro_for_admin(self, actor_id: int) -> tuple[ContentPart, ...]:
        self.require_admin(actor_id)
        return self.store.get_intro_parts()

    def get_outro_for_admin(self, actor_id: int, kind: OutroKind) -> tuple[ContentPart, ...]:
        self.require_admin(actor_id)
        return self.store.get_outro_parts(kind)

    def set_scores(self, actor_id: int, points: Sequence[int]) -> tuple[int, ...]:
        self.require_admin(actor_id)
        schedule = tuple(points)
        if (
            not schedule
            or any(point < 0 for point in schedule)
            or any(left < right for left, right in zip(schedule[:-1], schedule[1:], strict=True))
            or schedule[-1] != 0
        ):
            raise ContentValidationError("invalid scores")
        return self.store.set_score_steps(schedule)

    def set_time_limit(self, actor_id: int, minutes: int) -> int:
        self.require_admin(actor_id)
        if minutes <= 0:
            raise ContentValidationError("invalid time limit")
        return self.store.set_time_limit(minutes)

    def show_settings(self, actor_id: int) -> QuestSettingsSnapshot:
        self.require_admin(actor_id)
        return self._settings_snapshot()

    def leaderboard(self, actor_id: int) -> tuple[CaptainSummary, ...]:
        self.require_admin(actor_id)
        summaries = self.store.list_captain_summaries()
        return tuple(
            sorted(
                summaries,
                key=lambda item: (
                    -item.total_score,
                    item.user.display_name.casefold(),
                ),
            )
        )

    def task_statistics(self, actor_id: int) -> TaskStatisticsReport:
        self.require_admin(actor_id)
        active_captains = tuple(
            user
            for user in self.store.list_users(include_inactive=False)
            if user.role is UserRole.CAPTAIN
        )
        configured_tasks = tuple(
            (stage, task)
            for stage in self.store.list_stages()
            for task in self.store.list_stage_tasks(stage.stage_number)
        )
        accepted_answers = {
            (task.stage_number, task.task_number): frozenset(
                normalize_answer(answer) for answer in task.correct_answers
            )
            for _, task in configured_tasks
        }
        solved_captains: dict[tuple[int, int], set[int]] = {}
        wrong_attempts: Counter[tuple[int, int, str]] = Counter()
        wrong_captains: dict[tuple[int, int, str], set[int]] = {}
        for attempt in self.store.list_active_captain_attempts():
            task_key = (attempt.stage_number, attempt.task_number)
            if attempt.normalized_answer in accepted_answers.get(task_key, frozenset()):
                solved_captains.setdefault(task_key, set()).add(attempt.user_id)
                continue
            answer_key = (*task_key, attempt.normalized_answer)
            wrong_attempts[answer_key] += 1
            wrong_captains.setdefault(answer_key, set()).add(attempt.user_id)

        return TaskStatisticsReport(
            active_captain_count=len(active_captains),
            tasks=tuple(
                TaskAnswerStatistics(
                    stage=stage,
                    task=task,
                    solved_captain_count=len(
                        solved_captains.get((task.stage_number, task.task_number), ())
                    ),
                    wrong_answers=tuple(
                        sorted(
                            (
                                WrongAnswerStatistics(
                                    answer=answer,
                                    attempt_count=count,
                                    captain_count=len(
                                        wrong_captains[
                                            (task.stage_number, task.task_number, answer)
                                        ]
                                    ),
                                )
                                for (
                                    stage_number,
                                    task_number,
                                    answer,
                                ), count in wrong_attempts.items()
                                if stage_number == task.stage_number
                                and task_number == task.task_number
                            ),
                            key=lambda item: (-item.attempt_count, item.answer),
                        )
                    ),
                )
                for stage, task in configured_tasks
            ),
        )

    def captain_progress(self, actor_id: int, reference: str) -> CaptainProgressReport:
        self.require_admin(actor_id)
        target = self.resolve_user(reference)
        snapshot = self._status_snapshot(target)
        transitions = self.store.list_captain_transitions(target.user_id)
        start = self._current_run_start(snapshot, transitions)
        started_at_ms = start.event_at_ms if start is not None else snapshot.state.started_at_ms
        return CaptainProgressReport(
            snapshot=snapshot,
            started_at_ms=started_at_ms,
            tasks=self.store.list_task_progress(target.user_id),
            stages=self.store.list_stages(),
        )

    def captain_activity(self, actor_id: int, reference: str) -> CaptainActivityReport:
        self.require_admin(actor_id)
        target = self.resolve_user(reference)
        snapshot = self._status_snapshot(target)
        all_transitions = self.store.list_captain_transitions(target.user_id)
        start = self._current_run_start(snapshot, all_transitions)
        started_at_ms = start.event_at_ms if start is not None else snapshot.state.started_at_ms
        stages = self.store.list_stages()
        if started_at_ms is None:
            return CaptainActivityReport(snapshot, None, (), (), stages)

        current_transitions = tuple(
            transition
            for transition in all_transitions
            if (
                transition.sequence_number >= start.sequence_number
                if start is not None
                else transition.event_at_ms >= started_at_ms
            )
        )
        current_attempts = tuple(
            attempt
            for attempt in self.store.list_task_attempts(target.user_id)
            if attempt.event_at_ms >= started_at_ms
        )
        accepted_answers = {
            (task.stage_number, task.task_number): frozenset(
                normalize_answer(answer) for answer in task.correct_answers
            )
            for stage in stages
            for task in self.store.list_stage_tasks(stage.stage_number)
        }
        progress = {
            (task.stage_number, task.task_number): task
            for task in self.store.list_task_progress(target.user_id)
        }
        answers = tuple(
            self._answer_activity(attempt, accepted_answers, progress)
            for attempt in current_attempts
        )
        return CaptainActivityReport(
            snapshot=snapshot,
            started_at_ms=started_at_ms,
            transitions=current_transitions,
            answers=answers,
            stages=stages,
        )

    @staticmethod
    def _current_run_start(
        snapshot: StatusSnapshot,
        transitions: tuple[CaptainTransition, ...],
    ) -> CaptainTransition | None:
        if snapshot.state.started_at_ms is None:
            return None
        return next(
            (
                transition
                for transition in reversed(transitions)
                if transition.from_position is CaptainPosition.NOT_STARTED
                and transition.to_position is CaptainPosition.INTRO
            ),
            None,
        )

    @staticmethod
    def _answer_activity(
        attempt: TaskAttempt,
        accepted_answers: dict[tuple[int, int], frozenset[str]],
        progress: dict[tuple[int, int], TaskProgress],
    ) -> AnswerActivity:
        key = (attempt.stage_number, attempt.task_number)
        currently_correct = attempt.normalized_answer in accepted_answers.get(key, frozenset())
        task_progress = progress.get(key)
        credited = (
            task_progress is not None
            and task_progress.solved_attempt_number == attempt.attempt_number
        )
        return AnswerActivity(
            attempt=attempt,
            currently_correct=currently_correct,
            credited=credited,
            points=task_progress.points if credited and task_progress is not None else 0,
        )

    def resolve_user(self, reference: str) -> User:
        cleaned = reference.strip()
        user = self.store.get_user(int(cleaned)) if cleaned.isdecimal() else None
        if user is None and cleaned:
            user = self.store.get_user_by_display_name(cleaned)
        if user is None and cleaned and not cleaned.startswith("@"):
            user = self.store.get_user_by_display_name(f"@{cleaned}")
        if user is None:
            raise NotFound("captain")
        return user

    def active_recipients(self, actor_id: int) -> tuple[User, ...]:
        self.require_admin(actor_id)
        return tuple(
            user
            for user in self.store.list_users(include_inactive=False)
            if user.role is UserRole.CAPTAIN
        )

    # Timeout sweep ---------------------------------------------------------

    def sweep_timeouts(self) -> TimeoutSweepResult:
        expired = self.store.claim_overdue_captains(self._clock())
        if not expired:
            return TimeoutSweepResult((), ())
        return TimeoutSweepResult(
            tuple(
                (
                    state.user_id,
                    sum(item.points for item in self.store.list_task_progress(state.user_id)),
                )
                for state in expired
            ),
            self.store.get_outro_parts(OutroKind.TIMEOUT),
        )

    # Internal helpers ------------------------------------------------------

    def _assert_quest_ready(self) -> None:
        if not self._settings_snapshot().ready:
            raise QuestNotReady

    def _settings_snapshot(self) -> QuestSettingsSnapshot:
        stages = self.store.list_stages()
        return QuestSettingsSnapshot(
            time_limit_minutes=self.store.get_time_limit(),
            score_steps=self.store.get_score_steps(),
            intro_part_count=len(self.store.get_intro_parts()),
            success_outro_part_count=len(self.store.get_outro_parts(OutroKind.SUCCESS)),
            timeout_outro_part_count=len(self.store.get_outro_parts(OutroKind.TIMEOUT)),
            stages=tuple(
                ConfiguredStageSummary(
                    stage,
                    len(self.store.list_stage_tasks(stage.stage_number)),
                )
                for stage in stages
            ),
        )

    def _presentation(self, stage_number: int) -> StagePresentation:
        stage = self.store.get_stage(stage_number)
        if stage is None:
            raise NotFound("stage")
        return StagePresentation(stage, self.store.list_stage_tasks(stage_number))

    @staticmethod
    def _validate_parts(parts: Sequence[ContentPart]) -> None:
        if not parts or any(not part.data for part in parts):
            raise ContentValidationError("content must have at least one part")

    @staticmethod
    def _validate_identity(user_id: int, display_name: str) -> None:
        if user_id <= 0 or not display_name.strip().lstrip("@"):
            raise ContentValidationError("invalid captain")
