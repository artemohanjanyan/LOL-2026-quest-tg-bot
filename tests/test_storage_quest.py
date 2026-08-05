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
    OTHER_CAPTAIN_ID,
    seed_ready_quest,
    seed_users,
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[QuestStore]:
    database = SQLiteQuestStore.open(tmp_path / "quest.db")
    try:
        yield database
    finally:
        database.close()


def start_and_enter_first_stage(store: QuestStore, user_id: int = CAPTAIN_ID) -> None:
    started = store.start_captain(
        user_id,
        event_at_ms=BASE_TIME_MS + 1_000,
        recorded_at_ms=BASE_TIME_MS + 1_001,
        source_update_id=user_id * 100 + 1,
    )
    assert started.applied
    assert started.state.position is CaptainPosition.INTRO
    assert started.state.current_stage_number is None

    entered = store.transition_captain(
        user_id,
        expected_position=CaptainPosition.INTRO,
        expected_stage_number=None,
        target_position=CaptainPosition.STAGE,
        target_stage_number=1,
        event_at_ms=BASE_TIME_MS + 2_000,
        recorded_at_ms=BASE_TIME_MS + 2_001,
        source_update_id=user_id * 100 + 2,
    )
    assert entered.applied
    assert entered.state.position is CaptainPosition.STAGE
    assert entered.state.current_stage_number == 1


def test_start_enters_intro_before_any_numbered_stage(store: QuestStore) -> None:
    seed_users(store)
    seed_ready_quest(store)

    result = store.start_captain(
        CAPTAIN_ID,
        event_at_ms=BASE_TIME_MS + 100,
        recorded_at_ms=BASE_TIME_MS + 101,
        source_update_id=10,
    )

    assert result.applied
    assert result.state.position is CaptainPosition.INTRO
    assert result.state.current_stage_number is None
    transitions = store.list_captain_transitions(CAPTAIN_ID)
    assert [transition.to_position for transition in transitions] == [CaptainPosition.INTRO]


def test_named_stage_contains_only_ordered_task_prompts(store: QuestStore) -> None:
    seed_users(store)
    seed_ready_quest(store)

    stage = store.get_stage(1)
    tasks = store.list_stage_tasks(1)

    assert stage is not None
    assert stage.name == "Лондон"
    assert [item.task.task_number for item in tasks] == [1, 3]
    assert [part.data for part in tasks[0].prompt_parts] == ["TASK ONE PROMPT: How many days?"]
    assert [part.data for part in tasks[1].prompt_parts] == [
        "TASK THREE PROMPT: Name the traveller",
        "telegram-pdf-id",
        "telegram-video-id",
    ]


def test_attempt_number_drives_score_and_content_edits_recalculate_it(
    store: QuestStore,
) -> None:
    seed_users(store)
    seed_ready_quest(store)
    start_and_enter_first_stage(store)

    first = store.record_attempt(
        CAPTAIN_ID,
        1,
        1,
        "81",
        event_at_ms=BASE_TIME_MS + 3_000,
        recorded_at_ms=BASE_TIME_MS + 3_001,
        source_update_id=103,
    )
    second = store.record_attempt(
        CAPTAIN_ID,
        1,
        1,
        "  ８０  ",
        event_at_ms=BASE_TIME_MS + 4_000,
        recorded_at_ms=BASE_TIME_MS + 4_001,
        source_update_id=104,
    )

    assert first.attempt.attempt_number == 1
    assert second.attempt.attempt_number == 2
    progress = store.get_stage_progress(CAPTAIN_ID, 1)
    assert progress[0].solved_attempt_number == 2
    assert progress[0].points == 8
    assert store.get_total_score(CAPTAIN_ID) == 8

    store.set_score_steps((12, 7, 0))
    assert store.get_total_score(CAPTAIN_ID) == 7

    original = store.get_task(1, 1)
    assert original is not None
    store.set_task(
        1,
        1,
        "81",
        original.prompt_parts,
        BASE_TIME_MS + 5_000,
    )
    recalculated = store.get_stage_progress(CAPTAIN_ID, 1)
    assert recalculated[0].solved_attempt_number == 1
    assert recalculated[0].points == 12


def test_deleting_and_recreating_task_reconnects_retained_attempts(
    store: QuestStore,
) -> None:
    seed_users(store)
    seed_ready_quest(store)
    start_and_enter_first_stage(store)
    original = store.get_task(1, 1)
    assert original is not None
    store.record_attempt(
        CAPTAIN_ID,
        1,
        1,
        "81",
        event_at_ms=BASE_TIME_MS + 3_000,
        recorded_at_ms=BASE_TIME_MS + 3_001,
        source_update_id=107,
    )

    assert store.delete_task(1, 1)
    assert len(store.list_attempts(CAPTAIN_ID, stage_number=1, task_number=1)) == 1
    store.set_task(1, 1, "81", original.prompt_parts, BASE_TIME_MS + 4_000)

    progress = store.get_stage_progress(CAPTAIN_ID, 1)
    recreated = next(item for item in progress if item.task.task_number == 1)
    assert recreated.solved_attempt_number == 1
    assert recreated.points == 10


def test_skip_transition_records_that_unsolved_tasks_were_left_behind(
    store: QuestStore,
) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_stage(4, "Суець", BASE_TIME_MS)
    start_and_enter_first_stage(store)

    result = store.transition_captain(
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

    assert result.state.current_stage_number == 4
    assert store.list_captain_transitions(CAPTAIN_ID)[-1].skipped_unsolved_tasks


def test_timeout_is_claimed_by_sweep(store: QuestStore) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_time_limit(1, BASE_TIME_MS)
    start_and_enter_first_stage(store)

    # Passing the mathematical deadline does not itself change state or reject
    # an otherwise valid command. The periodic sweep is the authority.
    attempt = store.record_attempt(
        CAPTAIN_ID,
        1,
        1,
        "not yet",
        event_at_ms=BASE_TIME_MS + 61_001,
        recorded_at_ms=BASE_TIME_MS + 61_002,
        source_update_id=106,
    )
    assert attempt.created
    active_state = store.get_captain_state(CAPTAIN_ID)
    assert active_state is not None
    assert active_state.position is CaptainPosition.STAGE

    claimed = store.claim_overdue_captains(BASE_TIME_MS + 61_003)

    assert len(claimed) == 1
    assert claimed[0].state.position is CaptainPosition.TIMED_OUT


def test_finishing_during_sweep_grace_reaches_terminal_state(store: QuestStore) -> None:
    seed_users(store)
    seed_ready_quest(store)
    store.set_time_limit(1, BASE_TIME_MS)
    store.add_captain(OTHER_CAPTAIN_ID, "fix", BASE_TIME_MS)
    start_and_enter_first_stage(store, OTHER_CAPTAIN_ID)

    # No sweep has claimed this captain, so a finish just after the mathematical
    # deadline is still valid by design.
    finished = store.transition_captain(
        OTHER_CAPTAIN_ID,
        expected_position=CaptainPosition.STAGE,
        expected_stage_number=1,
        target_position=CaptainPosition.FINISHED,
        target_stage_number=None,
        event_at_ms=BASE_TIME_MS + 61_500,
        recorded_at_ms=BASE_TIME_MS + 61_501,
        source_update_id=OTHER_CAPTAIN_ID * 100 + 3,
    )

    assert finished.state.position is CaptainPosition.FINISHED
