"""Administrator enrollment and role commands."""

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.errors import ContentValidationError, InvalidQuestState, NotFound
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    clear_pending_captain_reset,
    command_args,
    event_at_ms,
    parse_integer_args,
    pending_captain_reset,
    remember_pending_captain_reset,
    send_text,
    update_id,
)


async def add_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_owner_admin(actor_id(update))
    args = command_args(context)
    if len(args) != 2:
        await send_text(update, deps, messages.USAGE_ADD_ADMIN)
        return
    try:
        telegram_id = int(args[0])
        user = deps.service.add_admin(actor_id(update), telegram_id, args[1])
    except ValueError, ContentValidationError:
        await send_text(update, deps, messages.USAGE_ADD_ADMIN)
        return
    await send_text(update, deps, messages.admin_added(user.username, user.user_id))


async def add_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if len(args) not in {1, 2}:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    try:
        telegram_id = int(args[0])
    except ValueError:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    try:
        username = args[1] if len(args) == 2 else None
        user = deps.service.add_captain(actor_id(update), telegram_id, username)
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


async def reset_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    admin_id = actor_id(update)
    deps.service.require_admin(admin_id)
    args = command_args(context)
    if len(args) != 1:
        await send_text(update, deps, messages.USAGE_RESET_CAPTAIN)
        return
    target, state = deps.service.captain_reset_target(admin_id, args[0])
    remember_pending_captain_reset(update, context, state)
    await send_text(
        update,
        deps,
        messages.captain_reset_warning(target.username, target.user_id),
    )


async def confirm_reset_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    admin_id = actor_id(update)
    deps.service.require_admin(admin_id)
    pending = pending_captain_reset(update, context)
    if pending is None:
        await send_text(update, deps, messages.NO_RESET_CONFIRMATION)
        return
    try:
        target = deps.service.reset_captain(
            admin_id,
            pending,
            event_at_ms=event_at_ms(update),
            source_update_id=update_id(update),
        )
    except InvalidQuestState:
        clear_pending_captain_reset(update, context)
        await send_text(update, deps, messages.RESET_TARGET_CHANGED)
        return
    clear_pending_captain_reset(update, context)
    await send_text(update, deps, messages.captain_reset(target.username, target.user_id))


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
        CommandHandler("add_admin", partial(add_admin, deps=deps)),
        CommandHandler("add_captain", partial(add_captain, deps=deps)),
        CommandHandler("remove_captain", partial(remove_captain, deps=deps)),
        CommandHandler("reset_captain", partial(reset_captain, deps=deps)),
        CommandHandler(
            "confirm_reset_captain",
            partial(confirm_reset_captain, deps=deps),
        ),
        CommandHandler("list_users", partial(list_users, deps=deps)),
    ]
