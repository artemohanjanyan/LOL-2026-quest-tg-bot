from __future__ import annotations

import pytest
from telegram import Bot
from telegram.warnings import PTBDeprecationWarning

from quest_bot.delivery import TelegramDelivery
from quest_bot.errors import DeliveryFailure
from quest_bot.models import ContentPart, ContentType
from tests.fakes import FakeTelegramRequest


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
        delivered = await TelegramDelivery(bot).send_parts(202, parts)

    assert len(delivered) == len(parts)
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
async def test_telegram_retry_after_is_exposed_to_durable_outbox(
    telegram_request: FakeTelegramRequest,
) -> None:
    bot = Bot("999001:test-token", request=telegram_request)
    telegram_request.fail_next(
        "sendMessage",
        error_code=429,
        description="Too Many Requests",
        retry_after=7,
    )

    async with bot:
        with (
            pytest.warns(PTBDeprecationWarning),
            pytest.raises(DeliveryFailure) as raised,
        ):
            await TelegramDelivery(bot).send_outro_part(
                202,
                ContentPart(ContentType.TEXT, "final dispatch"),
            )

    assert raised.value.retry_after_seconds == 7
