from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from quest_bot.models import CaptainPosition
from quest_bot.storage.base import QuestStore
from quest_bot.storage.sqlite import SQLiteQuestStore
from tests.quest_setup import (
    BASE_TIME_MS,
    CAPTAIN_ID,
    seed_ready_quest,
    seed_users,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[QuestStore]:
    database = SQLiteQuestStore.open(tmp_path / "quest.db", lock_instance=False)
    try:
        yield database
    finally:
        database.close()


def start_and_enter_first_stage(store: QuestStore) -> None:
    started = store.start_captain(
        CAPTAIN_ID,
        event_at_ms=BASE_TIME_MS + 1_000,
        recorded_at_ms=BASE_TIME_MS + 1_001,
        source_update_id=CAPTAIN_ID * 100 + 1,
    )
    assert started.applied

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
    assert entered.applied


def test_transition_history_preserves_metadata_and_update_idempotency(
    store: QuestStore,
) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_stage(4, "Суець", BASE_TIME_MS)
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

    assert transition.applied
    assert not duplicate.applied
    assert duplicate.transition == transition.transition
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
    store.set_time_limit(1, BASE_TIME_MS)
    start_and_enter_first_stage(store)

    claimed = store.claim_overdue_captains(BASE_TIME_MS + 62_000)
    repeated = store.claim_overdue_captains(BASE_TIME_MS + 63_000)

    assert len(claimed) == 1
    assert claimed[0].state.position is CaptainPosition.TIMED_OUT
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
