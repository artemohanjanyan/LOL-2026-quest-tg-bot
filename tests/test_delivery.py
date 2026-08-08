import pytest
from pydantic import BaseModel, Field
from telegram import Bot
from telegram.warnings import PTBDeprecationWarning

from quest_bot.delivery import TelegramDelivery
from quest_bot.models import ContentPart, ContentType
from tests.fakes import FakeTelegramRequest


class RecordingSleep(BaseModel):
    delays: list[float] = Field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def test_ordered_delivery_supports_all_quest_content_types(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    parts = (
        ContentPart(content_type=ContentType.TEXT, data="Board the train"),
        ContentPart(content_type=ContentType.PHOTO, data="photo-id", caption="map"),
        ContentPart(content_type=ContentType.STICKER, data="sticker-id"),
        ContentPart(content_type=ContentType.VOICE, data="voice-id", caption="dispatch"),
        ContentPart(
            content_type=ContentType.DOCUMENT,
            data="document-id",
            caption="tickets.pdf",
        ),
        ContentPart(content_type=ContentType.VIDEO, data="video-id", caption="crossing"),
        ContentPart(content_type=ContentType.VIDEO_NOTE, data="video-note-id"),
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
        ContentPart(content_type=ContentType.TEXT, data="The first dispatch"),
        ContentPart(content_type=ContentType.DOCUMENT, data="tickets-pdf"),
        ContentPart(content_type=ContentType.VIDEO, data="arrival-video"),
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
                (ContentPart(content_type=ContentType.TEXT, data="final dispatch"),),
            )

    assert report.sent_parts == 1
    assert report.failed_parts == 0
    assert not report.aborted
    assert sleep.delays == [7]
    assert [method for method, _ in telegram_request.calls_to(202)] == [
        "sendMessage",
        "sendMessage",
    ]


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
                ContentPart(content_type=ContentType.DOCUMENT, data="bad-document"),
                ContentPart(content_type=ContentType.TEXT, data="The rest of the outro"),
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
                ContentPart(content_type=ContentType.DOCUMENT, data="unavailable-document"),
                ContentPart(content_type=ContentType.TEXT, data="Continue after the document"),
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
                ContentPart(content_type=ContentType.TEXT, data="Already delivered"),
                ContentPart(content_type=ContentType.DOCUMENT, data="blocked-document"),
                ContentPart(content_type=ContentType.VIDEO, data="must-not-be-sent"),
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
