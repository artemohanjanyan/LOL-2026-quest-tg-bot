import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from telegram.ext import Application, CallbackContext

from quest_bot import messages
from quest_bot.app import (
    SWEEP_HEARTBEAT_EVERY_KEY,
    _timeout_sweep,
    create_application,
)
from quest_bot.config import Settings
from quest_bot.models import CaptainPosition
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
    service = QuestService(store, clock=clock, owner_admin_id=ADMIN_ID)
    application = create_application(
        Settings(
            token="999001:test-token",
            database_path=tmp_path / "quest.db",
            bootstrap_admin_id=ADMIN_ID,
            bootstrap_admin_username="organizer",
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
async def test_unknown_commands_and_messages_receive_guidance(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")

    await captain.send("/take_a_detour")
    await captain.send("Is this the right train?")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.UNKNOWN_COMMAND,
        messages.UNKNOWN_MESSAGE,
    ]


@pytest.mark.asyncio
async def test_incoming_updates_and_outgoing_responses_are_logged(
    bot_harness: BotHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="quest_bot.handlers.registry")
    caplog.set_level(logging.INFO, logger="quest_bot.delivery")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")

    await captain.send("/status")

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "Telegram update received:" in rendered
    assert '"text": "/status"' in rendered
    assert "Telegram request:" in rendered
    assert messages.status_not_started(limit_minutes=80) in rendered
    assert "Telegram response:" in rendered
    assert "status=ok" in rendered
    assert rendered.count("request_id=1") == 2


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

    await captain.send("/set_stage 2 Париж")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.PERMISSION_DENIED]

    await admin.send("/list_stages")
    visible_stages = bot_harness.telegram.messages_to(ADMIN_ID)[-1]
    assert "Лондон" in visible_stages
    assert "Париж" not in visible_stages


@pytest.mark.asyncio
async def test_add_captain_reuses_existing_username_when_omitted(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await admin.send(f"/remove_captain {CAPTAIN_ID}")
    bot_harness.telegram.clear()

    await admin.send(f"/add_captain {CAPTAIN_ID}")
    await admin.send(f"/add_captain {OTHER_CAPTAIN_ID}")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        messages.captain_added("passepartout", CAPTAIN_ID),
        messages.USAGE_ADD_CAPTAIN,
    ]
    captain = bot_harness.store.get_user(CAPTAIN_ID)
    assert captain is not None
    assert captain.username == "passepartout"
    assert captain.active
    assert bot_harness.store.get_user(OTHER_CAPTAIN_ID) is None


@pytest.mark.asyncio
async def test_only_owner_admin_can_add_admin_or_see_command(
    bot_harness: BotHarness,
) -> None:
    owner = bot_harness.user(ADMIN_ID, "organizer")
    await owner.send("/help")
    owner_help = bot_harness.telegram.messages_to(ADMIN_ID)[-1]
    assert "/add_admin" in owner_help
    assert "/confirm_next_stage" not in owner_help
    assert "/confirm_reset_captain" not in owner_help
    assert "/cancel" not in owner_help

    await owner.send(f"/add_admin {OTHER_CAPTAIN_ID} stationmaster")
    added = bot_harness.store.get_user(OTHER_CAPTAIN_ID)
    assert added is not None
    assert added.role.value == "admin"
    assert added.active

    regular_admin = bot_harness.user(OTHER_CAPTAIN_ID, "stationmaster")
    await regular_admin.send("/help")
    await regular_admin.send("/add_admin 404 conductor")

    regular_messages = bot_harness.telegram.messages_to(OTHER_CAPTAIN_ID)
    assert "/add_admin" not in regular_messages[0]
    assert regular_messages[1] == messages.PERMISSION_DENIED
    assert bot_harness.store.get_user(404) is None


@pytest.mark.asyncio
async def test_reset_captain_requires_confirmation_and_can_be_cancelled(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await captain.send("/start")
    await captain.send("/next_stage")
    await captain.send("/answer 1 80")
    transitions_before = bot_harness.store.list_captain_transitions(CAPTAIN_ID)
    bot_harness.telegram.clear()

    await admin.send("/reset_captain passepartout")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        messages.captain_reset_warning("passepartout", CAPTAIN_ID)
    ]
    assert bot_harness.store.get_captain_state(CAPTAIN_ID).position is CaptainPosition.STAGE
    assert bot_harness.store.get_attempt_counts(CAPTAIN_ID, 1) == ((1, 1),)

    bot_harness.telegram.clear()
    await admin.send("/cancel")
    assert bot_harness.telegram.messages_to(ADMIN_ID) == [messages.RESET_CANCELLED]
    assert bot_harness.store.get_captain_state(CAPTAIN_ID).position is CaptainPosition.STAGE

    bot_harness.telegram.clear()
    await admin.send("/confirm_reset_captain")
    assert bot_harness.telegram.messages_to(ADMIN_ID) == [messages.NO_RESET_CONFIRMATION]

    bot_harness.telegram.clear()
    await admin.send(f"/reset_captain {CAPTAIN_ID}")
    await admin.send("/confirm_reset_captain")

    assert bot_harness.telegram.messages_to(ADMIN_ID)[-1] == messages.captain_reset(
        "passepartout", CAPTAIN_ID
    )
    state = bot_harness.store.get_captain_state(CAPTAIN_ID)
    assert state.position is CaptainPosition.NOT_STARTED
    assert state.started_at_ms is None
    assert state.current_stage_number is None
    assert bot_harness.store.get_attempt_counts(CAPTAIN_ID, 1) == ()
    assert not any(item.solved for item in bot_harness.store.list_task_progress(CAPTAIN_ID))

    transitions = bot_harness.store.list_captain_transitions(CAPTAIN_ID)
    assert len(transitions) == len(transitions_before) + 1
    reset_transition = transitions[-1]
    assert reset_transition.from_position is CaptainPosition.STAGE
    assert reset_transition.to_position is CaptainPosition.NOT_STARTED
    assert reset_transition.source_update_id is not None


@pytest.mark.asyncio
async def test_reset_captain_accepts_admin_role_target(bot_harness: BotHarness) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await admin.send("/start")
    await admin.send("/next_stage")
    await admin.send("/answer 1 80")
    await admin.send("/next_stage")
    await admin.send("/confirm_next_stage")
    assert bot_harness.store.get_captain_state(ADMIN_ID).position is CaptainPosition.FINISHED
    bot_harness.telegram.clear()

    await admin.send(f"/reset_captain {ADMIN_ID}")
    await admin.send("/confirm_reset_captain")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        messages.captain_reset_warning("organizer", ADMIN_ID),
        messages.captain_reset("organizer", ADMIN_ID),
    ]
    user = bot_harness.store.get_user(ADMIN_ID)
    assert user is not None
    assert user.role.value == "admin"
    assert bot_harness.store.get_captain_state(ADMIN_ID).position is CaptainPosition.NOT_STARTED
    assert bot_harness.store.get_attempt_counts(ADMIN_ID, 1) == ()


