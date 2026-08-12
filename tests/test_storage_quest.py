import sqlite3
from collections.abc import Iterator
from importlib import resources
from pathlib import Path

import pytest

from quest_bot.models import CaptainPosition, ContentPart, ContentType
from quest_bot.service import QuestService
from quest_bot.storage.base import TaskLimitExceededError
from quest_bot.storage.sqlite import SQLiteQuestStore
from tests.quest_setup import (
    BASE_TIME_MS,
    CAPTAIN_ID,
    seed_ready_quest,
    seed_users,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteQuestStore]:
    database = SQLiteQuestStore.open(tmp_path / "quest.db", lock_instance=False)
    try:
        yield database
    finally:
        database.close()


def start_and_enter_first_stage(store: SQLiteQuestStore) -> None:
    started = store.transition_captain(
        CAPTAIN_ID,
        expected_position=CaptainPosition.NOT_STARTED,
        expected_stage_number=None,
        target_position=CaptainPosition.INTRO,
        target_stage_number=None,
        event_at_ms=BASE_TIME_MS + 1_000,
        recorded_at_ms=BASE_TIME_MS + 1_001,
        source_update_id=CAPTAIN_ID * 100 + 1,
    )
    assert started.position is CaptainPosition.INTRO

    entered = store.transition_captain(
        CAPTAIN_ID,
        expected_position=CaptainPosition.INTRO,
        expected_stage_number=None,
        target_position=CaptainPosition.STAGE,
        target_stage_number=1,
        event_at_ms=BASE_TIME_MS + 2_000,
        recorded_at_ms=BASE_TIME_MS + 2_001,
        source_update_id=CAPTAIN_ID * 100 + 2,
    )
    assert entered.position is CaptainPosition.STAGE


