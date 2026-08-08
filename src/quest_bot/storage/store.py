"""SQLAlchemy persistence for quest content, progress, and transitions."""

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Self

from filelock import FileLock, Timeout
from sqlalchemy import URL, Connection, Engine, and_, create_engine, delete, event, func, select
from sqlalchemy.engine import ExceptionContext
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.pool import StaticPool

from quest_bot.models import (
    CaptainPosition,
    CaptainState,
    CaptainSummary,
    CaptainTransition,
    ContentPart,
    OutroKind,
    RecordedAttempt,
    Stage,
    Task,
    TaskProgress,
    User,
    UserRole,
)
from quest_bot.normalization import normalize_answer
from quest_bot.storage.errors import (
    DuplicateUpdateError,
    InstanceAlreadyRunningError,
    RecordNotFoundError,
    StateConflictError,
    StoreClosedError,
    TaskAlreadySolvedError,
    TaskLimitExceededError,
)
from quest_bot.storage.migrations import upgrade_database
from quest_bot.storage.schema import (
    CaptainStateRecord,
    CaptainTransitionRecord,
    GlobalContentPartRecord,
    QuestSettingsRecord,
    ScoreStepRecord,
    StageRecord,
    TaskAttemptRecord,
    TaskPromptPartRecord,
    TaskRecord,
    UserRecord,
)

INTRO_CONTENT_KIND = "intro"
MAX_TASKS_PER_STAGE = 9
LOGGER = logging.getLogger(__name__)