@pytest.mark.asyncio
async def test_reset_confirmation_expires_when_captain_changes_stage(
    bot_harness: BotHarness,
) -> None:
    seed_second_stage(bot_harness.store)
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await captain.send("/start")
    await captain.send("/next_stage")
    await admin.send(f"/reset_captain {CAPTAIN_ID}")
    await captain.send("/next_stage")
    await captain.send("/confirm_next_stage")
    bot_harness.telegram.clear()

    await admin.send("/confirm_reset_captain")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [messages.RESET_TARGET_CHANGED]
    state = bot_harness.store.get_captain_state(CAPTAIN_ID)
    assert state.position is CaptainPosition.STAGE
    assert state.current_stage_number == 4


@pytest.mark.asyncio
async def test_empty_stage_list_uses_admin_facing_copy(bot_harness: BotHarness) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await admin.send("/delete_stage 1")
    bot_harness.telegram.clear()

    await admin.send("/list_stages")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [messages.NO_CONFIGURED_STAGES]


@pytest.mark.asyncio
async def test_show_settings_reports_values_content_stages_and_readiness(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    seed_second_stage(bot_harness.store)
    await admin.send("/set_time_limit 75")
    await admin.send("/set_scores 12 7 0")
    bot_harness.telegram.clear()

    await admin.send("/show_settings")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        "\n".join(
            [
                "Путівник експедиції:",
                "Ліміт часу: 75 хв.",
                "Шкала балів за спробами: 12, 7, 0.",
                "Вступ: налаштовано (частин: 1).",
                "Успішний фінал: налаштовано (частин: 1).",
                "Фінал за часом: налаштовано (частин: 1).",
                "Етапи:",
                "1. Лондон — завдань: 2",
                "4. Суець — завдань: 1",
                "Готовність: маршрут готовий до старту.",
            ]
        )
    ]

    await admin.send("/delete_task 4 2")
    bot_harness.telegram.clear()
    await admin.send("/show_settings")
    summary = bot_harness.telegram.messages_to(ADMIN_ID)[0]
    assert "4. Суець — завдань: 0" in summary
    assert "Готовність: маршрут ще не готовий до старту." in summary