def test_v1_database_migrates_users_task_names_and_correct_answers(tmp_path: Path) -> None:
    database = tmp_path / "quest.db"
    migration_root = resources.files("quest_bot.storage.migrations")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                name TEXT NOT NULL UNIQUE,
                applied_at_ms INTEGER NOT NULL CHECK (applied_at_ms >= 0)
            ) STRICT
            """
        )
        connection.executescript(
            migration_root.joinpath("001_initial.sql").read_text(encoding="utf-8")
        )
        connection.execute("INSERT INTO schema_migrations VALUES (1, '001_initial.sql', 0)")
        connection.execute(
            "INSERT INTO users VALUES (?, ?, 'captain', 1, ?)",
            (CAPTAIN_ID, "passepartout", BASE_TIME_MS),
        )
        connection.execute("INSERT INTO stages VALUES (1, 'Лондон')")
        connection.execute("INSERT INTO tasks VALUES (1, 1, '80', '80')")
        connection.execute("INSERT INTO task_prompt_parts VALUES (1, 1, 1, 'text', 'Prompt', NULL)")
        connection.execute(
            """
            INSERT INTO task_attempts (
                user_id, stage_number, task_number, attempt_number,
                raw_answer, normalized_answer, event_at_ms, recorded_at_ms, source_update_id
            ) VALUES (?, 1, 1, 1, '８０', '80', ?, ?, 1)
            """,
            (CAPTAIN_ID, BASE_TIME_MS, BASE_TIME_MS),
        )

    with SQLiteQuestStore.open(database, lock_instance=False) as migrated:
        assert migrated.schema_version == 4
        captain = migrated.get_user(CAPTAIN_ID)
        assert captain is not None
        assert captain.display_name == "@passepartout"
        task = migrated.get_task(1, 1)
        assert task is not None
        assert task.name is None
        assert task.correct_answers == ("80",)
        progress = migrated.list_task_progress(CAPTAIN_ID)
        assert len(progress) == 1
        assert progress[0].solved
        assert progress[0].points == 10

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(users)")}
    assert "display_name" in columns
    assert "username" not in columns
    assert "users_display_name_nocase_idx" in indexes
    with sqlite3.connect(database) as connection:
        task_columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        answer_rows = connection.execute(
            """
            SELECT answer_number, raw_answer, normalized_answer
            FROM task_correct_answers
            ORDER BY answer_number
            """
        ).fetchall()
    assert "name" in task_columns
    assert "correct_answer_raw" not in task_columns
    assert "correct_answer_normalized" not in task_columns
    assert answer_rows == [(1, "80", "80")]


def test_transition_history_preserves_metadata_and_update_idempotency(
    store: SQLiteQuestStore,
) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_stage(4, "Суець")
    start_and_enter_first_stage(store)

    transition = store.transition_captain(
        CAPTAIN_ID,
        expected_position=CaptainPosition.STAGE,
        expected_stage_number=1,
        target_position=CaptainPosition.STAGE,
        target_stage_number=4,
        event_at_ms=BASE_TIME_MS + 3_000,
        recorded_at_ms=BASE_TIME_MS + 3_001,
        source_update_id=105,
        skipped_unsolved_tasks=True,
    )
    duplicate = store.transition_captain(
        CAPTAIN_ID,
        expected_position=CaptainPosition.STAGE,
        expected_stage_number=1,
        target_position=CaptainPosition.STAGE,
        target_stage_number=4,
        event_at_ms=BASE_TIME_MS + 3_000,
        recorded_at_ms=BASE_TIME_MS + 3_001,
        source_update_id=105,
        skipped_unsolved_tasks=True,
    )

    assert transition == duplicate
    history = store.list_captain_transitions(CAPTAIN_ID)
    assert [item.sequence_number for item in history] == [1, 2, 3]
    assert [item.source_update_id for item in history] == [
        CAPTAIN_ID * 100 + 1,
        CAPTAIN_ID * 100 + 2,
        105,
    ]
    assert history[-1].from_position is CaptainPosition.STAGE
    assert history[-1].from_stage_number == 1
    assert history[-1].to_position is CaptainPosition.STAGE
    assert history[-1].to_stage_number == 4
    assert history[-1].event_at_ms == BASE_TIME_MS + 3_000
    assert history[-1].recorded_at_ms == BASE_TIME_MS + 3_001
    assert history[-1].skipped_unsolved_tasks


def test_timeout_claim_and_history_are_idempotent(store: SQLiteQuestStore) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_time_limit(1)
    start_and_enter_first_stage(store)

    claimed = store.claim_overdue_captains(BASE_TIME_MS + 62_000)
    repeated = store.claim_overdue_captains(BASE_TIME_MS + 63_000)

    assert len(claimed) == 1
    assert claimed[0].position is CaptainPosition.TIMED_OUT
    assert repeated == ()
    history = store.list_captain_transitions(CAPTAIN_ID)
    assert len(history) == 3
    assert history[-1].from_position is CaptainPosition.STAGE
    assert history[-1].from_stage_number == 1
    assert history[-1].to_position is CaptainPosition.TIMED_OUT
    assert history[-1].to_stage_number is None
    assert history[-1].event_at_ms == BASE_TIME_MS + 62_000
    assert history[-1].recorded_at_ms == BASE_TIME_MS + 62_000
    assert history[-1].source_update_id is None


def test_captain_state_read_does_not_compete_for_writer_lock(store: SQLiteQuestStore) -> None:
    seed_users(store)
    writer = sqlite3.connect(store.database_path, isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("BEGIN IMMEDIATE")
    try:
        status = QuestService(store, clock=lambda: BASE_TIME_MS).status(CAPTAIN_ID)
        assert status.state.position is CaptainPosition.NOT_STARTED
    finally:
        writer.rollback()
        writer.close()


def test_task_limit_is_enforced_atomically(store: SQLiteQuestStore) -> None:
    store.set_stage(1, "Лондон")
    prompt = [ContentPart(ContentType.TEXT, "Prompt")]
    with pytest.raises(ValueError, match="prompt"):
        store.set_task(1, 1, ("1",), [])
    for task_number in range(1, 10):
        store.set_task(1, task_number, (str(task_number),), prompt)

    with pytest.raises(TaskLimitExceededError):
        store.set_task(1, 10, ("10",), prompt)

    store.set_task(1, 1, ("updated",), prompt)
    assert len(store.list_stage_tasks(1)) == 9
