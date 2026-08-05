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
from quest_bot.models import CaptainPosition
from quest_bot.service import QuestService
from quest_bot.storage.base import QuestStore
from quest_bot.storage.sqlite import SQLiteQuestStore
from tests.fakes import FakeTelegramRequest, TelegramUser, joined_text
from tests.quest_setup import (
    ADMIN_ID,
    BASE_TIME_MS,
    CAPTAIN_ID,
    seed_ready_quest,
    seed_second_stage,
    seed_users,
)
from tests.test_service import MutableClock

type TestApplication = Application[Any, Any, Any, Any, Any, Any]


@dataclass(slots=True)
class BotHarness:
    application: TestApplication
    telegram: FakeTelegramRequest
    store: QuestStore
    service: QuestService
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
            yield BotHarness(application, telegram_request, store, service, clock)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_start_sends_intro_without_entering_or_printing_stage(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    bot_harness.telegram.clear()

    await captain.send("/start")

    output = joined_text(bot_harness.telegram.messages_to(CAPTAIN_ID))
    assert "intro: pack your carpetbag" in output
    assert "task one prompt" not in output
    assert "лондон" not in output
    state = bot_harness.store.get_captain_state(CAPTAIN_ID)
    assert state is not None
    assert state.position is CaptainPosition.INTRO
    assert state.current_stage_number is None


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
async def test_answers_and_status_report_attempt_based_score(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await captain.send("/start")
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/answer 1 eighty")
    await captain.send("/answer 1   ８０")
    await captain.send("/status")

    sent = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert sent[0] == messages.answer_incorrect(attempt_number=1)
    assert sent[1] == messages.answer_correct(points=8)
    assert "Бали: 8" in sent[2]
    assert "Завдання: 1 із 2 розв’язано" in sent[2]


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
    state = bot_harness.store.get_captain_state(CAPTAIN_ID)
    assert state is not None
    assert state.current_stage_number == 1

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
    state = bot_harness.store.get_captain_state(CAPTAIN_ID)
    assert state is not None
    assert state.current_stage_number == 4


@pytest.mark.asyncio
async def test_captain_cannot_use_admin_content_command(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    bot_harness.telegram.clear()

    await captain.send("/set_stage 2 Париж")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.PERMISSION_DENIED]
    assert bot_harness.store.get_stage(2) is None


@pytest.mark.asyncio
async def test_admin_can_publish_multipart_intro_through_conversation(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")

    await admin.send("/set_intro")
    await admin.send("A replacement introduction")
    await admin.send_message(document="intro-pdf-id", caption="Route papers")
    await admin.send("/done")

    parts = bot_harness.store.get_intro_parts()
    assert [part.data for part in parts] == [
        "A replacement introduction",
        "intro-pdf-id",
    ]
    assert parts[1].caption == "Route papers"
    assert bot_harness.telegram.messages_to(ADMIN_ID)[-1] == messages.DRAFT_PUBLISHED


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
