from __future__ import annotations

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
    AttemptResult,
    CaptainPosition,
    CaptainState,
    CaptainSummary,
    CaptainTransition,
    ContentPart,
    ContentType,
    OutroKind,
    QuestSettings,
    Stage,
    Task,
    TaskAttempt,
    TaskContent,
    TaskProgress,
    TransitionResult,
    User,
    UserRole,
    utc_now_ms,
)
from quest_bot.normalization import normalize_answer
from quest_bot.storage.base import (
    DuplicateUpdateError,
    InstanceAlreadyRunningError,
    RecordNotFoundError,
    StateConflictError,
    StoreClosedError,
    TaskAlreadySolvedError,
)

_MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]+)_[a-z0-9_]+\.sql$")
LOGGER = logging.getLogger(__name__)
_CONTENT_TABLES = {
    "intro": "quest_intro_parts",
    OutroKind.SUCCESS.value: "success_outro_parts",
    OutroKind.TIMEOUT.value: "timeout_outro_parts",
}


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

        path = os.fspath(database_path)
        lock_fd: int | None = None
        connection: sqlite3.Connection | None = None
        try:
            if lock_instance and path != ":memory:":
                lock_fd = cls._acquire_instance_lock(path)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            store = cls(connection, database_path=path, lock_fd=lock_fd)
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

    def ensure_admin(self, user_id: int, username: str, now_ms: int) -> User:
        return self._upsert_user(user_id, username, UserRole.ADMIN, now_ms)

    def add_captain(self, user_id: int, username: str, now_ms: int) -> User:
        if not username.strip():
            raise ValueError("username must not be empty")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    user_id, username, role, active, created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'captain', 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    role = CASE
                        WHEN users.role = 'admin' THEN 'admin'
                        ELSE 'captain'
                    END,
                    active = 1,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (user_id, username, now_ms, now_ms),
            )
            self._ensure_captain_state_in_transaction(connection, user_id)
            row = self._require_row(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone(),
                "user",
            )
        return self._user_from_row(row)

    def _upsert_user(self, user_id: int, username: str, role: UserRole, now_ms: int) -> User:
        if not username.strip():
            raise ValueError("username must not be empty")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    user_id, username, role, active, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    role = excluded.role,
                    active = 1,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (user_id, username, role.value, now_ms, now_ms),
            )
            self._ensure_captain_state_in_transaction(connection, user_id)
            row = self._require_row(
                connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone(),
                "user",
            )
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

    def get_user_by_username(self, username: str) -> User | None:
        normalized_username = username.removeprefix("@")
        row = (
            self._connection_or_raise()
            .execute(
                """
            SELECT *
            FROM users
            WHERE username = ? COLLATE NOCASE
            ORDER BY active DESC, updated_at_ms DESC, user_id
            LIMIT 1
            """,
                (normalized_username,),
            )
            .fetchone()
        )
        return None if row is None else self._user_from_row(row)

    def list_users(self, *, include_inactive: bool = True) -> tuple[User, ...]:
        sql = "SELECT * FROM users"
        if not include_inactive:
            sql += " WHERE active = 1"
        sql += " ORDER BY role, username COLLATE NOCASE, user_id"
        rows = self._connection_or_raise().execute(sql).fetchall()
        return tuple(self._user_from_row(row) for row in rows)

    # Global settings and content

    def get_settings(self) -> QuestSettings:
        row = self._require_row(
            self._connection_or_raise()
            .execute("SELECT * FROM quest_settings WHERE singleton_id = 1")
            .fetchone(),
            "quest settings",
        )
        return QuestSettings(
            time_limit_minutes=int(row["time_limit_minutes"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )

    def set_time_limit(self, minutes: int, now_ms: int) -> QuestSettings:
        if minutes <= 0:
            raise ValueError("time limit must be positive")
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE quest_settings
                SET time_limit_minutes = ?, updated_at_ms = ?
                WHERE singleton_id = 1
                """,
                (minutes, now_ms),
            )
        return self.get_settings()

    def get_score_steps(self) -> tuple[int, ...]:
        rows = self._connection_or_raise().execute(
            "SELECT points FROM score_steps ORDER BY attempt_number"
        )
        return tuple(int(row["points"]) for row in rows)

    def set_score_steps(self, points: Sequence[int]) -> tuple[int, ...]:
        schedule = tuple(points)
        if not schedule:
            raise ValueError("score schedule must not be empty")
        if any(point < 0 for point in schedule):
            raise ValueError("score points must not be negative")
        if any(left < right for left, right in zip(schedule[:-1], schedule[1:], strict=True)):
            raise ValueError("score schedule must be non-increasing")
        if schedule[-1] != 0:
            raise ValueError("score schedule must end in zero")
        with self._transaction() as connection:
            connection.execute("DELETE FROM score_steps")
            connection.executemany(
                "INSERT INTO score_steps (attempt_number, points) VALUES (?, ?)",
                enumerate(schedule, start=1),
            )
        return schedule

    def get_intro_parts(self) -> tuple[ContentPart, ...]:
        return self._get_content_parts(_CONTENT_TABLES["intro"])

    def replace_intro_parts(self, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(_CONTENT_TABLES["intro"], parts)

    def get_outro_parts(self, kind: OutroKind) -> tuple[ContentPart, ...]:
        return self._get_content_parts(_CONTENT_TABLES[kind.value])

    def replace_outro_parts(self, kind: OutroKind, parts: Sequence[ContentPart]) -> None:
        self._replace_content_parts(_CONTENT_TABLES[kind.value], parts)

    def _get_content_parts(self, table: str) -> tuple[ContentPart, ...]:
        rows = self._connection_or_raise().execute(f"SELECT * FROM {table} ORDER BY part_number")
        return tuple(self._content_part_from_row(row) for row in rows)

    def _replace_content_parts(self, table: str, parts: Sequence[ContentPart]) -> None:
        validated_parts = self._validate_parts(parts)
        with self._transaction() as connection:
            connection.execute(f"DELETE FROM {table}")
            connection.executemany(
                f"""
                INSERT INTO {table} (
                    part_number, content_type, data, caption
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    (number, part.content_type.value, part.data, part.caption)
                    for number, part in enumerate(validated_parts, start=1)
                ),
            )

    # Stages and tasks

    def set_stage(self, stage_number: int, name: str, now_ms: int) -> Stage:
        if stage_number <= 0:
            raise ValueError("stage number must be positive")
        if not name.strip():
            raise ValueError("stage name must not be empty")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO stages (
                    stage_number, name, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(stage_number) DO UPDATE SET
                    name = excluded.name,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (stage_number, name, now_ms, now_ms),
            )
        stage = self.get_stage(stage_number)
        assert stage is not None
        return stage

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
        correct_answer: str,
        prompt_parts: Sequence[ContentPart],
        now_ms: int,
    ) -> TaskContent:
        if stage_number <= 0 or task_number <= 0:
            raise ValueError("stage and task numbers must be positive")
        normalized_answer = normalize_answer(correct_answer)
        if not normalized_answer:
            raise ValueError("correct answer must not be empty")
        validated_parts = self._validate_parts(prompt_parts)
        if not validated_parts:
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
                    raise ValueError("a stage may contain at most nine tasks")
            connection.execute(
                """
                INSERT INTO tasks (
                    stage_number, task_number,
                    correct_answer_raw, correct_answer_normalized,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(stage_number, task_number) DO UPDATE SET
                    correct_answer_raw = excluded.correct_answer_raw,
                    correct_answer_normalized = excluded.correct_answer_normalized,
                    updated_at_ms = excluded.updated_at_ms
                """,
                (
                    stage_number,
                    task_number,
                    correct_answer,
                    normalized_answer,
                    now_ms,
                    now_ms,
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
                    for part_number, part in enumerate(validated_parts, start=1)
                ),
            )
        task = self.get_task(stage_number, task_number)
        assert task is not None
        return task

    def get_task(self, stage_number: int, task_number: int) -> TaskContent | None:
        connection = self._connection_or_raise()
        row = connection.execute(
            """
            SELECT * FROM tasks
            WHERE stage_number = ? AND task_number = ?
            """,
            (stage_number, task_number),
        ).fetchone()
        if row is None:
            return None
        prompt_rows = connection.execute(
            """
            SELECT * FROM task_prompt_parts
            WHERE stage_number = ? AND task_number = ?
            ORDER BY part_number
            """,
            (stage_number, task_number),
        )
        return TaskContent(
            task=self._task_from_row(row),
            prompt_parts=tuple(
                self._content_part_from_row(prompt_row) for prompt_row in prompt_rows
            ),
        )

    def list_stage_tasks(self, stage_number: int) -> tuple[TaskContent, ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT task_number FROM tasks
            WHERE stage_number = ?
            ORDER BY task_number
            """,
            (stage_number,),
        )
        tasks: list[TaskContent] = []
        for row in rows:
            content = self.get_task(stage_number, int(row["task_number"]))
            assert content is not None
            tasks.append(content)
        return tuple(tasks)

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

    def get_captain_state(self, user_id: int) -> CaptainState | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM captain_state WHERE user_id = ?", (user_id,))
            .fetchone()
        )
        return None if row is None else self._captain_state_from_row(row)

    def ensure_captain_state(self, user_id: int) -> CaptainState:
        with self._transaction() as connection:
            self._ensure_captain_state_in_transaction(connection, user_id)
            row = self._require_row(
                connection.execute(
                    "SELECT * FROM captain_state WHERE user_id = ?", (user_id,)
                ).fetchone(),
                "captain state",
            )
        return self._captain_state_from_row(row)

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

    def start_captain(
        self,
        user_id: int,
        *,
        event_at_ms: int,
        recorded_at_ms: int,
        source_update_id: int,
    ) -> TransitionResult:
        with self._transaction() as connection:
            duplicate = self._transition_for_update(connection, source_update_id)
            if duplicate is not None:
                if duplicate.user_id != user_id:
                    raise DuplicateUpdateError(
                        f"update {source_update_id} belongs to another captain"
                    )
                state = self._state_in_transaction(connection, user_id)
                return TransitionResult(state, duplicate, False)
            state = self._state_in_transaction(connection, user_id)
            if state.position is not CaptainPosition.NOT_STARTED:
                return TransitionResult(state, None, False)
            return self._transition_in_transaction(
                connection,
                state,
                target_position=CaptainPosition.INTRO,
                target_stage_number=None,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=False,
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
    ) -> TransitionResult:
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
                    state = self._state_in_transaction(connection, user_id)
                    return TransitionResult(state, duplicate, False)
            state = self._state_in_transaction(connection, user_id)
            if (
                state.position is not expected_position
                or state.current_stage_number != expected_stage_number
            ):
                return TransitionResult(state, None, False)
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

    def claim_overdue_captains(self, now_ms: int) -> tuple[TransitionResult, ...]:
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
            results: list[TransitionResult] = []
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
    ) -> TransitionResult:
        started_at_ms = state.started_at_ms
        if state.position is CaptainPosition.NOT_STARTED:
            if target_position is not CaptainPosition.INTRO:
                raise StateConflictError("the first position must be intro")
            started_at_ms = recorded_at_ms

        connection.execute(
            """
            UPDATE captain_state
            SET position = ?,
                started_at_ms = ?,
                current_stage_number = ?
            WHERE user_id = ?
            """,
            (
                target_position.value,
                started_at_ms,
                target_stage_number,
                state.user_id,
            ),
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
        cursor = connection.execute(
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
        transition_row = self._require_row(
            connection.execute(
                "SELECT * FROM captain_transitions WHERE transition_id = ?",
                (cursor.lastrowid,),
            ).fetchone(),
            "captain transition",
        )
        new_state = self._state_in_transaction(connection, state.user_id)
        return TransitionResult(
            state=new_state,
            transition=self._transition_from_row(transition_row),
            applied=True,
        )

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
    ) -> AttemptResult:
        normalized = normalize_answer(raw_answer)
        if not normalized:
            raise ValueError("answer must not be empty")
        with self._transaction() as connection:
            duplicate_row = connection.execute(
                "SELECT * FROM task_attempts WHERE source_update_id = ?",
                (source_update_id,),
            ).fetchone()
            if duplicate_row is not None:
                duplicate = self._attempt_from_row(duplicate_row)
                if (
                    duplicate.user_id != user_id
                    or duplicate.stage_number != stage_number
                    or duplicate.task_number != task_number
                ):
                    raise DuplicateUpdateError(
                        f"update {source_update_id} belongs to another attempt"
                    )
                return AttemptResult(duplicate, False)

            state = self._state_in_transaction(connection, user_id)
            if (
                state.position is not CaptainPosition.STAGE
                or state.current_stage_number != stage_number
            ):
                raise StateConflictError("captain is not in the requested stage")
            task_row = connection.execute(
                """
                SELECT * FROM tasks
                WHERE stage_number = ? AND task_number = ?
                """,
                (stage_number, task_number),
            ).fetchone()
            if task_row is None:
                raise RecordNotFoundError(f"task {stage_number}.{task_number} does not exist")
            solved = connection.execute(
                """
                SELECT 1
                FROM task_attempts
                WHERE user_id = ? AND stage_number = ? AND task_number = ?
                  AND normalized_answer = ?
                LIMIT 1
                """,
                (
                    user_id,
                    stage_number,
                    task_number,
                    str(task_row["correct_answer_normalized"]),
                ),
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
            cursor = connection.execute(
                """
                INSERT INTO task_attempts (
                    user_id, stage_number, task_number, attempt_number,
                    raw_answer, normalized_answer,
                    event_at_ms, recorded_at_ms, source_update_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            )
            row = self._require_row(
                connection.execute(
                    "SELECT * FROM task_attempts WHERE attempt_id = ?",
                    (cursor.lastrowid,),
                ).fetchone(),
                "task attempt",
            )
            return AttemptResult(self._attempt_from_row(row), True)

    def list_attempts(
        self,
        user_id: int,
        *,
        stage_number: int | None = None,
        task_number: int | None = None,
    ) -> tuple[TaskAttempt, ...]:
        if task_number is not None and stage_number is None:
            raise ValueError("task_number requires stage_number")
        clauses = ["user_id = ?"]
        parameters: list[int] = [user_id]
        if stage_number is not None:
            clauses.append("stage_number = ?")
            parameters.append(stage_number)
        if task_number is not None:
            clauses.append("task_number = ?")
            parameters.append(task_number)
        rows = self._connection_or_raise().execute(
            f"""
            SELECT * FROM task_attempts
            WHERE {" AND ".join(clauses)}
            ORDER BY stage_number, task_number, attempt_number
            """,
            parameters,
        )
        return tuple(self._attempt_from_row(row) for row in rows)

    def get_stage_progress(self, user_id: int, stage_number: int) -> tuple[TaskProgress, ...]:
        connection = self._connection_or_raise()
        tasks = self.list_stage_tasks(stage_number)
        progress: list[TaskProgress] = []
        for task_content in tasks:
            task = task_content.task
            solving_row = connection.execute(
                """
                SELECT min(attempt_number) AS attempt_number
                FROM task_attempts
                WHERE user_id = ? AND stage_number = ? AND task_number = ?
                  AND normalized_answer = ?
                """,
                (
                    user_id,
                    stage_number,
                    task.task_number,
                    task.correct_answer_normalized,
                ),
            ).fetchone()
            attempt_value = solving_row["attempt_number"]
            solving_attempt = None if attempt_value is None else int(attempt_value)
            points = 0
            if solving_attempt is not None:
                score_row = connection.execute(
                    """
                    SELECT points FROM score_steps WHERE attempt_number = ?
                    """,
                    (solving_attempt,),
                ).fetchone()
                if score_row is not None:
                    points = int(score_row["points"])
            progress.append(TaskProgress(task, solving_attempt, points))
        return tuple(progress)

    def get_total_score(self, user_id: int) -> int:
        return sum(
            task.points
            for stage in self.list_stages()
            for task in self.get_stage_progress(user_id, stage.stage_number)
        )

    def list_captain_summaries(
        self,
        *,
        include_admins: bool = False,
        include_inactive: bool = False,
    ) -> tuple[CaptainSummary, ...]:
        users = self.list_users(include_inactive=include_inactive)
        all_stages = self.list_stages()
        summaries: list[CaptainSummary] = []
        for user in users:
            if user.role is UserRole.ADMIN and not include_admins:
                continue
            state = self.get_captain_state(user.user_id)
            if state is None:
                continue
            progress = tuple(
                task
                for stage in all_stages
                for task in self.get_stage_progress(user.user_id, stage.stage_number)
            )
            summaries.append(
                CaptainSummary(
                    user=user,
                    state=state,
                    solved_tasks=sum(task.solved for task in progress),
                    total_tasks=len(progress),
                    total_score=sum(task.points for task in progress),
                )
            )
        return tuple(summaries)

    # Row mapping and validation

    @staticmethod
    def _validate_parts(parts: Sequence[ContentPart]) -> tuple[ContentPart, ...]:
        result = tuple(parts)
        for part in result:
            if not part.data:
                raise ValueError("content part data must not be empty")
        return result

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
            username=str(row["username"]),
            role=UserRole(str(row["role"])),
            active=bool(row["active"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
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
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        return Task(
            stage_number=int(row["stage_number"]),
            task_number=int(row["task_number"]),
            correct_answer_raw=str(row["correct_answer_raw"]),
            correct_answer_normalized=str(row["correct_answer_normalized"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
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

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> TaskAttempt:
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
