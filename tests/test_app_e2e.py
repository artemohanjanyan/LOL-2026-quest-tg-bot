from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from telegram.ext import Application

from quest_bot import messages
from quest_bot.app import create_application
from quest_bot.config import Settings
from quest_bot.service import QuestService
from quest_bot.storage.sqlite import SQLiteQuestStore
from tests.fakes import FakeTelegramRequest, TelegramUser
from tests.quest_setup import (
    ADMIN_ID,
    BASE_TIME_MS,
    CAPTAIN_ID,
    OTHER_CAPTAIN_ID,
    seed_ready_quest,
    seed_second_stage,
    seed_users,
)

type TestApplication = Application[Any, Any, Any, Any, Any, Any]


@dataclass(slots=True)
class MutableClock:
    now_ms: int = BASE_TIME_MS

    def __call__(self) -> int:
        return self.now_ms


@dataclass(slots=True)
class BotHarness:
    application: TestApplication
    telegram: FakeTelegramRequest
    store: SQLiteQuestStore
    clock: MutableClock

    def user(self, user_id: int, username: str) -> TelegramUser:
        return TelegramUser(
            self.application,
            user_id,
            username,
            timestamp=BASE_TIME_MS // 1_000,
        )


@pytest_asyncio.fixture
async def bot_harness(
    tmp_path: Path,
    telegram_request: FakeTelegramRequest,
) -> AsyncIterator[BotHarness]:
    store = SQLiteQuestStore.open(tmp_path / "quest.db", lock_instance=False)
    seed_users(store)
    seed_ready_quest(store)
    clock = MutableClock()
    service = QuestService(store, clock=clock)
    application = create_application(
        Settings(
            token="999001:test-token",
            database_path=tmp_path / "quest.db",
            sweep_interval_seconds=15,
        ),
        service,
        request=telegram_request,
    )
    try:
        async with application:
            yield BotHarness(application, telegram_request, store, clock)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_sends_intro_and_repeated_start_does_not_reset_timer(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    bot_harness.telegram.clear()

    await captain.send("/start")

    output = "\n".join(bot_harness.telegram.messages_to(CAPTAIN_ID)).casefold()
    assert "intro: pack your carpetbag" in output
    assert "task one prompt" not in output
    assert "лондон" not in output

    bot_harness.clock.now_ms += 10_000
    bot_harness.telegram.clear()
    await captain.send("/start")
    await captain.send("/status")

    repeated = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert repeated[0] == messages.QUEST_ALREADY_STARTED
    assert "INTRO: Pack your carpetbag" in repeated
    assert "Час у дорозі: 00:10" in repeated[-1]


@pytest.mark.asyncio
async def test_next_stage_prints_name_labels_and_every_task_prompt(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await captain.send("/start")
    bot_harness.telegram.clear()

    await captain.send("/next_stage")

    calls = bot_harness.telegram.calls_to(CAPTAIN_ID)
    assert [method for method, _ in calls] == [
        "sendMessage",
        "sendMessage",
        "sendMessage",
        "sendMessage",
        "sendMessage",
        "sendDocument",
        "sendVideo",
    ]
    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.stage_heading(1, "Лондон"),
        messages.task_heading(1, "Лондон", 1, 2),
        "TASK ONE PROMPT: How many days?",
        messages.task_heading(3, "Лондон", 2, 2),
        "TASK THREE PROMPT: Name the traveller",
    ]
    assert calls[-2][1]["document"] == "telegram-pdf-id"
    assert calls[-1][1]["video"] == "telegram-video-id"


@pytest.mark.asyncio
async def test_answers_and_live_configuration_recalculate_visible_score(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/answer 1 81")
    await captain.send("/answer 1   ８０")
    await captain.send("/status")
    await captain.send("/answer 1 80")

    sent = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert sent[0] == messages.answer_incorrect(attempt_number=1)
    assert sent[1] == messages.answer_correct(points=8)
    assert "Бали: 8" in sent[2]
    assert "Завдання: 1 із 2 розв’язано" in sent[2]
    assert sent[3] == messages.TASK_ALREADY_SOLVED

    await admin.send("/set_scores 1 2 0")
    assert bot_harness.telegram.messages_to(ADMIN_ID)[-1] == messages.USAGE_SET_SCORES
    await captain.send("/status")
    assert "Бали: 8" in bot_harness.telegram.messages_to(CAPTAIN_ID)[-1]

    await admin.send("/set_scores 12 7 0")
    await captain.send("/status")
    assert "Бали: 7" in bot_harness.telegram.messages_to(CAPTAIN_ID)[-1]

    await admin.send("/delete_task 1 1")
    await admin.send("/set_task 1 1")
    await admin.send("Replacement prompt")
    await admin.send("/correct_answer 81")
    await admin.send("/done")
    await captain.send("/status")

    recalculated = bot_harness.telegram.messages_to(CAPTAIN_ID)[-1]
    assert "Бали: 12" in recalculated
    assert "Завдання: 1 із 2 розв’язано" in recalculated


@pytest.mark.asyncio
async def test_duplicate_answer_update_does_not_create_another_attempt(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/answer 1 80")
    await captain.replay_last_update()
    await admin.send("/progress passepartout")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.answer_correct(points=10),
        messages.answer_correct(points=10),
    ]
    progress = bot_harness.telegram.messages_to(ADMIN_ID)[-1]
    assert "Бали: 10" in progress
    assert "№1: 1 спроб" in progress


@pytest.mark.asyncio
async def test_zero_point_correct_answer_still_solves_task(bot_harness: BotHarness) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await admin.send("/set_scores 10 0")
    await captain.send("/start")
    await captain.send("/next_stage")
    await captain.send("/answer 1 81")
    await captain.send("/answer 1 80")
    await captain.send("/status")

    sent = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert sent[-2] == messages.answer_correct(points=0)
    assert "Завдання: 1 із 2 розв’язано" in sent[-1]
    assert "Бали: 0" in sent[-1]


@pytest.mark.asyncio
async def test_unresolved_advance_requires_confirmation_then_prints_next_stage(
    bot_harness: BotHarness,
) -> None:
    seed_second_stage(bot_harness.store)
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/next_stage")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.skip_warning((1, 3))]

    bot_harness.telegram.clear()
    await captain.send("/confirm_next_stage")

    sent = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert sent[:3] == [
        messages.SKIP_CONFIRMED,
        messages.stage_heading(4, "Суець"),
        messages.task_heading(2, "Суець", 1, 1),
    ]
    calls = bot_harness.telegram.calls_to(CAPTAIN_ID)
    assert calls[-1][0] == "sendVideoNote"


@pytest.mark.asyncio
async def test_captain_cannot_use_admin_content_command(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    admin = bot_harness.user(ADMIN_ID, "organizer")
    bot_harness.telegram.clear()

    await captain.send("/set_stage 2 Париж")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.PERMISSION_DENIED]

    await admin.send("/list_stages")
    visible_stages = bot_harness.telegram.messages_to(ADMIN_ID)[-1]
    assert "Лондон" in visible_stages
    assert "Париж" not in visible_stages


@pytest.mark.asyncio
async def test_admin_can_publish_multipart_intro_through_conversation(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")

    await admin.send("/set_intro")
    await admin.send("A replacement introduction")
    await admin.send_message(document="intro-pdf-id", caption="Route papers")
    await admin.send("/done")

    assert bot_harness.telegram.messages_to(ADMIN_ID)[-1] == messages.DRAFT_PUBLISHED

    bot_harness.telegram.clear()
    await admin.send("/show_intro")
    calls = bot_harness.telegram.calls_to(ADMIN_ID)
    assert [method for method, _ in calls] == ["sendMessage", "sendDocument"]
    assert calls[0][1]["text"] == "A replacement introduction"
    assert calls[1][1]["document"] == "intro-pdf-id"
    assert calls[1][1]["caption"] == "Route papers"

    bot_harness.telegram.clear()
    await admin.send("/show_success_outro")
    await admin.send("/show_timeout_outro")
    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        "SUCCESS OUTRO: Reform Club reached",
        "TIMEOUT OUTRO: The clock wins",
    ]


@pytest.mark.asyncio
async def test_application_post_init_registers_independent_timeout_sweep(
    bot_harness: BotHarness,
) -> None:
    post_init = bot_harness.application.post_init
    assert post_init is not None

    await post_init(bot_harness.application)

    assert bot_harness.application.job_queue is not None
    jobs = bot_harness.application.job_queue.jobs()
    assert [job.name for job in jobs] == ["quest-timeout-sweep"]
    control_methods = [method for method, _ in bot_harness.telegram.calls]
    assert "setMyCommands" in control_methods


@pytest.mark.asyncio
async def test_finishing_after_deadline_before_sweep_prevents_timeout(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await admin.send("/set_time_limit 1")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.clock.now_ms += 60_001
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/confirm_next_stage")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.SKIP_CONFIRMED,
        messages.QUEST_FINISHED,
        "SUCCESS OUTRO: Reform Club reached",
    ]

    bot_harness.telegram.clear()
    post_init = bot_harness.application.post_init
    assert post_init is not None
    await post_init(bot_harness.application)
    await captain.send("/status")
    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.status_finished(score=0)]


@pytest.mark.asyncio
async def test_commands_work_after_deadline_until_sweep_claims_timeout(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await admin.send("/set_time_limit 1")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.clock.now_ms += 60_001
    bot_harness.telegram.clear()

    await captain.send("/answer 1 still travelling")
    await captain.send("/status")
    before_sweep = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert before_sweep[0] == messages.answer_incorrect(attempt_number=1)
    assert "Позиція: етап 1" in before_sweep[1]

    bot_harness.telegram.clear()
    post_init = bot_harness.application.post_init
    assert post_init is not None

    await post_init(bot_harness.application)

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == ["TIMEOUT OUTRO: The clock wins"]

    bot_harness.telegram.clear()
    await captain.send("/answer 1 80")
    await captain.send("/status")
    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.TERMINAL_PLAY_REJECTED,
        messages.status_timed_out(score=0),
    ]


@pytest.mark.asyncio
async def test_leaderboard_orders_by_score_then_username(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await admin.send(f"/add_captain {OTHER_CAPTAIN_ID} aardvark")
    bot_harness.telegram.clear()

    await admin.send("/leaderboard")
    tied = bot_harness.telegram.messages_to(ADMIN_ID)[-1].splitlines()
    assert tied[1].startswith("1. @aardvark — 0 балів")
    assert tied[2].startswith("2. @passepartout — 0 балів")

    await captain.send("/start")
    await captain.send("/next_stage")
    await captain.send("/answer 1 80")
    bot_harness.telegram.clear()

    await admin.send("/leaderboard")
    scored = bot_harness.telegram.messages_to(ADMIN_ID)[-1].splitlines()
    assert scored[1].startswith("1. @passepartout — 10 балів")
    assert scored[2].startswith("2. @aardvark — 0 балів")
