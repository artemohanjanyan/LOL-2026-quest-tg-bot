"""Ordered delivery of persisted quest content through Telegram."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import count
from typing import Final, assert_never

from telegram import Bot, Message, MessageEntity, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from quest_bot.models import ContentPart, ContentType

PLAIN_PARSE_MODE: Final[None] = None
OUTRO_MAX_ATTEMPTS: Final = 3
LOGGER = logging.getLogger(__name__)

SleepCallable = Callable[[float], Awaitable[None]]


async def _default_sleep(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)


@dataclass(frozen=True, slots=True)
class OutroDeliveryReport:
    """Best-effort outcome for one chat's ordered terminal content."""

    sent_parts: int
    failed_parts: int
    aborted: bool


class TelegramDelivery:
    """Send content parts without applying quest or persistence rules."""

    def __init__(
        self,
        bot: Bot,
        *,
        sleep: SleepCallable = _default_sleep,
    ) -> None:
        self._bot = bot
        self._sleep = sleep
        self._request_ids = count(1)

    async def send_parts(
        self,
        chat_id: int,
        parts: Iterable[ContentPart],
    ) -> None:
        """Send parts sequentially in iterable order."""

        for part in parts:
            await self.send_part(chat_id, part)

    async def send_part(
        self,
        chat_id: int,
        part: ContentPart,
        *,
        reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
        entities: Sequence[MessageEntity] | None = None,
    ) -> Message:
        """Send one part using its matching Telegram Bot API method."""

        request_id = next(self._request_ids)
        LOGGER.info(
            "Telegram request: request_id=%s chat_id=%s content_type=%s "
            "data=%r caption=%r reply_markup=%r entities=%r",
            request_id,
            chat_id,
            part.content_type.value,
            part.data,
            part.caption,
            reply_markup,
            entities,
        )
        try:
            response = await self._send_part(
                chat_id,
                part,
                reply_markup=reply_markup,
                entities=entities,
            )
        except Exception as error:
            safe_error = str(error).replace(self._bot.token, "<redacted>")
            LOGGER.warning(
                "Telegram response: request_id=%s chat_id=%s content_type=%s "
                "status=error error_type=%s error=%s",
                request_id,
                chat_id,
                part.content_type.value,
                type(error).__name__,
                safe_error,
            )
            raise
        LOGGER.info(
            "Telegram response: request_id=%s chat_id=%s content_type=%s status=ok payload=%s",
            request_id,
            chat_id,
            part.content_type.value,
            response.to_json(),
        )
        return response

    async def _send_part(
        self,
        chat_id: int,
        part: ContentPart,
        *,
        reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | None,
        entities: Sequence[MessageEntity] | None,
    ) -> Message:
        """Call the matching Telegram Bot API method."""

        match part.content_type:
            case ContentType.TEXT:
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=part.data,
                    parse_mode=PLAIN_PARSE_MODE,
                    reply_markup=reply_markup,
                    entities=entities,
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

    async def send_outro(
        self,
        chat_id: int,
        parts: Iterable[ContentPart],
    ) -> OutroDeliveryReport:
        """Send terminal content in order with small, in-memory retries.

        A failed part does not block later parts unless Telegram reports that the
        bot is forbidden from writing to the chat. Unexpected non-Telegram
        exceptions intentionally remain visible to the application boundary.
        """

        sent_parts = 0
        failed_parts = 0
        for part_number, part in enumerate(parts, start=1):
            delivered, aborted = await self._send_outro_part(chat_id, part_number, part)
            if delivered:
                sent_parts += 1
                continue
            failed_parts += 1
            if aborted:
                return OutroDeliveryReport(sent_parts, failed_parts, True)

        return OutroDeliveryReport(sent_parts, failed_parts, False)

    async def _send_outro_part(
        self,
        chat_id: int,
        part_number: int,
        part: ContentPart,
    ) -> tuple[bool, bool]:
        failure: TelegramError | None = None
        aborted = False
        attempts = 0
        for attempts in range(1, OUTRO_MAX_ATTEMPTS + 1):
            try:
                await self.send_part(chat_id, part)
            except Forbidden as error:
                failure = error
                aborted = True
                break
            except BadRequest as error:
                # BadRequest inherits NetworkError, but retrying malformed input
                # or an invalid file ID cannot make it valid.
                failure = error
                break
            except RetryAfter as error:
                failure = error
                if attempts < OUTRO_MAX_ATTEMPTS:
                    await self._sleep(self._retry_after_seconds(error))
                    continue
                break
            except NetworkError as error:
                failure = error
                if attempts < OUTRO_MAX_ATTEMPTS:
                    await self._sleep(float(2 ** (attempts - 1)))
                    continue
                break
            except TelegramError as error:
                failure = error
                break
            else:
                return True, False

        assert failure is not None
        self._log_outro_failure(
            chat_id,
            part_number,
            part,
            attempts,
            failure,
            aborted=aborted,
        )
        return False, aborted

    @staticmethod
    def _retry_after_seconds(error: RetryAfter) -> float:
        retry_after = error.retry_after
        seconds = (
            retry_after.total_seconds()
            if isinstance(retry_after, timedelta)
            else float(retry_after)
        )
        return max(0.0, seconds)

    @staticmethod
    def _log_outro_failure(
        chat_id: int,
        part_number: int,
        part: ContentPart,
        attempts: int,
        error: TelegramError,
        *,
        aborted: bool = False,
    ) -> None:
        LOGGER.warning(
            "Terminal content part could not be delivered",
            exc_info=error,
            extra={
                "chat_id": chat_id,
                "part_number": part_number,
                "content_type": part.content_type.value,
                "attempts": attempts,
                "remaining_parts_aborted": aborted,
            },
        )
