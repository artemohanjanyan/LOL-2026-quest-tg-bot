import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from quest_bot.models import CaptainPosition, ContentPart, ContentType
from quest_bot.service import QuestService
from quest_bot.storage import QuestStore
from quest_bot.storage.errors import InstanceAlreadyRunningError, TaskLimitExceededError
from tests.quest_setup import (
    BASE_TIME_MS,
    CAPTAIN_ID,
    seed_ready_quest,
    seed_users,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[QuestStore]:
    database = QuestStore.open(tmp_path / "quest.db", lock_instance=False)
    try:
        yield database
    finally:
        database.close()


def start_and_enter_first_stage(store: QuestStore) -> None:
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


def test_store_runs_migrations_and_holds_the_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "locked.db"
    with QuestStore.open(path) as first:
        assert first.schema_revision == "20260808_01"
        with pytest.raises(InstanceAlreadyRunningError):
            QuestStore.open(path)

    with QuestStore.open(path) as reopened:
        assert reopened.schema_revision == "20260808_01"


def test_database_errors_log_transaction_state_and_roll_back(
    store: QuestStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.ERROR, logger="quest_bot.storage.store"),
        pytest.raises(IntegrityError),
    ):
        store.set_time_limit(0)

    assert "in_transaction=True" in caplog.text
    assert store.get_time_limit() == 80


def test_transition_history_preserves_metadata_and_update_idempotency(
    store: QuestStore,
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


def test_timeout_claim_and_history_are_idempotent(store: QuestStore) -> None:
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


def test_captain_state_read_does_not_compete_for_writer_lock(store: QuestStore) -> None:
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


def test_task_limit_is_enforced_atomically(store: QuestStore) -> None:
    store.set_stage(1, "Лондон")
    prompt = [ContentPart(content_type=ContentType.TEXT, data="Prompt")]
    with pytest.raises(ValueError, match="prompt"):
        store.set_task(1, 1, "1", [])
    for task_number in range(1, 10):
        store.set_task(1, task_number, str(task_number), prompt)

    with pytest.raises(TaskLimitExceededError):
        store.set_task(1, 10, "10", prompt)

    store.set_task(1, 1, "updated", prompt)
    assert len(store.list_stage_tasks(1)) == 9
