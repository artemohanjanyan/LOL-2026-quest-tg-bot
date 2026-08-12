"""Administrator enrollment and role commands."""

from functools import partial

from telegram import (
    KeyboardButton,
    KeyboardButtonRequestUsers,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    SharedUser,
    Update,
)
from telegram.ext import CommandHandler, ContextTypes, MessageHandler, filters

from quest_bot import messages
from quest_bot.errors import ContentValidationError, InvalidQuestState, NotFound
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    clear_pending_captain_add,
    clear_pending_captain_reset,
    command_args,
    event_at_ms,
    parse_integer_args,
    pending_captain_add,
    pending_captain_reset,
    remember_pending_captain_add,
    remember_pending_captain_reset,
    send_text,
    update_id,
)
from quest_bot.models import UserRole


def _display_name(user: SharedUser) -> str:
    if user.username:
        return f"@{user.username}"
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip()


async def add_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_owner_admin(actor_id(update))
    args = command_args(context)
    if len(args) < 2:
        await send_text(update, deps, messages.USAGE_ADD_ADMIN)
        return
    try:
        telegram_id = int(args[0])
        user = deps.service.add_admin(actor_id(update), telegram_id, " ".join(args[1:]))
    except ValueError, ContentValidationError:
        await send_text(update, deps, messages.USAGE_ADD_ADMIN)
        return
    await send_text(update, deps, messages.admin_added(user.display_name, user.user_id))


async def add_captain(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_admin(actor_id(update))
    if command_args(context):
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN)
        return
    request_id = update_id(update) & 0x7FFF_FFFF
    remember_pending_captain_add(update, context, request_id)
    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    messages.CAPTAIN_PICKER_BUTTON,
                    request_users=KeyboardButtonRequestUsers(
                        request_id=request_id,
                        user_is_bot=False,
                        max_quantity=1,
                        request_name=True,
                        request_username=True,
                    ),
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await send_text(
        update,
        deps,
        messages.CAPTAIN_PICKER_PROMPT,
        reply_markup=keyboard,
    )


async def add_captain_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    admin_id = actor_id(update)
    deps.service.require_admin(admin_id)
    message = update.effective_message
    shared = message.users_shared if message is not None else None
    expected_request_id = pending_captain_add(update, context)
    if shared is None or expected_request_id is None:
        await send_text(
            update,
            deps,
            messages.CAPTAIN_PICKER_STALE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if shared.request_id != expected_request_id:
        await send_text(update, deps, messages.CAPTAIN_PICKER_STALE)
        return
    if len(shared.users) != 1:
        clear_pending_captain_add(update, context)
        await send_text(
            update,
            deps,
            messages.CAPTAIN_PICKER_STALE,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    selected = shared.users[0]
    display_name = _display_name(selected)
    if not display_name:
        clear_pending_captain_add(update, context)
        await send_text(
            update,
            deps,
            messages.CAPTAIN_PICKER_NAME_MISSING,
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    try:
        existing = deps.service.resolve_user(str(selected.user_id))
    except NotFound:
        existing = None
    if existing is not None and existing.role is UserRole.ADMIN:
        result = messages.selected_user_is_admin(existing.display_name)
    elif existing is not None and existing.active:
        result = messages.captain_already_active(existing.display_name)
    else:
        user = deps.service.add_captain(admin_id, selected.user_id, display_name)
        result = messages.captain_added(user.display_name, user.user_id)
    clear_pending_captain_add(update, context)
    await send_text(update, deps, result, reply_markup=ReplyKeyboardRemove())


async def add_captain_by_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    admin_id = actor_id(update)
    deps.service.require_admin(admin_id)
    args = command_args(context)
    if not args:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN_BY_ID)
        return
    try:
        telegram_id = int(args[0])
        display_name = " ".join(args[1:]).strip() or None
        user = deps.service.add_captain(admin_id, telegram_id, display_name)
    except ValueError, ContentValidationError:
        await send_text(update, deps, messages.USAGE_ADD_CAPTAIN_BY_ID)
        return
    await send_text(update, deps, messages.captain_added(user.display_name, user.user_id))


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
        messages.captain_removed(user.display_name, user.user_id),
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
        messages.captain_reset_warning(target.display_name, target.user_id),
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
    await send_text(update, deps, messages.captain_reset(target.display_name, target.user_id))


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
        f"{user.user_id} — {user.display_name} — {user.role.value} — "
        f"{'активний' if user.active else 'неактивний'}"
        for user in users
    )
    await send_text(update, deps, "\n".join(lines))


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("add_admin", partial(add_admin, deps=deps)),
        CommandHandler("add_captain", partial(add_captain, deps=deps)),
        CommandHandler("add_captain_by_id", partial(add_captain_by_id, deps=deps)),
        CommandHandler("remove_captain", partial(remove_captain, deps=deps)),
        CommandHandler("reset_captain", partial(reset_captain, deps=deps)),
        CommandHandler(
            "confirm_reset_captain",
            partial(confirm_reset_captain, deps=deps),
        ),
        CommandHandler("list_users", partial(list_users, deps=deps)),
    ]


def build_captain_picker_handler(deps: Dependencies) -> HandlerType:
    return MessageHandler(
        filters.StatusUpdate.USERS_SHARED,
        partial(add_captain_selection, deps=deps),
    )
