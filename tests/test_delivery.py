from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from telegram import Bot
from telegram.warnings import PTBDeprecationWarning

from quest_bot.delivery import TelegramDelivery
from quest_bot.models import ContentPart, ContentType
from tests.fakes import FakeTelegramRequest


@dataclass(slots=True)
class RecordingSleep:
    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.mark.asyncio
async def test_ordered_delivery_supports_all_quest_content_types(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    parts = (
        ContentPart(ContentType.TEXT, "Board the train"),
        ContentPart(ContentType.PHOTO, "photo-id", "map"),
        ContentPart(ContentType.STICKER, "sticker-id"),
        ContentPart(ContentType.VOICE, "voice-id", "dispatch"),
        ContentPart(ContentType.DOCUMENT, "document-id", "tickets.pdf"),
        ContentPart(ContentType.VIDEO, "video-id", "crossing"),
        ContentPart(ContentType.VIDEO_NOTE, "video-note-id"),
    )

    async with bot:
        await TelegramDelivery(bot).send_parts(202, parts)

    calls = telegram_request.calls_to(202)
    assert [method for method, _ in calls] == [
        "sendMessage",
        "sendPhoto",
        "sendSticker",
        "sendVoice",
        "sendDocument",
        "sendVideo",
        "sendVideoNote",
    ]
    assert calls[0][1]["text"] == "Board the train"
    assert calls[4][1]["document"] == "document-id"
    assert calls[4][1]["caption"] == "tickets.pdf"
    assert calls[5][1]["video"] == "video-id"
    assert calls[6][1]["video_note"] == "video-note-id"


@pytest.mark.asyncio
async def test_outro_retries_network_error_without_resending_prior_parts(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    sleep = RecordingSleep()
    telegram_request.fail_next(
        "sendDocument",
        error_code=500,
        description="Temporary Telegram outage",
    )
    parts = (
        ContentPart(ContentType.TEXT, "The first dispatch"),
        ContentPart(ContentType.DOCUMENT, "tickets-pdf"),
        ContentPart(ContentType.VIDEO, "arrival-video"),
    )

    async with bot:
        report = await TelegramDelivery(bot, sleep=sleep).send_outro(202, parts)

    assert report.sent_parts == 3
    assert report.failed_parts == 0
    assert not report.aborted
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendMessage",
        "sendDocument",
        "sendDocument",
        "sendVideo",
    ]
    assert sleep.delays == [1]


@pytest.mark.asyncio
async def test_outro_retry_after_uses_exact_injected_sleep(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    sleep = RecordingSleep()
    telegram_request.fail_next(
        "sendMessage",
        error_code=429,
        description="Too Many Requests",
        retry_after=7,
    )

    async with bot:
        with pytest.warns(PTBDeprecationWarning):
            report = await TelegramDelivery(bot, sleep=sleep).send_outro(
                202,
                (ContentPart(ContentType.TEXT, "final dispatch"),),
            )

    assert report.sent_parts == 1
    assert report.failed_parts == 0
    assert not report.aborted
    assert sleep.delays == [7]
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendMessage",
        "sendMessage",
    ]


@pytest.mark.asyncio
async def test_outro_permanent_part_error_continues_with_later_parts(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    sleep = RecordingSleep()
    telegram_request.fail_next(
        "sendDocument",
        error_code=400,
        description="Bad Request: invalid document file ID",
    )

    async with bot:
        report = await TelegramDelivery(bot, sleep=sleep).send_outro(
            202,
            (
                ContentPart(ContentType.DOCUMENT, "bad-document"),
                ContentPart(ContentType.TEXT, "The rest of the outro"),
            ),
        )

    assert report.sent_parts == 1
    assert report.failed_parts == 1
    assert not report.aborted
    assert sleep.delays == []
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendDocument",
        "sendMessage",
    ]


@pytest.mark.asyncio
async def test_outro_exhausts_three_network_attempts_then_continues(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    sleep = RecordingSleep()
    for _ in range(3):
        telegram_request.fail_next(
            "sendDocument",
            error_code=500,
            description="Temporary Telegram outage",
        )

    async with bot:
        report = await TelegramDelivery(bot, sleep=sleep).send_outro(
            202,
            (
                ContentPart(ContentType.DOCUMENT, "unavailable-document"),
                ContentPart(ContentType.TEXT, "Continue after the document"),
            ),
        )

    assert report.sent_parts == 1
    assert report.failed_parts == 1
    assert not report.aborted
    assert sleep.delays == [1, 2]
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendDocument",
        "sendDocument",
        "sendDocument",
        "sendMessage",
    ]


@pytest.mark.asyncio
async def test_outro_forbidden_aborts_without_sending_later_parts(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    sleep = RecordingSleep()
    telegram_request.fail_next(
        "sendDocument",
        error_code=403,
        description="Forbidden: bot was blocked by the user",
    )

    async with bot:
        report = await TelegramDelivery(bot, sleep=sleep).send_outro(
            202,
            (
                ContentPart(ContentType.TEXT, "Already delivered"),
                ContentPart(ContentType.DOCUMENT, "blocked-document"),
                ContentPart(ContentType.VIDEO, "must-not-be-sent"),
            ),
        )

    assert report.sent_parts == 1
    assert report.failed_parts == 1
    assert report.aborted
    assert sleep.delays == []
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendMessage",
        "sendDocument",
    ]
