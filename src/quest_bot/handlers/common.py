"""Shared Telegram-boundary helpers and dependency container."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import Any

from telegram import Message, Update
from telegram.ext import Application, BaseHandler, ContextTypes, ExtBot, JobQueue

from quest_bot import messages
from quest_bot.delivery import TelegramDelivery
from quest_bot.errors import (
    ContentValidationError,
    InactiveUser,
    InvalidQuestState,
    NotAuthorized,
    NotFound,
    QuestNotReady,
    UnknownUser,
    UsageError,
)
from quest_bot.models import CaptainPosition, ContentPart, ContentType
from quest_bot.service import QuestService, StagePresentation, StatusSnapshot

LOGGER = logging.getLogger(__name__)
type ApplicationType = Application[
    ExtBot[int],
    ContextTypes.DEFAULT_TYPE,
    dict[Any, Any],
    dict[Any, Any],
    dict[Any, Any],
    JobQueue[ContextTypes.DEFAULT_TYPE],
]
type HandlerType = BaseHandler[Update, ContextTypes.DEFAULT_TYPE, object]


@dataclass(frozen=True, slots=True)
class Dependencies:
    service: QuestService
    delivery: TelegramDelivery


def actor_id(update: Update) -> int:
    user = update.effective_user
    if user is None:
        raise UnknownUser
    return user.id


def chat_id(update: Update) -> int:
    chat = update.effective_chat
    if chat is None:
        raise UsageError
    return chat.id


def update_id(update: Update) -> int:
    if update.update_id is None:
        raise UsageError
    return update.update_id


def event_at_ms(update: Update) -> int:
    message = update.effective_message
    if message is None:
        raise UsageError
    return int(message.date.timestamp() * 1_000)


def command_args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return context.args or []


def user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    data = context.user_data
    if data is None:
        raise RuntimeError("per-user handler data is unavailable")
    return data


async def send_text(update: Update, deps: Dependencies, text: str) -> None:
    await deps.delivery.send_part(chat_id(update), ContentPart(ContentType.TEXT, text))


async def send_stage(update: Update, deps: Dependencies, presentation: StagePresentation) -> None:
    await send_text(
        update,
        deps,
        messages.stage_heading(
            presentation.stage.stage_number,
            presentation.stage.name,
        ),
    )
    total = len(presentation.tasks)
    for ordinal, task in enumerate(presentation.tasks, start=1):
        await send_text(
            update,
            deps,
            messages.task_heading(
                task.task.task_number,
                presentation.stage.name,
                ordinal,
                total,
            ),
        )
        await deps.delivery.send_parts(chat_id(update), task.prompt_parts)


def render_status(snapshot: StatusSnapshot) -> str:
    state = snapshot.state
    if state.position is CaptainPosition.NOT_STARTED:
        return messages.status_not_started(limit_minutes=snapshot.limit_minutes)
    if state.position is CaptainPosition.INTRO:
        return messages.status_intro(
            elapsed_seconds=snapshot.elapsed_seconds,
            limit_minutes=snapshot.limit_minutes,
            score=snapshot.total_score,
        )
    if state.position is CaptainPosition.STAGE:
        stage_name = snapshot.stage.name if snapshot.stage is not None else "невідомий етап"
        stage_number = state.current_stage_number or 0
        return messages.status_stage(
            elapsed_seconds=snapshot.elapsed_seconds,
            limit_minutes=snapshot.limit_minutes,
            score=snapshot.total_score,
            stage_number=stage_number,
            stage_name=stage_name,
            solved_tasks=snapshot.solved_tasks,
            total_tasks=snapshot.total_tasks,
        )
    if state.position is CaptainPosition.FINISHED:
        return messages.status_finished(
            elapsed_seconds=snapshot.elapsed_seconds,
            score=snapshot.total_score,
        )
    return messages.status_timed_out(
        elapsed_seconds=snapshot.elapsed_seconds,
        limit_minutes=snapshot.limit_minutes,
        score=snapshot.total_score,
    )


def content_part_from_message(message: Message) -> ContentPart | None:
    caption = message.caption
    if message.text is not None:
        return ContentPart(ContentType.TEXT, message.text)
    if message.photo:
        return ContentPart(ContentType.PHOTO, message.photo[-1].file_id, caption)
    if message.sticker is not None:
        return ContentPart(ContentType.STICKER, message.sticker.file_id)
    if message.voice is not None:
        return ContentPart(ContentType.VOICE, message.voice.file_id, caption)
    if message.document is not None:
        return ContentPart(ContentType.DOCUMENT, message.document.file_id, caption)
    if message.video is not None:
        return ContentPart(ContentType.VIDEO, message.video.file_id, caption)
    if message.video_note is not None:
        return ContentPart(ContentType.VIDEO_NOTE, message.video_note.file_id)
    return None


def expected_error_message(error: BaseException) -> str | None:
    if isinstance(error, UnknownUser):
        return messages.UNKNOWN_USER
    if isinstance(error, InactiveUser):
        return messages.INACTIVE_USER
    if isinstance(error, NotAuthorized):
        return messages.PERMISSION_DENIED
    if isinstance(error, QuestNotReady):
        return messages.QUEST_NOT_READY
    if isinstance(error, NotFound):
        if str(error) == "task":
            return messages.TASK_NOT_FOUND
        if str(error) == "captain":
            return messages.CAPTAIN_NOT_FOUND
        return messages.NO_CURRENT_STAGE
    if isinstance(error, InvalidQuestState):
        reason = str(error)
        if reason == "not_started":
            return "Подорож ще не розпочалася. Команда для старту: /start"
        if reason == "task already solved":
            return messages.TASK_ALREADY_SOLVED
        if reason == "no current stage":
            return messages.NO_CURRENT_STAGE
        if reason == "answers require a stage":
            return messages.ANSWER_NOT_AVAILABLE
        if reason == "terminal":
            return messages.TERMINAL_PLAY_REJECTED
        return messages.TERMINAL_PLAY_REJECTED
    if isinstance(error, ContentValidationError | UsageError):
        return messages.UNKNOWN_COMMAND
    return None


async def handle_error(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    error = context.error
    text = expected_error_message(error) if error is not None else None
    if text is None:
        details: dict[str, object] = {}
        if isinstance(update, Update):
            details["update_id"] = update.update_id
            details["user_id"] = (
                update.effective_user.id if update.effective_user is not None else None
            )
            message = update.effective_message
            if message is not None and message.text:
                details["command"] = message.text.split(maxsplit=1)[0]
        LOGGER.error("Unhandled Telegram update error", exc_info=error, extra=details)
        text = messages.TECHNICAL_ERROR
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await send_text(update, deps, text)
        except Exception:  # noqa: BLE001 - last-resort boundary logging
            LOGGER.exception("Could not deliver error response")


def register_error_handler(
    application: ApplicationType,
    deps: Dependencies,
) -> None:
    application.add_error_handler(partial(handle_error, deps=deps))


__all__ = [
    "ApplicationType",
    "Dependencies",
    "HandlerType",
    "actor_id",
    "chat_id",
    "command_args",
    "content_part_from_message",
    "event_at_ms",
    "register_error_handler",
    "render_status",
    "send_stage",
    "send_text",
    "update_id",
    "user_data",
]
