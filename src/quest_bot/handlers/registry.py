"""Explicit handler ordering and Telegram command metadata."""

import logging
from functools import partial

from telegram import BotCommand, Update
from telegram.ext import ContextTypes, MessageHandler, TypeHandler, filters

from quest_bot import messages
from quest_bot.handlers import captain
from quest_bot.handlers.admin import content, operations, reports, users
from quest_bot.handlers.common import (
    ApplicationType,
    Dependencies,
    HandlerType,
    register_error_handler,
    send_text,
)

LOGGER = logging.getLogger(__name__)

COMMANDS: tuple[BotCommand, ...] = (
    BotCommand("start", "розпочати відлік і отримати вступ"),
    BotCommand("next_stage", "перейти до наступного етапу"),
    BotCommand("answer", "відповісти на завдання"),
    BotCommand("stage", "повторити поточний етап"),
    BotCommand("status", "перевірити час і прогрес"),
    BotCommand("help", "показати доступні команди"),
)


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.info("Telegram update received: %s", update.to_json())


async def ignore_edited_message(
    _update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Consume edits that Telegram queued before the polling filter took effect."""


async def unknown_command(update: Update, context: object, *, deps: Dependencies) -> None:
    await send_text(update, deps, messages.UNKNOWN_COMMAND)


def register_handlers(
    application: ApplicationType,
    deps: Dependencies,
) -> None:
    application.add_handler(TypeHandler(Update, log_update), group=-1)
    application.add_handler(MessageHandler(filters.UpdateType.EDITED, ignore_edited_message))
    application.add_handler(users.build_captain_picker_handler(deps))
    # The content conversation comes first so /done and /cancel are routed back
    # to the active per-user, per-chat draft before ordinary command handlers.
    modules = (content, users, operations, reports, captain)
    for module in modules:
        handlers: list[HandlerType] = module.build_handlers(deps)
        for handler in handlers:
            application.add_handler(handler)
    application.add_handler(MessageHandler(filters.COMMAND, partial(unknown_command, deps=deps)))
    register_error_handler(application, deps)
