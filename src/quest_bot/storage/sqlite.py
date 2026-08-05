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
    DeliveryStatus,
    OutroDelivery,
    OutroDeliveryPart,
    OutroKind,
    OutroWorkItem,
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
            self._ensure_captain_state_in_transaction(connection, user_id, now_ms)
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
            self._ensure_captain_state_in_transaction(connection, user_id, now_ms)
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

    def ensure_captain_state(self, user_id: int, now_ms: int) -> CaptainState:
        with self._transaction() as connection:
            self._ensure_captain_state_in_transaction(connection, user_id, now_ms)
            row = self._require_row(
                connection.execute(
                    "SELECT * FROM captain_state WHERE user_id = ?", (user_id,)
                ).fetchone(),
                "captain state",
            )
        return self._captain_state_from_row(row)

    @staticmethod
    def _ensure_captain_state_in_transaction(
        connection: sqlite3.Connection, user_id: int, now_ms: int
    ) -> None:
        connection.execute(
            """
            INSERT INTO captain_state (
                user_id, position, position_changed_at_ms
            ) VALUES (?, 'not_started', ?)
            ON CONFLICT(user_id) DO NOTHING
            """,
            (user_id, now_ms),
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
                return TransitionResult(state, duplicate, None, False)
            state = self._state_in_transaction(connection, user_id)
            if state.position is not CaptainPosition.NOT_STARTED:
                return TransitionResult(state, None, None, False)
            return self._transition_in_transaction(
                connection,
                state,
                target_position=CaptainPosition.INTRO,
                target_stage_number=None,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=False,
                timeout_deadline_at_ms=None,
                timeout_limit_minutes=None,
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
        timeout_deadline_at_ms: int | None = None,
        timeout_limit_minutes: int | None = None,
    ) -> TransitionResult:
        self._validate_position_stage(expected_position, expected_stage_number)
        self._validate_position_stage(target_position, target_stage_number)
        if target_position is CaptainPosition.NOT_STARTED:
            raise ValueError("captains cannot transition back to not_started")
        if target_position is CaptainPosition.TIMED_OUT:
            if timeout_deadline_at_ms is None or timeout_limit_minutes is None:
                raise ValueError("timeout transition requires its deadline and limit")
        elif timeout_deadline_at_ms is not None or timeout_limit_minutes is not None:
            raise ValueError("timeout metadata is valid only for timed_out")

        with self._transaction() as connection:
            if source_update_id is not None:
                duplicate = self._transition_for_update(connection, source_update_id)
                if duplicate is not None:
                    if duplicate.user_id != user_id:
                        raise DuplicateUpdateError(
                            f"update {source_update_id} belongs to another captain"
                        )
                    state = self._state_in_transaction(connection, user_id)
                    delivery = self._delivery_for_user_in_transaction(connection, user_id)
                    return TransitionResult(state, duplicate, delivery, False)
            state = self._state_in_transaction(connection, user_id)
            if (
                state.position is not expected_position
                or state.current_stage_number != expected_stage_number
            ):
                return TransitionResult(state, None, None, False)
            return self._transition_in_transaction(
                connection,
                state,
                target_position=target_position,
                target_stage_number=target_stage_number,
                event_at_ms=event_at_ms,
                recorded_at_ms=recorded_at_ms,
                source_update_id=source_update_id,
                skipped_unsolved_tasks=skipped_unsolved_tasks,
                timeout_deadline_at_ms=timeout_deadline_at_ms,
                timeout_limit_minutes=timeout_limit_minutes,
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
                        timeout_deadline_at_ms=state.started_at_ms + limit_ms,
                        timeout_limit_minutes=limit_minutes,
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
        timeout_deadline_at_ms: int | None,
        timeout_limit_minutes: int | None,
    ) -> TransitionResult:
        started_at_ms = state.started_at_ms
        if state.position is CaptainPosition.NOT_STARTED:
            if target_position is not CaptainPosition.INTRO:
                raise StateConflictError("the first position must be intro")
            started_at_ms = recorded_at_ms

        terminal_at_ms = recorded_at_ms if target_position.is_terminal else None
        connection.execute(
            """
            UPDATE captain_state
            SET position = ?,
                started_at_ms = ?,
                position_changed_at_ms = ?,
                current_stage_number = ?,
                terminal_at_ms = ?,
                timeout_deadline_at_ms = ?,
                timeout_limit_minutes = ?
            WHERE user_id = ?
            """,
            (
                target_position.value,
                started_at_ms,
                recorded_at_ms,
                target_stage_number,
                terminal_at_ms,
                timeout_deadline_at_ms,
                timeout_limit_minutes,
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
        delivery: OutroDelivery | None = None
        if target_position is CaptainPosition.FINISHED:
            delivery = self._snapshot_outro_in_transaction(
                connection, state.user_id, OutroKind.SUCCESS, recorded_at_ms
            )
        elif target_position is CaptainPosition.TIMED_OUT:
            delivery = self._snapshot_outro_in_transaction(
                connection, state.user_id, OutroKind.TIMEOUT, recorded_at_ms
            )
        return TransitionResult(
            state=new_state,
            transition=self._transition_from_row(transition_row),
            delivery=delivery,
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

    # Durable terminal-message outbox

    def _snapshot_outro_in_transaction(
        self,
        connection: sqlite3.Connection,
        user_id: int,
        kind: OutroKind,
        trigger_at_ms: int,
    ) -> OutroDelivery:
        table = _CONTENT_TABLES[kind.value]
        parts = connection.execute(f"SELECT * FROM {table} ORDER BY part_number").fetchall()
        status = DeliveryStatus.PENDING if parts else DeliveryStatus.DELIVERED
        cursor = connection.execute(
            """
            INSERT INTO outro_deliveries (
                user_id, kind, trigger_at_ms, status,
                retry_count, next_attempt_at_ms, delivered_at_ms,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                user_id,
                kind.value,
                trigger_at_ms,
                status.value,
                trigger_at_ms if parts else None,
                trigger_at_ms if not parts else None,
                trigger_at_ms,
                trigger_at_ms,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return the inserted delivery ID")
        delivery_id = cursor.lastrowid
        connection.executemany(
            """
            INSERT INTO outro_delivery_parts (
                delivery_id, part_number, content_type, data, caption,
                status, attempt_count, next_attempt_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
            """,
            (
                (
                    delivery_id,
                    int(row["part_number"]),
                    str(row["content_type"]),
                    str(row["data"]),
                    None if row["caption"] is None else str(row["caption"]),
                    trigger_at_ms,
                )
                for row in parts
            ),
        )
        delivery = self._delivery_in_transaction(connection, delivery_id)
        assert delivery is not None
        return delivery

    def get_outro_delivery(self, delivery_id: int) -> OutroDelivery | None:
        return self._delivery_in_transaction(self._connection_or_raise(), delivery_id)

    def get_outro_delivery_for_user(self, user_id: int) -> OutroDelivery | None:
        return self._delivery_for_user_in_transaction(self._connection_or_raise(), user_id)

    def get_outro_delivery_parts(self, delivery_id: int) -> tuple[OutroDeliveryPart, ...]:
        rows = self._connection_or_raise().execute(
            """
            SELECT * FROM outro_delivery_parts
            WHERE delivery_id = ?
            ORDER BY part_number
            """,
            (delivery_id,),
        )
        return tuple(self._delivery_part_from_row(row) for row in rows)

    def list_ready_outro_work(
        self, now_ms: int, *, max_attempts: int = 5, limit: int = 100
    ) -> tuple[OutroWorkItem, ...]:
        if max_attempts <= 0 or limit <= 0:
            raise ValueError("max_attempts and limit must be positive")
        rows = (
            self._connection_or_raise()
            .execute(
                """
            SELECT
                d.delivery_id AS d_delivery_id,
                d.user_id AS d_user_id,
                d.kind AS d_kind,
                d.trigger_at_ms AS d_trigger_at_ms,
                d.status AS d_status,
                d.retry_count AS d_retry_count,
                d.last_attempt_at_ms AS d_last_attempt_at_ms,
                d.next_attempt_at_ms AS d_next_attempt_at_ms,
                d.last_error AS d_last_error,
                d.delivered_at_ms AS d_delivered_at_ms,
                d.created_at_ms AS d_created_at_ms,
                d.updated_at_ms AS d_updated_at_ms,
                p.*
            FROM outro_delivery_parts AS p
            JOIN outro_deliveries AS d USING (delivery_id)
            WHERE d.status IN ('pending', 'failed')
              AND p.status IN ('pending', 'failed')
              AND p.attempt_count < ?
              AND coalesce(p.next_attempt_at_ms, 0) <= ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM outro_delivery_parts AS earlier
                  WHERE earlier.delivery_id = p.delivery_id
                    AND earlier.part_number < p.part_number
                    AND earlier.status <> 'delivered'
              )
            ORDER BY coalesce(p.next_attempt_at_ms, 0), d.delivery_id, p.part_number
            LIMIT ?
            """,
                (max_attempts, now_ms, limit),
            )
            .fetchall()
        )
        work: list[OutroWorkItem] = []
        for row in rows:
            delivery = OutroDelivery(
                delivery_id=int(row["d_delivery_id"]),
                user_id=int(row["d_user_id"]),
                kind=OutroKind(str(row["d_kind"])),
                trigger_at_ms=int(row["d_trigger_at_ms"]),
                status=DeliveryStatus(str(row["d_status"])),
                retry_count=int(row["d_retry_count"]),
                last_attempt_at_ms=self._optional_int(row["d_last_attempt_at_ms"]),
                next_attempt_at_ms=self._optional_int(row["d_next_attempt_at_ms"]),
                last_error=self._optional_str(row["d_last_error"]),
                delivered_at_ms=self._optional_int(row["d_delivered_at_ms"]),
                created_at_ms=int(row["d_created_at_ms"]),
                updated_at_ms=int(row["d_updated_at_ms"]),
            )
            work.append(OutroWorkItem(delivery, self._delivery_part_from_row(row)))
        return tuple(work)

    def mark_outro_part_sending(
        self, delivery_id: int, part_number: int, attempted_at_ms: int
    ) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outro_delivery_parts
                SET status = 'sending',
                    attempt_count = attempt_count + 1,
                    last_attempt_at_ms = ?,
                    next_attempt_at_ms = NULL,
                    last_error = NULL
                WHERE delivery_id = ? AND part_number = ?
                  AND status IN ('pending', 'failed')
                """,
                (attempted_at_ms, delivery_id, part_number),
            )
            if cursor.rowcount != 1:
                return False
            self._refresh_delivery_in_transaction(connection, delivery_id, attempted_at_ms)
            return True

    def mark_outro_part_delivered(
        self,
        delivery_id: int,
        part_number: int,
        *,
        telegram_message_id: int | None,
        delivered_at_ms: int,
    ) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE outro_delivery_parts
                SET status = 'delivered',
                    next_attempt_at_ms = NULL,
                    last_error = NULL,
                    telegram_message_id = ?,
                    delivered_at_ms = ?
                WHERE delivery_id = ? AND part_number = ?
                """,
                (
                    telegram_message_id,
                    delivered_at_ms,
                    delivery_id,
                    part_number,
                ),
            )
            if cursor.rowcount != 1:
                raise RecordNotFoundError("outro delivery part does not exist")
            self._refresh_delivery_in_transaction(connection, delivery_id, delivered_at_ms)

    def mark_outro_part_failed(
        self,
        delivery_id: int,
        part_number: int,
        *,
        error: str,
        failed_at_ms: int,
        next_attempt_at_ms: int | None,
        max_attempts: int = 5,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT attempt_count FROM outro_delivery_parts
                WHERE delivery_id = ? AND part_number = ?
                """,
                (delivery_id, part_number),
            ).fetchone()
            if row is None:
                raise RecordNotFoundError("outro delivery part does not exist")
            attempt_count = int(row["attempt_count"])
            retry_at = next_attempt_at_ms if attempt_count < max_attempts else None
            connection.execute(
                """
                UPDATE outro_delivery_parts
                SET status = 'failed',
                    next_attempt_at_ms = ?,
                    last_error = ?
                WHERE delivery_id = ? AND part_number = ?
                """,
                (retry_at, error, delivery_id, part_number),
            )
            self._refresh_delivery_in_transaction(
                connection, delivery_id, failed_at_ms, max_attempts=max_attempts
            )

    def recover_interrupted_outro_deliveries(self, now_ms: int) -> int:
        with self._transaction() as connection:
            delivery_rows = connection.execute(
                """
                SELECT DISTINCT delivery_id
                FROM outro_delivery_parts
                WHERE status = 'sending'
                """
            ).fetchall()
            cursor = connection.execute(
                """
                UPDATE outro_delivery_parts
                SET status = 'pending', next_attempt_at_ms = ?
                WHERE status = 'sending'
                """,
                (now_ms,),
            )
            for row in delivery_rows:
                self._refresh_delivery_in_transaction(connection, int(row["delivery_id"]), now_ms)
            return cursor.rowcount

    def retry_outro_for_user(self, user_id: int, now_ms: int) -> bool:
        with self._transaction() as connection:
            delivery_row = connection.execute(
                """
                SELECT delivery_id, status FROM outro_deliveries
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if delivery_row is None or str(delivery_row["status"]) == "delivered":
                return False
            delivery_id = int(delivery_row["delivery_id"])
            cursor = connection.execute(
                """
                UPDATE outro_delivery_parts
                SET status = 'pending', attempt_count = 0,
                    last_attempt_at_ms = NULL, next_attempt_at_ms = ?,
                    last_error = NULL
                WHERE delivery_id = ? AND status <> 'delivered'
                """,
                (now_ms, delivery_id),
            )
            if cursor.rowcount == 0:
                return False
            self._refresh_delivery_in_transaction(connection, delivery_id, now_ms)
            return True

    def _refresh_delivery_in_transaction(
        self,
        connection: sqlite3.Connection,
        delivery_id: int,
        now_ms: int,
        *,
        max_attempts: int = 5,
    ) -> None:
        parts = connection.execute(
            """
            SELECT * FROM outro_delivery_parts
            WHERE delivery_id = ? ORDER BY part_number
            """,
            (delivery_id,),
        ).fetchall()
        if not parts or all(str(part["status"]) == "delivered" for part in parts):
            status = DeliveryStatus.DELIVERED
            delivered_at = now_ms
            next_attempt = None
        elif any(str(part["status"]) == "sending" for part in parts):
            status = DeliveryStatus.SENDING
            delivered_at = None
            next_attempt = None
        else:
            retryable = [
                part
                for part in parts
                if str(part["status"]) != "delivered"
                and int(part["attempt_count"]) < max_attempts
                and part["next_attempt_at_ms"] is not None
            ]
            status = DeliveryStatus.PENDING if retryable else DeliveryStatus.FAILED
            delivered_at = None
            next_attempt = (
                min(int(part["next_attempt_at_ms"]) for part in retryable) if retryable else None
            )
        last_attempt_values = [
            int(part["last_attempt_at_ms"])
            for part in parts
            if part["last_attempt_at_ms"] is not None
        ]
        errors = [
            str(part["last_error"]) for part in reversed(parts) if part["last_error"] is not None
        ]
        connection.execute(
            """
            UPDATE outro_deliveries
            SET status = ?, retry_count = ?, last_attempt_at_ms = ?,
                next_attempt_at_ms = ?, last_error = ?, delivered_at_ms = ?,
                updated_at_ms = ?
            WHERE delivery_id = ?
            """,
            (
                status.value,
                sum(int(part["attempt_count"]) for part in parts),
                max(last_attempt_values) if last_attempt_values else None,
                next_attempt,
                errors[0] if errors else None,
                delivered_at,
                now_ms,
                delivery_id,
            ),
        )

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

    def _delivery_in_transaction(
        self, connection: sqlite3.Connection, delivery_id: int
    ) -> OutroDelivery | None:
        row = connection.execute(
            "SELECT * FROM outro_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        return None if row is None else self._delivery_from_row(row)

    def _delivery_for_user_in_transaction(
        self, connection: sqlite3.Connection, user_id: int
    ) -> OutroDelivery | None:
        row = connection.execute(
            "SELECT * FROM outro_deliveries WHERE user_id = ?", (user_id,)
        ).fetchone()
        return None if row is None else self._delivery_from_row(row)

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
    def _optional_str(value: object) -> str | None:
        return None if value is None else str(value)

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
            position_changed_at_ms=int(row["position_changed_at_ms"]),
            current_stage_number=SQLiteQuestStore._optional_int(row["current_stage_number"]),
            terminal_at_ms=SQLiteQuestStore._optional_int(row["terminal_at_ms"]),
            timeout_deadline_at_ms=SQLiteQuestStore._optional_int(row["timeout_deadline_at_ms"]),
            timeout_limit_minutes=SQLiteQuestStore._optional_int(row["timeout_limit_minutes"]),
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

    @staticmethod
    def _delivery_from_row(row: sqlite3.Row) -> OutroDelivery:
        return OutroDelivery(
            delivery_id=int(row["delivery_id"]),
            user_id=int(row["user_id"]),
            kind=OutroKind(str(row["kind"])),
            trigger_at_ms=int(row["trigger_at_ms"]),
            status=DeliveryStatus(str(row["status"])),
            retry_count=int(row["retry_count"]),
            last_attempt_at_ms=SQLiteQuestStore._optional_int(row["last_attempt_at_ms"]),
            next_attempt_at_ms=SQLiteQuestStore._optional_int(row["next_attempt_at_ms"]),
            last_error=SQLiteQuestStore._optional_str(row["last_error"]),
            delivered_at_ms=SQLiteQuestStore._optional_int(row["delivered_at_ms"]),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )

    @staticmethod
    def _delivery_part_from_row(row: sqlite3.Row) -> OutroDeliveryPart:
        return OutroDeliveryPart(
            delivery_id=int(row["delivery_id"]),
            part_number=int(row["part_number"]),
            content=SQLiteQuestStore._content_part_from_row(row),
            status=DeliveryStatus(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            last_attempt_at_ms=SQLiteQuestStore._optional_int(row["last_attempt_at_ms"]),
            next_attempt_at_ms=SQLiteQuestStore._optional_int(row["next_attempt_at_ms"]),
            last_error=SQLiteQuestStore._optional_str(row["last_error"]),
            telegram_message_id=SQLiteQuestStore._optional_int(row["telegram_message_id"]),
            delivered_at_ms=SQLiteQuestStore._optional_int(row["delivered_at_ms"]),
        )
