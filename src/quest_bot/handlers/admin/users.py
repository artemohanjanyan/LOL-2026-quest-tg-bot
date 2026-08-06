"""Administrator enrollment and role commands."""

from __future__ import annotations

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.errors import ContentValidationError, NotFound
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    command_args,
    parse_integer_args,
    send_text,
)


async def add_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if len(args) != 2:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    try:
        telegram_id = int(args[0])
    except ValueError:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    try:
        user = deps.service.add_captain(actor_id(update), telegram_id, args[1])
    except ContentValidationError:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    await send_text(
        update,
        deps,
        messages.captain_added(user.username, user.user_id),
    )


async def remove_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=1)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_REMOVE_CAPTAIN)
        return
    (telegram_id,) = numbers
    deps.service.require_admin(actor_id(update))
    try:
        user = deps.service.resolve_user(str(telegram_id))
    except NotFound:
        await send_text(update, deps, messages.CAPTAIN_NOT_FOUND)
        return
    if not deps.service.remove_captain(actor_id(update), telegram_id):
        await send_text(update, deps, messages.CAPTAIN_NOT_FOUND)
        return
    await send_text(
        update,
        deps,
        messages.captain_removed(user.username, user.user_id),
    )


async def list_users(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    users = deps.service.list_users(actor_id(update))
    if not users:
        await send_text(update, deps, "Список мандрівників порожній.")
        return
    lines = ["Мандрівники експедиції:"]
    lines.extend(
        f"{user.user_id} — @{user.username} — {user.role.value} — "
        f"{'активний' if user.active else 'неактивний'}"
        for user in users
    )
    await send_text(update, deps, "\n".join(lines))


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("add_captain", partial(add_captain, deps=deps)),
        CommandHandler("remove_captain", partial(remove_captain, deps=deps)),
        CommandHandler("list_users", partial(list_users, deps=deps)),
    ]