class QuestStore:
    """Short-lived SQLAlchemy sessions over one explicitly owned engine."""

    def __init__(
        self,
        engine: Engine,
        *,
        database_path: str,
        instance_lock: FileLock | None,
    ) -> None:
        self._engine: Engine | None = engine
        self._database_path = database_path
        self._instance_lock = instance_lock

    @classmethod
    def open(
        cls,
        database_path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 5_000,
        lock_instance: bool = True,
    ) -> Self:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")

        path = os.fspath(database_path)
        instance_lock = cls._acquire_instance_lock(path) if lock_instance else None
        engine: Engine | None = None
        try:
            engine = cls._create_engine(path, busy_timeout_ms)
            upgrade_database(engine)
            return cls(engine, database_path=path, instance_lock=instance_lock)
        except BaseException:
            try:
                if engine is not None:
                    engine.dispose()
            finally:
                if instance_lock is not None:
                    instance_lock.release()
            raise

    @staticmethod
    def _create_engine(database_path: str, busy_timeout_ms: int) -> Engine:
        memory = database_path == ":memory:"
        url = URL.create("sqlite+pysqlite", database=None if memory else database_path)
        options: dict[str, Any] = {
            "connect_args": {"timeout": busy_timeout_ms / 1_000},
        }
        if memory:
            options["poolclass"] = StaticPool
        engine = create_engine(url, **options)

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection: Any, _: Any) -> None:
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            if not memory:
                cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.close()

        @event.listens_for(engine, "begin")
        def begin_transaction(connection: Connection) -> None:
            statement = (
                "BEGIN IMMEDIATE"
                if connection.get_execution_options().get("quest_write")
                else "BEGIN"
            )
            connection.exec_driver_sql(statement)

        @event.listens_for(engine, "handle_error")
        def log_database_error(context: ExceptionContext) -> None:
            connection = context.connection
            LOGGER.error(
                "Database operation failed (path=%s, in_transaction=%s, statement=%r)",
                database_path,
                connection is not None and connection.in_transaction(),
                context.statement,
                exc_info=True,
            )

        return engine

    @staticmethod
    def _acquire_instance_lock(database_path: str) -> FileLock | None:
        if database_path == ":memory:":
            return None
        lock = FileLock(f"{database_path}.lock")
        try:
            lock.acquire(timeout=0)
        except Timeout as error:
            raise InstanceAlreadyRunningError(
                f"another bot process is already using {database_path}"
            ) from error
        return lock

    @property
    def database_path(self) -> str:
        return self._database_path

    @property
    def schema_revision(self) -> str:
        with self._engine_or_raise().connect() as connection:
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one()
        return str(revision)

    def __enter__(self) -> Self:
        self._engine_or_raise()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        engine = self._engine
        if engine is None:
            return
        self._engine = None
        try:
            engine.dispose()
        finally:
            if self._instance_lock is not None:
                self._instance_lock.release()
                self._instance_lock = None

    def _engine_or_raise(self) -> Engine:
        if self._engine is None:
            raise StoreClosedError("the quest store is closed")
        return self._engine

    @contextmanager
    def _read(self) -> Iterator[Session]:
        with Session(self._engine_or_raise()) as session:
            yield session

    @contextmanager
    def _write(self) -> Iterator[Session]:
        with (
            self._engine_or_raise().connect().execution_options(quest_write=True) as connection,
            Session(connection, expire_on_commit=False) as session,
            session.begin(),
        ):
            yield session

    # Users

    def ensure_admin(self, user_id: int, username: str, now_ms: int) -> User:
        return self._upsert_user(user_id, username, UserRole.ADMIN, now_ms)

    def add_captain(self, user_id: int, username: str, now_ms: int) -> User:
        return self._upsert_user(user_id, username, UserRole.CAPTAIN, now_ms)

    def _upsert_user(self, user_id: int, username: str, role: UserRole, now_ms: int) -> User:
        if not username.strip():
            raise ValueError("username must not be empty")
        with self._write() as session:
            record = session.get(UserRecord, user_id)
            if record is None:
                record = UserRecord(
                    user_id=user_id,
                    username=username,
                    role=role,
                    active=True,
                    updated_at_ms=now_ms,
                )
                session.add(record)
            else:
                record.username = username
                record.role = UserRole.ADMIN if record.role is UserRole.ADMIN else role
                record.active = True
                record.updated_at_ms = now_ms
            if session.get(CaptainStateRecord, user_id) is None:
                session.add(
                    CaptainStateRecord(
                        user_id=user_id,
                        position=CaptainPosition.NOT_STARTED,
                        started_at_ms=None,
                        current_stage_number=None,
                    )
                )
            session.flush()
            return User.model_validate(record)

    def deactivate_captain(self, user_id: int, now_ms: int) -> bool:
        with self._write() as session:
            record = session.get(UserRecord, user_id)
            if record is None or record.role is not UserRole.CAPTAIN or not record.active:
                return False
            record.active = False
            record.updated_at_ms = now_ms
            return True

    def get_user(self, user_id: int) -> User | None:
        with self._read() as session:
            record = session.get(UserRecord, user_id)
            return None if record is None else User.model_validate(record)

    def get_user_by_username(self, username: str) -> User | None:
        with self._read() as session:
            record = session.scalar(
                select(UserRecord)
                .where(UserRecord.username.collate("NOCASE") == username.removeprefix("@"))
                .order_by(
                    UserRecord.active.desc(),
                    UserRecord.updated_at_ms.desc(),
                    UserRecord.user_id,
                )
                .limit(1)
            )
            return None if record is None else User.model_validate(record)

    def list_users(self, *, include_inactive: bool = True) -> tuple[User, ...]:
        statement = select(UserRecord)
        if not include_inactive:
            statement = statement.where(UserRecord.active.is_(True))
        statement = statement.order_by(
            UserRecord.role,
            UserRecord.username.collate("NOCASE"),
            UserRecord.user_id,
        )
        with self._read() as session:
            return tuple(User.model_validate(row) for row in session.scalars(statement))

    # Quest settings and global content

    def get_time_limit(self) -> int:
        with self._read() as session:
            return self._settings(session).time_limit_minutes

    def set_time_limit(self, minutes: int) -> int:
        with self._write() as session:
            self._settings(session).time_limit_minutes = minutes
        return minutes

    @staticmethod
    def _settings(session: Session) -> QuestSettingsRecord:
        record = session.get(QuestSettingsRecord, 1)
        if record is None:
            raise RecordNotFoundError("quest settings do not exist")
        return record

    def get_score_steps(self) -> tuple[int, ...]:
        with self._read() as session:
            return tuple(
                session.scalars(
                    select(ScoreStepRecord.points).order_by(ScoreStepRecord.attempt_number)
                )
            )

    def set_score_steps(self, points: Sequence[int]) -> tuple[int, ...]:
        schedule = tuple(points)
        with self._write() as session:
            session.execute(delete(ScoreStepRecord))
            session.add_all(
                ScoreStepRecord(attempt_number=number, points=value)
                for number, value in enumerate(schedule, start=1)
            )
        return schedule

    def get_intro_parts(self) -> tuple[ContentPart, ...]:
        return self._get_content_parts(INTRO_CONTENT_KIND)

    def replace_intro_parts(self, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(INTRO_CONTENT_KIND, parts)

    def get_outro_parts(self, kind: OutroKind) -> tuple[ContentPart, ...]:
        return self._get_content_parts(kind.value)

    def replace_outro_parts(self, kind: OutroKind, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(kind.value, parts)

    def _get_content_parts(self, kind: str) -> tuple[ContentPart, ...]:
        with self._read() as session:
            rows = session.scalars(
                select(GlobalContentPartRecord)
                .where(GlobalContentPartRecord.content_kind == kind)
                .order_by(GlobalContentPartRecord.part_number)
            )
            return tuple(ContentPart.model_validate(row) for row in rows)

    def _replace_content_parts(self, kind: str, parts: Sequence[ContentPart]) -> None:
        with self._write() as session:
            session.execute(
                delete(GlobalContentPartRecord).where(GlobalContentPartRecord.content_kind == kind)
            )
            session.add_all(
                GlobalContentPartRecord(
                    content_kind=kind,
                    part_number=number,
                    content_type=part.content_type,
                    data=part.data,
                    caption=part.caption,
                )
                for number, part in enumerate(parts, start=1)
            )

    # Stages and tasks

    def set_stage(self, stage_number: int, name: str) -> Stage:
        with self._write() as session:
            record = session.get(StageRecord, stage_number)
            if record is None:
                record = StageRecord(stage_number=stage_number, name=name)
                session.add(record)
            else:
                record.name = name
            session.flush()
            return Stage.model_validate(record)

    def get_stage(self, stage_number: int) -> Stage | None:
        with self._read() as session:
            record = session.get(StageRecord, stage_number)
            return None if record is None else Stage.model_validate(record)

    def list_stages(self) -> tuple[Stage, ...]:
        with self._read() as session:
            return tuple(
                Stage.model_validate(row)
                for row in session.scalars(select(StageRecord).order_by(StageRecord.stage_number))
            )

    def delete_stage(self, stage_number: int) -> bool:
        with self._write() as session:
            record = session.get(StageRecord, stage_number)
            if record is None:
                return False
            session.delete(record)
            return True

    def set_task(
        self,
        stage_number: int,
        task_number: int,
        correct_answer: str,
        prompt_parts: Sequence[ContentPart],
    ) -> Task:
        parts = tuple(prompt_parts)
        if not parts:
            raise ValueError("task prompt must contain at least one part")
        with self._write() as session:
            if session.get(StageRecord, stage_number) is None:
                raise RecordNotFoundError(f"stage {stage_number} does not exist")
            record = session.get(TaskRecord, (stage_number, task_number))
            if record is None:
                task_count = session.scalar(
                    select(func.count())
                    .select_from(TaskRecord)
                    .where(TaskRecord.stage_number == stage_number)
                )
                if task_count is not None and task_count >= MAX_TASKS_PER_STAGE:
                    raise TaskLimitExceededError(
                        f"a stage may contain at most {MAX_TASKS_PER_STAGE} tasks"
                    )
                record = TaskRecord(
                    stage_number=stage_number,
                    task_number=task_number,
                    correct_answer_raw=correct_answer,
                    correct_answer_normalized=normalize_answer(correct_answer),
                )
                session.add(record)
            else:
                record.correct_answer_raw = correct_answer
                record.correct_answer_normalized = normalize_answer(correct_answer)
            record.prompt_parts = [
                TaskPromptPartRecord(
                    stage_number=stage_number,
                    task_number=task_number,
                    part_number=number,
                    content_type=part.content_type,
                    data=part.data,
                    caption=part.caption,
                )
                for number, part in enumerate(parts, start=1)
            ]
            session.flush()
            return Task.model_validate(record)

    def get_task(self, stage_number: int, task_number: int) -> Task | None:
        with self._read() as session:
            record = session.scalar(
                select(TaskRecord)
                .options(selectinload(TaskRecord.prompt_parts))
                .where(
                    TaskRecord.stage_number == stage_number,
                    TaskRecord.task_number == task_number,
                )
            )
            return None if record is None else Task.model_validate(record)

    def list_stage_tasks(self, stage_number: int) -> tuple[Task, ...]:
        with self._read() as session:
            records = session.scalars(
                select(TaskRecord)
                .options(selectinload(TaskRecord.prompt_parts))
                .where(TaskRecord.stage_number == stage_number)
                .order_by(TaskRecord.task_number)
            )
            return tuple(Task.model_validate(row) for row in records)

    def delete_task(self, stage_number: int, task_number: int) -> bool:
        with self._write() as session:
            record = session.get(TaskRecord, (stage_number, task_number))
            if record is None:
                return False
            session.delete(record)
            return True

    # Captain state transitions

    def get_captain_state(self, user_id: int) -> CaptainState:
        with self._read() as session:
            return CaptainState.model_validate(self._state(session, user_id))

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
    ) -> CaptainState:
        self._validate_position_stage(expected_position, expected_stage_number)
        self._validate_position_stage(target_position, target_stage_number)
        if target_position is CaptainPosition.NOT_STARTED:
            raise ValueError("captains cannot transition back to not_started")

        with self._write() as session:
            if source_update_id is not None:
                duplicate = session.scalar(
                    select(CaptainTransitionRecord).where(
                        CaptainTransitionRecord.source_update_id == source_update_id
                    )
                )
                if duplicate is not None:
                    if duplicate.user_id != user_id:
                        raise DuplicateUpdateError(
                            f"update {source_update_id} belongs to another captain"
                        )
                    return CaptainState.model_validate(self._state(session, user_id))

            state = self._state(session, user_id)
            if (
                state.position is not expected_position
                or state.current_stage_number != expected_stage_number
            ):
                raise StateConflictError("captain position changed")
            return self._transition(
                session,
                state,
                target_position=target_position,
                target_stage_number=target_stage_number,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=skipped_unsolved_tasks,
            )

    def claim_overdue_captains(self, now_ms: int) -> tuple[CaptainState, ...]:
        with self._write() as session:
            limit_ms = self._settings(session).time_limit_minutes * 60_000
            overdue = session.scalars(
                select(CaptainStateRecord)
                .where(
                    CaptainStateRecord.position.in_((CaptainPosition.INTRO, CaptainPosition.STAGE)),
                    CaptainStateRecord.started_at_ms.is_not(None),
                    CaptainStateRecord.started_at_ms + limit_ms <= now_ms,
                )
                .order_by(CaptainStateRecord.started_at_ms, CaptainStateRecord.user_id)
            ).all()
            return tuple(
                self._transition(
                    session,
                    state,
                    target_position=CaptainPosition.TIMED_OUT,
                    target_stage_number=None,
                    event_at_ms=now_ms,
                    recorded_at_ms=now_ms,
                    source_update_id=None,
                    skipped_unsolved_tasks=False,
                )
                for state in overdue
            )

    @staticmethod
    def _state(session: Session, user_id: int) -> CaptainStateRecord:
        state = session.get(CaptainStateRecord, user_id)
        if state is None:
            raise RecordNotFoundError(f"captain state for {user_id} does not exist")
        return state

    @staticmethod
    def _transition(
        session: Session,
        state: CaptainStateRecord,
        *,
        target_position: CaptainPosition,
        target_stage_number: int | None,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int | None,
        skipped_unsolved_tasks: bool,
    ) -> CaptainState:
        started_at_ms = state.started_at_ms
        if state.position is CaptainPosition.NOT_STARTED:
            if target_position is not CaptainPosition.INTRO:
                raise StateConflictError("the first position must be intro")
            started_at_ms = recorded_at_ms

        previous_position = state.position
        previous_stage = state.current_stage_number
        state.position = target_position
        state.started_at_ms = started_at_ms
        state.current_stage_number = target_stage_number
        sequence_number = session.scalar(
            select(func.coalesce(func.max(CaptainTransitionRecord.sequence_number), 0) + 1).where(
                CaptainTransitionRecord.user_id == state.user_id
            )
        )
        assert sequence_number is not None
        session.add(
            CaptainTransitionRecord(
                user_id=state.user_id,
                sequence_number=sequence_number,
                from_position=previous_position,
                from_stage_number=previous_stage,
                to_position=target_position,
                to_stage_number=target_stage_number,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=skipped_unsolved_tasks,
            )
        )
        session.flush()
        return CaptainState.model_validate(state)

    def list_captain_transitions(self, user_id: int) -> tuple[CaptainTransition, ...]:
        with self._read() as session:
            records = session.scalars(
                select(CaptainTransitionRecord)
                .where(CaptainTransitionRecord.user_id == user_id)
                .order_by(CaptainTransitionRecord.sequence_number)
            )
            return tuple(CaptainTransition.model_validate(row) for row in records)

    # Attempts and dynamically calculated scores

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
    ) -> RecordedAttempt:
        normalized = normalize_answer(raw_answer)
        with self._write() as session:
            duplicate = session.scalar(
                select(TaskAttemptRecord).where(
                    TaskAttemptRecord.source_update_id == source_update_id
                )
            )
            if duplicate is not None and (
                duplicate.user_id != user_id
                or duplicate.stage_number != stage_number
                or duplicate.task_number != task_number
            ):
                raise DuplicateUpdateError(f"update {source_update_id} belongs to another attempt")

            state = self._state(session, user_id)
            if (
                state.position is not CaptainPosition.STAGE
                or state.current_stage_number != stage_number
            ):
                raise StateConflictError("captain is not in the requested stage")
            task = session.get(TaskRecord, (stage_number, task_number))
            if task is None:
                raise RecordNotFoundError(f"task {stage_number}.{task_number} does not exist")
            if duplicate is not None:
                return RecordedAttempt(
                    attempt_number=duplicate.attempt_number,
                    correct=duplicate.normalized_answer == task.correct_answer_normalized,
                )

            already_solved = session.scalar(
                select(TaskAttemptRecord.attempt_id)
                .where(
                    TaskAttemptRecord.user_id == user_id,
                    TaskAttemptRecord.stage_number == stage_number,
                    TaskAttemptRecord.task_number == task_number,
                    TaskAttemptRecord.normalized_answer == task.correct_answer_normalized,
                )
                .limit(1)
            )
            if already_solved is not None:
                raise TaskAlreadySolvedError(f"task {stage_number}.{task_number} is already solved")

            attempt_number = session.scalar(
                select(func.coalesce(func.max(TaskAttemptRecord.attempt_number), 0) + 1).where(
                    TaskAttemptRecord.user_id == user_id,
                    TaskAttemptRecord.stage_number == stage_number,
                    TaskAttemptRecord.task_number == task_number,
                )
            )
            assert attempt_number is not None
            session.add(
                TaskAttemptRecord(
                    user_id=user_id,
                    stage_number=stage_number,
                    task_number=task_number,
                    attempt_number=attempt_number,
                    raw_answer=raw_answer,
                    normalized_answer=normalized,
                    event_at_ms=event_at_ms,
                    recorded_at_ms=recorded_at_ms,
                    source_update_id=source_update_id,
                )
            )
            return RecordedAttempt(
                attempt_number=attempt_number,
                correct=normalized == task.correct_answer_normalized,
            )

    def get_attempt_counts(
        self,
        user_id: int,
        stage_number: int,
    ) -> tuple[tuple[int, int], ...]:
        with self._read() as session:
            rows = session.execute(
                select(
                    TaskAttemptRecord.task_number,
                    func.max(TaskAttemptRecord.attempt_number),
                )
                .where(
                    TaskAttemptRecord.user_id == user_id,
                    TaskAttemptRecord.stage_number == stage_number,
                )
                .group_by(TaskAttemptRecord.task_number)
                .order_by(TaskAttemptRecord.task_number)
            ).tuples()
            return tuple((task_number, int(count)) for task_number, count in rows)

    def list_task_progress(self, user_id: int) -> tuple[TaskProgress, ...]:
        with self._read() as session:
            return self._task_progress(session, user_id)

    @staticmethod
    def _task_progress(session: Session, user_id: int) -> tuple[TaskProgress, ...]:
        correct_attempts = (
            select(
                TaskAttemptRecord.stage_number,
                TaskAttemptRecord.task_number,
                func.min(TaskAttemptRecord.attempt_number).label("solved_attempt_number"),
            )
            .select_from(TaskAttemptRecord)
            .join(
                TaskRecord,
                and_(
                    TaskRecord.stage_number == TaskAttemptRecord.stage_number,
                    TaskRecord.task_number == TaskAttemptRecord.task_number,
                    TaskRecord.correct_answer_normalized == TaskAttemptRecord.normalized_answer,
                ),
            )
            .where(TaskAttemptRecord.user_id == user_id)
            .group_by(TaskAttemptRecord.stage_number, TaskAttemptRecord.task_number)
            .subquery()
        )
        rows = session.execute(
            select(
                TaskRecord.stage_number,
                TaskRecord.task_number,
                correct_attempts.c.solved_attempt_number,
                func.coalesce(ScoreStepRecord.points, 0),
            )
            .outerjoin(
                correct_attempts,
                and_(
                    correct_attempts.c.stage_number == TaskRecord.stage_number,
                    correct_attempts.c.task_number == TaskRecord.task_number,
                ),
            )
            .outerjoin(
                ScoreStepRecord,
                ScoreStepRecord.attempt_number == correct_attempts.c.solved_attempt_number,
            )
            .order_by(TaskRecord.stage_number, TaskRecord.task_number)
        ).tuples()
        return tuple(
            TaskProgress(
                stage_number=stage_number,
                task_number=task_number,
                solved_attempt_number=solved_attempt_number,
                points=int(points),
            )
            for stage_number, task_number, solved_attempt_number, points in rows
        )

    def list_captain_summaries(self) -> tuple[CaptainSummary, ...]:
        with self._read() as session:
            users = session.scalars(
                select(UserRecord)
                .options(selectinload(UserRecord.state))
                .where(
                    UserRecord.role == UserRole.CAPTAIN,
                    UserRecord.active.is_(True),
                )
                .order_by(UserRecord.username.collate("NOCASE"), UserRecord.user_id)
            )
            summaries: list[CaptainSummary] = []
            for user in users:
                if user.state is None:
                    raise RecordNotFoundError(f"captain state for {user.user_id} does not exist")
                progress = self._task_progress(session, user.user_id)
                summaries.append(
                    CaptainSummary(
                        user=User.model_validate(user),
                        state=CaptainState.model_validate(user.state),
                        solved_tasks=sum(item.solved for item in progress),
                        total_tasks=len(progress),
                        total_score=sum(item.points for item in progress),
                    )
                )
            return tuple(summaries)

    @staticmethod
    def _validate_position_stage(
        position: CaptainPosition,
        stage_number: int | None,
    ) -> None:
        if position is CaptainPosition.STAGE:
            if stage_number is None or stage_number <= 0:
                raise ValueError("stage position requires a positive stage number")
        elif stage_number is not None:
            raise ValueError("only stage position may carry a stage number")
