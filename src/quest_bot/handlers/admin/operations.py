"""Administrator live settings and delivery operations."""

from __future__ import annotations

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.errors import ContentValidationError
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    command_args,
    send_text,
)


async def set_scores(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if not args:
        await send_text(update, deps, messages.USAGE_SET_SCORES)
        return
    try:
        points = tuple(int(value) for value in args)
        saved = deps.service.set_scores(actor_id(update), points)
    except (ValueError, ContentValidationError):
        await send_text(update, deps, messages.USAGE_SET_SCORES)
        return
    await send_text(update, deps, messages.scores_updated(saved))


async def set_time_limit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if len(args) != 1:
        await send_text(update, deps, messages.USAGE_SET_TIME_LIMIT)
        return
    try:
        minutes = int(args[0])
        saved = deps.service.set_time_limit(actor_id(update), minutes)
    except (ValueError, ContentValidationError):
        await send_text(update, deps, messages.USAGE_SET_TIME_LIMIT)
        return
    await send_text(update, deps, messages.time_limit_updated(saved))


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("set_scores", partial(set_scores, deps=deps)),
        CommandHandler("set_time_limit", partial(set_time_limit, deps=deps)),
    ]


__all__ = ["build_handlers"]