@pytest.mark.asyncio
async def test_missing_stage_uses_admin_facing_copy(bot_harness: BotHarness) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")

    await admin.send("/delete_stage 99")
    await admin.send("/show_stage 99")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [
        messages.STAGE_NOT_FOUND,
        messages.STAGE_NOT_FOUND,
    ]

    await admin.send("/set_task 99 1")
    await admin.send("A task for a missing stage")
    await admin.send("/correct_answer answer")
    bot_harness.telegram.clear()
    await admin.send("/done")

    assert bot_harness.telegram.messages_to(ADMIN_ID) == [messages.STAGE_NOT_FOUND]


@pytest.mark.asyncio
async def test_admin_can_publish_multipart_intro_through_conversation(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")

    await admin.send("/set_intro")
    assert bot_harness.telegram.messages_to(ADMIN_ID)[-1] == messages.DRAFT_READY
    assert "/cancel" in messages.DRAFT_READY
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
async def test_timeout_sweep_logs_periodic_application_heartbeat(
    bot_harness: BotHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    post_init = bot_harness.application.post_init
    assert post_init is not None
    await post_init(bot_harness.application)
    heartbeat_every = int(
        bot_harness.application.bot_data[SWEEP_HEARTBEAT_EVERY_KEY]
    )
    context = CallbackContext(bot_harness.application)
    caplog.set_level(logging.INFO, logger="quest_bot.app")
    caplog.clear()

    for _ in range(heartbeat_every - 1):
        await _timeout_sweep(context)
    assert "Quest timeout sweep healthy" not in caplog.text

    await _timeout_sweep(context)

    assert f"Quest timeout sweep healthy: runs={heartbeat_every}" in caplog.text


@pytest.mark.asyncio
async def test_finishing_after_deadline_before_sweep_prevents_timeout(
    bot_harness: BotHarness,
) -> None:
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    admin = bot_harness.user(ADMIN_ID, "organizer")
    await admin.send("/set_time_limit 1")
    await captain.send("/start")
    await captain.send("/next_stage")
    await captain.send("/answer 1 80")
    bot_harness.clock.now_ms += 60_001
    await captain.send("/next_stage")
    bot_harness.telegram.clear()

    await captain.send("/confirm_next_stage")

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.SKIP_CONFIRMED,
        "SUCCESS OUTRO: Reform Club reached",
        messages.final_score(10),
    ]

    bot_harness.telegram.clear()
    post_init = bot_harness.application.post_init
    assert post_init is not None
    await post_init(bot_harness.application)
    await captain.send("/status")
    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [messages.status_finished(score=10)]


@pytest.mark.asyncio
async def test_commands_work_after_deadline_until_sweep_claims_timeout(
    bot_harness: BotHarness,
) -> None:
    admin = bot_harness.user(ADMIN_ID, "organizer")
    captain = bot_harness.user(CAPTAIN_ID, "passepartout")
    await admin.send("/set_time_limit 1")
    await admin.send(f"/add_captain {OTHER_CAPTAIN_ID} fix")
    other_captain = bot_harness.user(OTHER_CAPTAIN_ID, "fix")
    await captain.send("/start")
    await captain.send("/next_stage")
    await other_captain.send("/start")
    await other_captain.send("/next_stage")
    bot_harness.clock.now_ms += 60_001
    bot_harness.telegram.clear()

    await captain.send("/answer 1 still travelling")
    await captain.send("/answer 1 80")
    await captain.send("/status")
    before_sweep = bot_harness.telegram.messages_to(CAPTAIN_ID)
    assert before_sweep[0] == messages.answer_incorrect(attempt_number=1)
    assert before_sweep[1] == messages.answer_correct(points=8)
    assert "Позиція: етап 1" in before_sweep[2]
    assert "Бали: 8" in before_sweep[2]

    bot_harness.telegram.clear()
    post_init = bot_harness.application.post_init
    assert post_init is not None

    await post_init(bot_harness.application)

    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        "TIMEOUT OUTRO: The clock wins",
        messages.final_score(8),
    ]
    assert bot_harness.telegram.messages_to(OTHER_CAPTAIN_ID) == [
        "TIMEOUT OUTRO: The clock wins",
        messages.final_score(0),
    ]

    bot_harness.telegram.clear()
    await captain.send("/answer 1 80")
    await captain.send("/status")
    assert bot_harness.telegram.messages_to(CAPTAIN_ID) == [
        messages.TERMINAL_PLAY_REJECTED,
        messages.status_timed_out(score=8),
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
