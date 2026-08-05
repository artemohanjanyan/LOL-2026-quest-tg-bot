"""Ordered delivery of persisted quest content through Telegram."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import Final, assert_never

from telegram import Bot, Message
from telegram.error import RetryAfter, TelegramError

from quest_bot.errors import DeliveryFailure
from quest_bot.models import ContentPart, ContentType

PLAIN_PARSE_MODE: Final[None] = None


class TelegramDelivery:
    """Send content parts without applying quest or persistence rules."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_parts(
        self,
        chat_id: int,
        parts: Iterable[ContentPart],
    ) -> tuple[Message, ...]:
        """Send parts sequentially in iterable order."""

        sent: list[Message] = []
        for part in parts:
            sent.append(await self.send_part(chat_id, part))
        return tuple(sent)

    async def send_part(self, chat_id: int, part: ContentPart) -> Message:
        """Send one part using its matching Telegram Bot API method."""

        match part.content_type:
            case ContentType.TEXT:
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=part.data,
                    parse_mode=PLAIN_PARSE_MODE,
                )
            case ContentType.PHOTO:
                return await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=part.data,
                    caption=part.caption,
                    parse_mode=PLAIN_PARSE_MODE,
                )
            case ContentType.STICKER:
                return await self._bot.send_sticker(
                    chat_id=chat_id,
                    sticker=part.data,
                )
            case ContentType.VOICE:
                return await self._bot.send_voice(
                    chat_id=chat_id,
                    voice=part.data,
                    caption=part.caption,
                    parse_mode=PLAIN_PARSE_MODE,
                )
            case ContentType.DOCUMENT:
                return await self._bot.send_document(
                    chat_id=chat_id,
                    document=part.data,
                    caption=part.caption,
                    parse_mode=PLAIN_PARSE_MODE,
                )
            case ContentType.VIDEO:
                return await self._bot.send_video(
                    chat_id=chat_id,
                    video=part.data,
                    caption=part.caption,
                    parse_mode=PLAIN_PARSE_MODE,
                )
            case ContentType.VIDEO_NOTE:
                return await self._bot.send_video_note(
                    chat_id=chat_id,
                    video_note=part.data,
                )

        assert_never(part.content_type)

    async def send_outro_part(self, chat_id: int, part: ContentPart) -> int:
        """Send an outbox part and expose only its durable Telegram message ID."""

        try:
            message = await self.send_part(chat_id, part)
        except RetryAfter as error:
            retry_after = error.retry_after
            seconds = (
                retry_after.total_seconds()
                if isinstance(retry_after, timedelta)
                else float(retry_after)
            )
            raise DeliveryFailure(str(error), retry_after_seconds=max(0.0, seconds)) from error
        except TelegramError as error:
            raise DeliveryFailure(str(error)) from error
        return message.message_id


__all__ = ["PLAIN_PARSE_MODE", "TelegramDelivery"]
