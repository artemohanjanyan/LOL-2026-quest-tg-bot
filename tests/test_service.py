from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from quest_bot.errors import DeliveryFailure, InvalidQuestState, NotAuthorized
from quest_bot.models import CaptainPosition, ContentPart, DeliveryStatus, OutroKind
from quest_bot.service import QuestService
from quest_bot.storage.base import QuestStore
from quest_bot.storage.sqlite import SQLiteQuestStore
from tests.quest_setup import (
    ADMIN_ID,
    BASE_TIME_MS,
    CAPTAIN_ID,
    seed_ready_quest,
    seed_second_stage,
    seed_users,
)


@dataclass(slots=True)
class MutableClock:
    now_ms: int = BASE_TIME_MS

    def __call__(self) -> int:
        return self.now_ms


@dataclass(slots=True)
class RecordingOutroSender:
    sent: list[tuple[int, ContentPart]] = field(default_factory=list)

    async def send_outro_part(self, chat_id: int, part: ContentPart) -> int:
        self.sent.append((chat_id, part))
        return 1_000 + len(self.sent)


@dataclass(slots=True)
class FailingOutroSender:
    calls: int = 0

    async def send_outro_part(self, chat_id: int, part: ContentPart) -> int:
        del chat_id, part
        self.calls += 1
        raise DeliveryFailure("Telegram remains unavailable")


@pytest.fixture
def service_and_store(tmp_path: Path) -> Iterator[tuple[QuestService, QuestStore, MutableClock]]:
    store = SQLiteQuestStore.open(tmp_path / "quest.db", lock_instance=False)
    clock = MutableClock()
    seed_users(store)
    seed_ready_quest(store)
    service = QuestService(store, clock=clock)
    try:
        yield service, store, clock
    finally:
        store.close()


def test_start_starts_clock_and_returns_only_the_intro(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, clock = service_and_store

    started = service.start(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms,
        source_update_id=1,
    )
    repeated = service.start(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 10_000,
        source_update_id=2,
    )

    assert started.started
    assert started.state.position is CaptainPosition.INTRO
    assert started.state.current_stage_number is None
    assert [part.data for part in started.intro_parts] == ["INTRO: Pack your carpetbag"]
    assert not repeated.started
    assert repeated.state.started_at_ms == started.state.started_at_ms


def test_advance_from_intro_presents_named_stage_and_all_task_prompts(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, clock = service_and_store
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=10)

    result = service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=11,
    )

    assert result.applied
    assert result.state.position is CaptainPosition.STAGE
    assert result.presentation is not None
    assert result.presentation.stage.name == "Лондон"
    assert [task.task.task_number for task in result.presentation.tasks] == [1, 3]
    assert [part.data for part in result.presentation.tasks[0].prompt_parts] == [
        "TASK ONE PROMPT: How many days?"
    ]
    assert [part.data for part in result.presentation.tasks[1].prompt_parts] == [
        "TASK THREE PROMPT: Name the traveller",
        "telegram-pdf-id",
        "telegram-video-id",
    ]


def test_answer_attempts_use_normalized_canonical_answer_and_score_step(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, clock = service_and_store
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=20)
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=21,
    )

    wrong = service.answer(
        CAPTAIN_ID,
        1,
        "eighty",
        event_at_ms=clock.now_ms + 2_000,
        source_update_id=22,
    )
    correct = service.answer(
        CAPTAIN_ID,
        1,
        " ８０ ",
        event_at_ms=clock.now_ms + 3_000,
        source_update_id=23,
    )

    assert not wrong.correct
    assert wrong.attempt.attempt_number == 1
    assert wrong.points == 0
    assert correct.correct
    assert correct.attempt.attempt_number == 2
    assert correct.points == 8
    assert service.status(CAPTAIN_ID).total_score == 8
    with pytest.raises(InvalidQuestState, match="already solved"):
        service.answer(
            CAPTAIN_ID,
            1,
            "80",
            event_at_ms=clock.now_ms + 4_000,
            source_update_id=24,
        )


