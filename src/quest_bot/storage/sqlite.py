import errno
import fcntl
import logging
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from types import TracebackType
from typing import Self

from quest_bot.models import (
    CaptainPosition,
    CaptainState,
    CaptainSummary,
    CaptainTransition,
    ContentPart,
    ContentType,
    OutroKind,
    RecordedAttempt,
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
    DuplicateUpdateError,
    InstanceAlreadyRunningError,
    RecordNotFoundError,
    StateConflictError,
    StoreClosedError,
    TaskAlreadySolvedError,
    TaskLimitExceededError,
)

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_[a-z0-9_]+\.sql$")
_MIN_SQLITE_VERSION = (3, 35, 0)
LOGGER = logging.getLogger(__name__)
_INTRO_CONTENT_KIND = "intro"


class SQLiteQuestStore:
    """The single, explicitly owned SQLite connection for one bot process."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        database_path: str,
        lock_fd: int | None,
    ) -> None:
        self._connection: sqlite3.Connection | None = connection
        self._database_path = database_path
        self._lock_fd = lock_fd

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
        if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
            raise RuntimeError("SQLite 3.35 or newer is required")

        path = os.fsdecode(database_path)
        lock_fd: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            if lock_instance and path != ":memory:":
                lock_fd = cls._acquire_instance_lock(path)
            opened_connection = sqlite3.connect(path, isolation_level=None)
            connection = opened_connection
            opened_connection.row_factory = sqlite3.Row
            opened_connection.execute("PRAGMA foreign_keys = ON")
            opened_connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            opened_connection.execute("PRAGMA journal_mode = WAL")
            opened_connection.execute("PRAGMA synchronous = NORMAL")
            store = cls(opened_connection, database_path=path, lock_fd=lock_fd)
            store._run_migrations()
            return store
        except BaseException:
            if connection is not None:
                connection.close()
            if lock_fd is not None:
                cls._release_instance_lock(lock_fd)
            raise

    @staticmethod
    def _acquire_instance_lock(database_path: str) -> int:
        path = Path(database_path)
        lock_path = path.with_name(f"{path.name}.lock")
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(lock_fd)
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise InstanceAlreadyRunningError(
                    f"another bot process is already using {database_path}"
                ) from error
            raise
        return lock_fd

    @staticmethod
    def _release_instance_lock(lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    @property
    def database_path(self) -> str:
        return self._database_path

    @property
    def schema_version(self) -> int:
        row = (
            self._connection_or_raise()
            .execute("SELECT coalesce(max(version), 0) FROM schema_migrations")
            .fetchone()
        )
        assert row is not None
        return int(row[0])

    def __enter__(self) -> Self:
        self._connection_or_raise()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        try:
            connection.close()
        finally:
            if self._lock_fd is not None:
                self._release_instance_lock(self._lock_fd)
                self._lock_fd = None

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreClosedError("the quest store is closed")
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection_or_raise()
        if connection.in_transaction:
            raise RuntimeError("nested SQLiteQuestStore transaction")
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException as error:
            was_in_transaction = connection.in_transaction
            connection.rollback()
            if isinstance(error, sqlite3.Error):
                LOGGER.exception(
                    "SQLite write transaction failed",
                    extra={
                        "database_path": self._database_path,
                        "in_transaction": was_in_transaction,
                    },
                )
            raise
        else:
            connection.commit()

    def _run_migrations(self) -> None:
        connection = self._connection_or_raise()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                applied_at_ms INTEGER NOT NULL CHECK (applied_at_ms >= 0)
            ) STRICT
            """
        )
        applied = {
            int(row["version"])
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        migration_root = resources.files("quest_bot.storage.migrations")
        migrations: list[tuple[int, str, str]] = []
        for resource in migration_root.iterdir():
            match = _MIGRATION_NAME.fullmatch(resource.name)
            if match is None:
                continue
            migrations.append(
                (
                    int(match.group("version")),
                    resource.name,
                    resource.read_text(encoding="utf-8"),
                )
            )
        migrations.sort(key=lambda migration: migration[0])
        if len({version for version, _, _ in migrations}) != len(migrations):
            raise RuntimeError("duplicate SQLite migration version")

        for version, name, sql in migrations:
            if version in applied:
                continue
            with self._transaction() as transaction:
                for statement in self._split_sql_statements(sql):
                    transaction.execute(statement)
                transaction.execute(
                    """
                    INSERT INTO schema_migrations (version, name, applied_at_ms)
                    VALUES (?, ?, ?)
                    """,
                    (version, name, utc_now_ms()),
                )

    @staticmethod
    def _split_sql_statements(sql: str) -> Iterator[str]:
        buffer: list[str] = []
        for line in sql.splitlines(keepends=True):
            buffer.append(line)
            candidate = "".join(buffer)
            if sqlite3.complete_statement(candidate):
                if candidate.strip():
                    yield candidate
                buffer.clear()
        remainder = "".join(buffer)
        if remainder.strip():
            raise RuntimeError("migration ends with an incomplete SQL statement")

    # Users

    def ensure_admin(self, user_id: int, display_name: str, now_ms: int) -> User:
        return self._upsert_user(user_id, display_name, UserRole.ADMIN, now_ms)

    def add_captain(self, user_id: int, display_name: str, now_ms: int) -> User:
        if not display_name.strip():
            raise ValueError("display name must not be empty")
        with self._transaction() as connection:
            row = self._require_row(
                connection.execute(
                    """
                INSERT INTO users (
                    user_id, display_name, role, active, updated_at_ms
                ) VALUES (?, ?, 'captain', 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    role = CASE
                        WHEN users.role = 'admin' THEN 'admin'
                        ELSE 'captain'
                    END,
                    active = 1,
                    updated_at_ms = excluded.updated_at_ms
                RETURNING *
                """,
                    (user_id, display_name, now_ms),
                ).fetchone(),
                "user",
            )
            self._ensure_captain_state_in_transaction(connection, user_id)
        return self._user_from_row(row)

    def _upsert_user(
        self,
        user_id: int,
        display_name: str,
        role: UserRole,
        now_ms: int,
    ) -> User:
        if not display_name.strip():
            raise ValueError("display name must not be empty")
        with self._transaction() as connection:
            row = self._require_row(
                connection.execute(
                    """
                INSERT INTO users (
                    user_id, display_name, role, active, updated_at_ms
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    role = excluded.role,
                    active = 1,
                    updated_at_ms = excluded.updated_at_ms
                RETURNING *
                """,
                    (user_id, display_name, role.value, now_ms),
                ).fetchone(),
                "user",
            )
            self._ensure_captain_state_in_transaction(connection, user_id)
        return self._user_from_row(row)

    def deactivate_captain(self, user_id: int, now_ms: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET active = 0, updated_at_ms = ?
                WHERE user_id = ? AND role = 'captain' AND active = 1
                """,
                (now_ms, user_id),
            )
            return cursor.rowcount == 1

    def get_user(self, user_id: int) -> User | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            .fetchone()
        )
        return None if row is None else self._user_from_row(row)

    def get_user_by_display_name(self, display_name: str) -> User | None:
        row = (
            self._connection_or_raise()
            .execute(
                """
            SELECT *
            FROM users
            WHERE display_name = ? COLLATE NOCASE
            ORDER BY active DESC, updated_at_ms DESC, user_id
            LIMIT 1
            """,
                (display_name,),
            )
            .fetchone()
        )
        return None if row is None else self._user_from_row(row)

    def list_users(self, *, include_inactive: bool = True) -> tuple[User, ...]:
        sql = "SELECT * FROM users"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY role, display_name COLLATE NOCASE, user_id"
        rows = self._connection_or_raise().execute(sql).fetchall()
        return tuple(self._user_from_row(row) for row in rows)

    # Global settings and content

    def get_time_limit(self) -> int:
        row = self._require_row(
            self._connection_or_raise()
            .execute("SELECT time_limit_minutes FROM quest_settings WHERE singleton_id = 1")
            .fetchone(),
            "quest settings",
        )
        return int(row["time_limit_minutes"])

    def set_time_limit(self, minutes: int) -> int:
        with self._transaction() as connection:
            row = self._require_row(
                connection.execute(
                    """
                    UPDATE quest_settings SET time_limit_minutes = ?
                    WHERE singleton_id = 1
                    RETURNING time_limit_minutes
                    """,
                    (minutes,),
                ).fetchone(),
                "quest settings",
            )
        return int(row["time_limit_minutes"])

    def get_score_steps(self) -> tuple[int, ...]:
        rows = self._connection_or_raise().execute(
            "SELECT points FROM score_steps ORDER BY attempt_number"
        )
        return tuple(int(row["points"]) for row in rows)

    def set_score_steps(self, points: Sequence[int]) -> tuple[int, ...]:
        schedule = tuple(points)
        with self._transaction() as connection:
            connection.execute("DELETE FROM score_steps")
            connection.executemany(
                "INSERT INTO score_steps (attempt_number, points) VALUES (?, ?)",
                enumerate(schedule, start=1),
            )
        return schedule

    def get_intro_parts(self) -> tuple[ContentPart, ...]:
        return self._get_content_parts(_INTRO_CONTENT_KIND)

    def replace_intro_parts(self, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(_INTRO_CONTENT_KIND, parts)

    def get_outro_parts(self, kind: OutroKind) -> tuple[ContentPart, ...]:
        return self._get_content_parts(kind.value)

    def replace_outro_parts(self, kind: OutroKind, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(kind.value, parts)

    def _get_content_parts(self, content_kind: str) -> tuple[ContentPart, ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT * FROM global_content_parts
            WHERE content_kind = ?
            ORDER BY part_number
            """,
            (content_kind,),
        )
        return tuple(self._content_part_from_row(row) for row in rows)

    def _replace_content_parts(self, content_kind: str, parts: Sequence[ContentPart]) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM global_content_parts WHERE content_kind = ?",
                (content_kind,),
            )
            connection.executemany(
                """
                INSERT INTO global_content_parts (
                    content_kind, part_number, content_type, data, caption
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (content_kind, number, part.content_type.value, part.data, part.caption)
                    for number, part in enumerate(parts, start=1)
                ),
            )

    # Stages and tasks

    def set_stage(self, stage_number: int, name: str) -> Stage:
        with self._transaction() as connection:
            row = self._require_row(
                connection.execute(
                    """
                INSERT INTO stages (stage_number, name) VALUES (?, ?)
                ON CONFLICT(stage_number) DO UPDATE SET
                    name = excluded.name
                RETURNING *
                    """,
                    (stage_number, name),
                ).fetchone(),
                "stage",
            )
        return self._stage_from_row(row)

    def get_stage(self, stage_number: int) -> Stage | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM stages WHERE stage_number = ?", (stage_number,))
            .fetchone()
        )
        return None if row is None else self._stage_from_row(row)

    def list_stages(self) -> tuple[Stage, ...]:
        rows = self._connection_or_raise().execute("SELECT * FROM stages ORDER BY stage_number")
        return tuple(self._stage_from_row(row) for row in rows)

    def delete_stage(self, stage_number: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM stages WHERE stage_number = ?", (stage_number,)
            )
            return cursor.rowcount == 1

    def set_task(
        self,
        stage_number: int,
        task_number: int,
        correct_answers: Sequence[str],
        prompt_parts: Sequence[ContentPart],
        *,
        name: str | None = None,
    ) -> Task:
        answers = tuple(answer.strip() for answer in correct_answers)
        normalized_answers = tuple(normalize_answer(answer) for answer in answers)
        if not answers or any(not answer for answer in answers):
            raise ValueError("task must have at least one non-empty correct answer")
        if len(set(normalized_answers)) != len(normalized_answers):
            raise ValueError("task correct answers must be unique after normalization")
        normalized_name = name.strip() or None if name is not None else None
        parts = tuple(prompt_parts)
        if not parts:
            raise ValueError("task prompt must contain at least one part")

        with self._transaction() as connection:
            stage_exists = connection.execute(
                "SELECT 1 FROM stages WHERE stage_number = ?", (stage_number,)
            ).fetchone()
            if stage_exists is None:
                raise RecordNotFoundError(f"stage {stage_number} does not exist")
            existing = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            ).fetchone()
            if existing is None:
                task_count = int(
                    connection.execute(
                        "SELECT count(*) FROM tasks WHERE stage_number = ?",
                        (stage_number,),
                    ).fetchone()[0]
                )
                if task_count >= 9:
                    raise TaskLimitExceededError("a stage may contain at most nine tasks")
            task_row = self._require_row(
                connection.execute(
                    """
                INSERT INTO tasks (stage_number, task_number, name)
                VALUES (?, ?, ?)
                ON CONFLICT(stage_number, task_number) DO UPDATE SET
                    name = excluded.name
                RETURNING *
                    """,
                    (stage_number, task_number, normalized_name),
                ).fetchone(),
                "task",
            )
            connection.execute(
                """
                DELETE FROM task_correct_answers
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            )
            connection.executemany(
                """
                INSERT INTO task_correct_answers (
                    stage_number, task_number, answer_number,
                    raw_answer, normalized_answer
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    (stage_number, task_number, number, raw, normalized)
                    for number, (raw, normalized) in enumerate(
                        zip(answers, normalized_answers, strict=True), start=1
                    )
                ),
            )
            connection.execute(
                """
                DELETE FROM task_prompt_parts
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            )
            connection.executemany(
                """
                INSERT INTO task_prompt_parts (
                    stage_number, task_number, part_number,
                    content_type, data, caption
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        stage_number,
                        task_number,
                        part_number,
                        part.content_type.value,
                        part.data,
                        part.caption,
                    )
                    for part_number, part in enumerate(parts, start=1)
                ),
            )
        return self._task_from_row(task_row, answers, parts)

    def get_task(self, stage_number: int, task_number: int) -> Task | None:
        tasks = self._read_tasks(stage_number, task_number)
        return tasks[0] if tasks else None

    def list_stage_tasks(self, stage_number: int) -> tuple[Task, ...]:
        return self._read_tasks(stage_number)

    def _read_tasks(self, stage_number: int, task_number: int | None = None) -> tuple[Task, ...]:
        task_filter = "" if task_number is None else "AND tasks.task_number = ?"
        parameters = (stage_number,) if task_number is None else (stage_number, task_number)
        rows = self._connection_or_raise().execute(
            f"""
            SELECT tasks.*,
                   task_prompt_parts.content_type AS prompt_content_type,
                   task_prompt_parts.data AS prompt_data,
                   task_prompt_parts.caption AS prompt_caption
            FROM tasks
            JOIN task_prompt_parts USING (stage_number, task_number)
            WHERE tasks.stage_number = ? {task_filter}
            ORDER BY tasks.task_number, task_prompt_parts.part_number
            """,
            parameters,
        )
        grouped: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for row in rows:
            key = (int(row["stage_number"]), int(row["task_number"]))
            grouped.setdefault(key, []).append(row)
        answer_rows = self._connection_or_raise().execute(
            f"""
            SELECT task_correct_answers.*
            FROM task_correct_answers
            JOIN tasks USING (stage_number, task_number)
            WHERE tasks.stage_number = ? {task_filter}
            ORDER BY tasks.task_number, task_correct_answers.answer_number
            """,
            parameters,
        )
        answers: dict[tuple[int, int], list[str]] = {}
        for row in answer_rows:
            key = (int(row["stage_number"]), int(row["task_number"]))
            answers.setdefault(key, []).append(str(row["raw_answer"]))
        return tuple(
            self._task_from_row(
                task_rows[0],
                tuple(answers[key]),
                tuple(
                    ContentPart(
                        ContentType(str(row["prompt_content_type"])),
                        str(row["prompt_data"]),
                        None if row["prompt_caption"] is None else str(row["prompt_caption"]),
                    )
                    for row in task_rows
                ),
            )
            for key, task_rows in grouped.items()
        )

    def delete_task(self, stage_number: int, task_number: int) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM tasks
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            )
            return cursor.rowcount == 1

    # Captain state transitions

    def get_captain_state(self, user_id: int) -> CaptainState:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM captain_state WHERE user_id = ?", (user_id,))
            .fetchone()
        )
        return self._captain_state_from_row(self._require_row(row, "captain state"))

    @staticmethod
    def _ensure_captain_state_in_transaction(connection: sqlite3.Connection, user_id: int) -> None:
        connection.execute(
            """
            INSERT INTO captain_state (user_id, position)
            VALUES (?, 'not_started')
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id,),
        )

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

        with self._transaction() as connection:
            if source_update_id is not None:
                duplicate = self._transition_for_update(connection, source_update_id)
                if duplicate is not None:
                    if duplicate.user_id != user_id:
                        raise DuplicateUpdateError(
                            f"update {source_update_id} belongs to another captain"
                        )
                    return self._state_in_transaction(connection, user_id)
            state = self._state_in_transaction(connection, user_id)
            if (
                state.position is not expected_position
                or state.current_stage_number != expected_stage_number
            ):
                raise StateConflictError("captain position changed")
            return self._transition_in_transaction(
                connection,
                state,
                target_position=target_position,
                target_stage_number=target_stage_number,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=skipped_unsolved_tasks,
            )

    def reset_captain(
        self,
        expected_state: CaptainState,
        *,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int,
    ) -> CaptainState:
        with self._transaction() as connection:
            duplicate = self._transition_for_update(connection, source_update_id)
            if duplicate is not None:
                if duplicate.user_id != expected_state.user_id:
                    raise DuplicateUpdateError(
                        f"update {source_update_id} belongs to another captain"
                    )
                return self._state_in_transaction(connection, expected_state.user_id)
            state = self._state_in_transaction(connection, expected_state.user_id)
            if state != expected_state:
                raise StateConflictError("captain position changed")
            connection.execute(
                "DELETE FROM task_attempts WHERE user_id = ?",
                (state.user_id,),
            )
            return self._transition_in_transaction(
                connection,
                state,
                target_position=CaptainPosition.NOT_STARTED,
                target_stage_number=None,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=False,
            )

    def claim_overdue_captains(self, now_ms: int) -> tuple[CaptainState, ...]:
        with self._transaction() as connection:
            settings_row = self._require_row(
                connection.execute(
                    "SELECT * FROM quest_settings WHERE singleton_id = 1"
                ).fetchone(),
                "quest settings",
            )
            limit_minutes = int(settings_row["time_limit_minutes"])
            limit_ms = limit_minutes * 60_000
            rows = connection.execute(
                """
                SELECT *
                FROM captain_state
                WHERE position IN ('intro', 'stage')
                  AND started_at_ms IS NOT NULL
                  AND started_at_ms + ? <= ?
                ORDER BY started_at_ms, user_id
                """,
                (limit_ms, now_ms),
            ).fetchall()
            results: list[CaptainState] = []
            for row in rows:
                state = self._captain_state_from_row(row)
                assert state.started_at_ms is not None
                results.append(
                    self._transition_in_transaction(
                        connection,
                        state,
                        target_position=CaptainPosition.TIMED_OUT,
                        target_stage_number=None,
                        event_at_ms=now_ms,
                        recorded_at_ms=now_ms,
                        source_update_id=None,
                        skipped_unsolved_tasks=False,
                    )
                )
            return tuple(results)

    def _transition_in_transaction(
        self,
        connection: sqlite3.Connection,
        state: CaptainState,
        *,
        target_position: CaptainPosition,
        target_stage_number: int | None,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int | None,
        skipped_unsolved_tasks: bool,
    ) -> CaptainState:
        started_at_ms = state.started_at_ms
        if target_position is CaptainPosition.NOT_STARTED:
            started_at_ms = None
        elif state.position is CaptainPosition.NOT_STARTED:
            if target_position is not CaptainPosition.INTRO:
                raise StateConflictError("the first position must be intro")
            started_at_ms = recorded_at_ms

        state_row = self._require_row(
            connection.execute(
                """
            UPDATE captain_state
            SET position = ?,
                started_at_ms = ?,
                current_stage_number = ?
            WHERE user_id = ?
            RETURNING *
                """,
                (
                    target_position.value,
                    started_at_ms,
                    target_stage_number,
                    state.user_id,
                ),
            ).fetchone(),
            "captain state",
        )
        sequence_number = int(
            connection.execute(
                """
                SELECT coalesce(max(sequence_number), 0) + 1
                FROM captain_transitions WHERE user_id = ?
                """,
                (state.user_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO captain_transitions (
                user_id, sequence_number,
                from_position, from_stage_number,
                to_position, to_stage_number,
                event_at_ms, recorded_at_ms, source_update_id,
                skipped_unsolved_tasks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.user_id,
                sequence_number,
                state.position.value,
                state.current_stage_number,
                target_position.value,
                target_stage_number,
                event_at_ms,
                recorded_at_ms,
                source_update_id,
                int(skipped_unsolved_tasks),
            ),
        )
        return self._captain_state_from_row(state_row)

    def list_captain_transitions(self, user_id: int) -> tuple[CaptainTransition, ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT * FROM captain_transitions
            WHERE user_id = ?
            ORDER BY sequence_number
            """,
            (user_id,),
        )
        return tuple(self._transition_from_row(row) for row in rows)

    # Attempts and dynamic score

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
        with self._transaction() as connection:
            duplicate_row = connection.execute(
                "SELECT * FROM task_attempts WHERE source_update_id = ?",
                (source_update_id,),
            ).fetchone()
            if duplicate_row is not None and (
                int(duplicate_row["user_id"]) != user_id
                or int(duplicate_row["stage_number"]) != stage_number
                or int(duplicate_row["task_number"]) != task_number
            ):
                raise DuplicateUpdateError(f"update {source_update_id} belongs to another attempt")

            state = self._state_in_transaction(connection, user_id)
            if (
                state.position is not CaptainPosition.STAGE
                or state.current_stage_number != stage_number
            ):
                raise StateConflictError("captain is not in the requested stage")
            task_row = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            ).fetchone()
            if task_row is None:
                raise RecordNotFoundError(f"task {stage_number}.{task_number} does not exist")
            if duplicate_row is not None:
                duplicate_correct = connection.execute(
                    """
                    SELECT 1 FROM task_correct_answers
                    WHERE stage_number = ? AND task_number = ?
                      AND normalized_answer = ?
                    """,
                    (
                        stage_number,
                        task_number,
                        str(duplicate_row["normalized_answer"]),
                    ),
                ).fetchone()
                return RecordedAttempt(
                    int(duplicate_row["attempt_number"]),
                    duplicate_correct is not None,
                )
            solved = connection.execute(
                """
                SELECT 1
                FROM task_attempts
                JOIN task_correct_answers USING (stage_number, task_number, normalized_answer)
                WHERE user_id = ? AND stage_number = ? AND task_number = ?
                LIMIT 1
                """,
                (user_id, stage_number, task_number),
            ).fetchone()
            if solved is not None:
                raise TaskAlreadySolvedError(f"task {stage_number}.{task_number} is already solved")
            attempt_number = int(
                connection.execute(
                    """
                    SELECT coalesce(max(attempt_number), 0) + 1
                    FROM task_attempts
                    WHERE user_id = ? AND stage_number = ? AND task_number = ?
                    """,
                    (user_id, stage_number, task_number),
                ).fetchone()[0]
            )
            score_row = connection.execute(
                """
                SELECT points FROM score_steps
                WHERE attempt_number = ? AND points > 0
                """,
                (attempt_number,),
            ).fetchone()
            if score_row is None:
                raise AttemptLimitReachedError(
                    f"task {stage_number}.{task_number} has no scored attempts left"
                )
            row = self._require_row(
                connection.execute(
                    """
                INSERT INTO task_attempts (
                    user_id, stage_number, task_number, attempt_number,
                    raw_answer, normalized_answer,
                    event_at_ms, recorded_at_ms, source_update_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING attempt_number, normalized_answer
                    """,
                    (
                        user_id,
                        stage_number,
                        task_number,
                        attempt_number,
                        raw_answer,
                        normalized,
                        event_at_ms,
                        recorded_at_ms,
                        source_update_id,
                    ),
                ).fetchone(),
                "task attempt",
            )
            return RecordedAttempt(
                int(row["attempt_number"]),
                connection.execute(
                    """
                    SELECT 1 FROM task_correct_answers
                    WHERE stage_number = ? AND task_number = ?
                      AND normalized_answer = ?
                    """,
                    (stage_number, task_number, str(row["normalized_answer"])),
                ).fetchone()
                is not None,
            )

    def get_attempt_counts(
        self,
        user_id: int,
        stage_number: int,
    ) -> tuple[tuple[int, int], ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT task_number, max(attempt_number) AS attempt_count
            FROM task_attempts
            WHERE user_id = ? AND stage_number = ?
            GROUP BY task_number
            ORDER BY task_number
            """,
            (user_id, stage_number),
        )
        return tuple((int(row["task_number"]), int(row["attempt_count"])) for row in rows)

    def list_task_attempts(self, user_id: int) -> tuple[TaskAttempt, ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT * FROM task_attempts
            WHERE user_id = ?
            ORDER BY event_at_ms, recorded_at_ms, attempt_id
            """,
            (user_id,),
        )
        return tuple(self._task_attempt_from_row(row) for row in rows)

    def list_task_progress(self, user_id: int) -> tuple[TaskProgress, ...]:
        rows = self._connection_or_raise().execute(
            """
            WITH attempt_totals AS (
                SELECT stage_number,
                       task_number,
                       count(*) AS attempt_count,
                       max(event_at_ms) AS last_attempt_at_ms
                FROM task_attempts
                WHERE user_id = ?
                GROUP BY stage_number, task_number
            )
            SELECT task_progress.stage_number,
                   task_progress.task_number,
                   task_progress.attempt_number,
                   coalesce(score_steps.points, 0) AS points,
                   coalesce(attempt_totals.attempt_count, 0) AS attempt_count,
                   solved_attempt.event_at_ms AS solved_at_ms,
                   attempt_totals.last_attempt_at_ms
            FROM task_progress
            LEFT JOIN score_steps
              ON score_steps.attempt_number = task_progress.attempt_number
            LEFT JOIN attempt_totals
              ON attempt_totals.stage_number = task_progress.stage_number
             AND attempt_totals.task_number = task_progress.task_number
            LEFT JOIN task_attempts AS solved_attempt
              ON solved_attempt.user_id = task_progress.user_id
             AND solved_attempt.stage_number = task_progress.stage_number
             AND solved_attempt.task_number = task_progress.task_number
             AND solved_attempt.attempt_number = task_progress.attempt_number
            WHERE task_progress.user_id = ?
            ORDER BY task_progress.stage_number, task_progress.task_number
            """,
            (user_id, user_id),
        )
        return tuple(
            TaskProgress(
                stage_number=int(row["stage_number"]),
                task_number=int(row["task_number"]),
                solved_attempt_number=self._optional_int(row["attempt_number"]),
                points=int(row["points"]),
                attempt_count=int(row["attempt_count"]),
                solved_at_ms=self._optional_int(row["solved_at_ms"]),
                last_attempt_at_ms=self._optional_int(row["last_attempt_at_ms"]),
            )
            for row in rows
        )

    def list_captain_summaries(self) -> tuple[CaptainSummary, ...]:
        rows = self._connection_or_raise().execute(
            """
            WITH totals AS (
                SELECT task_progress.user_id,
                       count(*) AS total_tasks,
                       count(task_progress.attempt_number) AS solved_tasks,
                       sum(coalesce(score_steps.points, 0)) AS total_score
                FROM task_progress
                JOIN users USING (user_id)
                LEFT JOIN score_steps
                  ON score_steps.attempt_number = task_progress.attempt_number
                WHERE users.role = 'captain' AND users.active = 1
                GROUP BY task_progress.user_id
            )
            SELECT users.*,
                   captain_state.position,
                   captain_state.started_at_ms,
                   captain_state.current_stage_number,
                   coalesce(totals.solved_tasks, 0) AS solved_tasks,
                   coalesce(totals.total_tasks, (SELECT count(*) FROM tasks)) AS total_tasks,
                   coalesce(totals.total_score, 0) AS total_score
            FROM users
            JOIN captain_state USING (user_id)
            LEFT JOIN totals USING (user_id)
            WHERE users.role = 'captain' AND users.active = 1
            ORDER BY users.display_name COLLATE NOCASE, users.user_id
            """
        )
        return tuple(
            CaptainSummary(
                user=self._user_from_row(row),
                state=self._captain_state_from_row(row),
                solved_tasks=int(row["solved_tasks"]),
                total_tasks=int(row["total_tasks"]),
                total_score=int(row["total_score"]),
            )
            for row in rows
        )

    # Row mapping and validation

    @staticmethod
    def _validate_position_stage(position: CaptainPosition, stage_number: int | None) -> None:
        if position is CaptainPosition.STAGE:
            if stage_number is None or stage_number <= 0:
                raise ValueError("stage position requires a positive stage number")
        elif stage_number is not None:
            raise ValueError("only stage position may carry a stage number")

    def _state_in_transaction(self, connection: sqlite3.Connection, user_id: int) -> CaptainState:
        row = connection.execute(
            "SELECT * FROM captain_state WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"captain state for {user_id} does not exist")
        return self._captain_state_from_row(row)

    def _transition_for_update(
        self, connection: sqlite3.Connection, source_update_id: int
    ) -> CaptainTransition | None:
        row = connection.execute(
            """
            SELECT * FROM captain_transitions WHERE source_update_id = ?
            """,
            (source_update_id,),
        ).fetchone()
        return None if row is None else self._transition_from_row(row)

    @staticmethod
    def _require_row(row: sqlite3.Row | None, name: str) -> sqlite3.Row:
        if row is None:
            raise RecordNotFoundError(f"{name} does not exist")
        return row

    @staticmethod
    def _optional_int(value: object) -> int | None:
        if value is None:
            return None
        if not isinstance(value, (int, str, bytes, bytearray)):
            raise TypeError(f"expected a SQLite integer value, got {type(value)!r}")
        return int(value)

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> User:
        return User(
            user_id=int(row["user_id"]),
            display_name=str(row["display_name"]),
            role=UserRole(str(row["role"])),
            active=bool(row["active"]),
        )

    @staticmethod
    def _content_part_from_row(row: sqlite3.Row) -> ContentPart:
        return ContentPart(
            content_type=ContentType(str(row["content_type"])),
            data=str(row["data"]),
            caption=None if row["caption"] is None else str(row["caption"]),
        )

    @staticmethod
    def _stage_from_row(row: sqlite3.Row) -> Stage:
        return Stage(
            stage_number=int(row["stage_number"]),
            name=str(row["name"]),
        )

    @staticmethod
    def _task_from_row(
        row: sqlite3.Row,
        correct_answers: tuple[str, ...],
        prompt_parts: tuple[ContentPart, ...],
    ) -> Task:
        return Task(
            stage_number=int(row["stage_number"]),
            task_number=int(row["task_number"]),
            name=None if row["name"] is None else str(row["name"]),
            correct_answers=correct_answers,
            prompt_parts=prompt_parts,
        )

    @staticmethod
    def _captain_state_from_row(row: sqlite3.Row) -> CaptainState:
        return CaptainState(
            user_id=int(row["user_id"]),
            position=CaptainPosition(str(row["position"])),
            started_at_ms=SQLiteQuestStore._optional_int(row["started_at_ms"]),
            current_stage_number=SQLiteQuestStore._optional_int(row["current_stage_number"]),
        )

    @staticmethod
    def _task_attempt_from_row(row: sqlite3.Row) -> TaskAttempt:
        return TaskAttempt(
            attempt_id=int(row["attempt_id"]),
            user_id=int(row["user_id"]),
            stage_number=int(row["stage_number"]),
            task_number=int(row["task_number"]),
            attempt_number=int(row["attempt_number"]),
            raw_answer=str(row["raw_answer"]),
            normalized_answer=str(row["normalized_answer"]),
            event_at_ms=int(row["event_at_ms"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
            source_update_id=int(row["source_update_id"]),
        )

    @staticmethod
    def _transition_from_row(row: sqlite3.Row) -> CaptainTransition:
        return CaptainTransition(
            transition_id=int(row["transition_id"]),
            user_id=int(row["user_id"]),
            sequence_number=int(row["sequence_number"]),
            from_position=CaptainPosition(str(row["from_position"])),
            from_stage_number=SQLiteQuestStore._optional_int(row["from_stage_number"]),
            to_position=CaptainPosition(str(row["to_position"])),
            to_stage_number=SQLiteQuestStore._optional_int(row["to_stage_number"]),
            event_at_ms=int(row["event_at_ms"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
            source_update_id=SQLiteQuestStore._optional_int(row["source_update_id"]),
            skipped_unsolved_tasks=bool(row["skipped_unsolved_tasks"]),
        )