def test_unresolved_stage_requires_explicit_confirmation_before_advance(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, store, clock = service_and_store
    seed_second_stage(store)
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=30)
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=31,
    )

    warning = service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 2_000,
        source_update_id=32,
    )

    assert warning.needs_confirmation
    assert warning.unsolved_task_numbers == (1, 3)
    assert not warning.applied
    assert warning.state.current_stage_number == 1

    confirmed = service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 3_000,
        source_update_id=33,
        confirm_skip=True,
    )
    assert confirmed.applied
    assert confirmed.state.current_stage_number == 4
    assert confirmed.presentation is not None
    assert confirmed.presentation.stage.name == "Суець"
    assert store.list_captain_transitions(CAPTAIN_ID)[-1].skipped_unsolved_tasks


@pytest.mark.asyncio
async def test_overdue_commands_work_until_sweep_claims_timeout(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, clock = service_and_store
    service.set_time_limit(ADMIN_ID, 1)
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=40)
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=41,
    )

    clock.now_ms += 60_001
    late_answer = service.answer(
        CAPTAIN_ID,
        1,
        "still travelling",
        event_at_ms=clock.now_ms,
        source_update_id=42,
    )
    assert late_answer.created
    assert service.status(CAPTAIN_ID).state.position is CaptainPosition.STAGE

    sender = RecordingOutroSender()
    sweep = await service.sweep_and_deliver(sender)

    assert sweep.expired_captains == 1
    assert sweep.delivered_parts == 1
    assert service.status(CAPTAIN_ID).state.position is CaptainPosition.TIMED_OUT
    assert [(chat_id, part.data) for chat_id, part in sender.sent] == [
        (CAPTAIN_ID, "TIMEOUT OUTRO: The clock wins")
    ]
    with pytest.raises(InvalidQuestState, match="terminal"):
        service.answer(
            CAPTAIN_ID,
            1,
            "80",
            event_at_ms=clock.now_ms + 1,
            source_update_id=43,
        )


@pytest.mark.asyncio
async def test_finish_during_sweep_grace_uses_success_outro(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, clock = service_and_store
    service.set_time_limit(ADMIN_ID, 1)
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=50)
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=51,
    )

    clock.now_ms += 60_001
    finished = service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms,
        source_update_id=52,
        confirm_skip=True,
    )
    assert finished.finished
    assert finished.state.position is CaptainPosition.FINISHED

    sender = RecordingOutroSender()
    sweep = await service.sweep_and_deliver(sender)

    assert sweep.expired_captains == 0
    assert [(chat_id, part.data) for chat_id, part in sender.sent] == [
        (CAPTAIN_ID, "SUCCESS OUTRO: Reform Club reached")
    ]
    delivery = service.store.get_outro_delivery_for_user(CAPTAIN_ID)
    assert delivery is not None
    assert delivery.kind is OutroKind.SUCCESS


@pytest.mark.asyncio
async def test_outro_delivery_stops_after_five_automatic_attempts(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, store, clock = service_and_store
    service.start(CAPTAIN_ID, event_at_ms=clock.now_ms, source_update_id=60)
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 1_000,
        source_update_id=61,
    )
    service.advance(
        CAPTAIN_ID,
        event_at_ms=clock.now_ms + 2_000,
        source_update_id=62,
        confirm_skip=True,
    )
    sender = FailingOutroSender()

    for _ in range(5):
        await service.sweep_and_deliver(sender)
        clock.now_ms += 60_000

    delivery = store.get_outro_delivery_for_user(CAPTAIN_ID)
    assert delivery is not None
    assert delivery.status is DeliveryStatus.FAILED
    parts = store.get_outro_delivery_parts(delivery.delivery_id)
    assert parts[0].attempt_count == 5
    await service.sweep_and_deliver(sender)
    assert sender.calls == 5


def test_admin_content_changes_require_admin_role(
    service_and_store: tuple[QuestService, QuestStore, MutableClock],
) -> None:
    service, _, _ = service_and_store

    with pytest.raises(NotAuthorized):
        service.set_stage(CAPTAIN_ID, 2, "Бомбей")

    stage = service.set_stage(ADMIN_ID, 2, "Бомбей")
    assert stage.name == "Бомбей"
