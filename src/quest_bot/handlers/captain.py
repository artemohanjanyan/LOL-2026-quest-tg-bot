"""Captain-facing Telegram command handlers."""

from functools import partial

from telegram import ForceReply, ReplyKeyboardRemove, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from quest_bot import messages
from quest_bot.handlers.common import (
    TASK_ANSWER_CALLBACK_PATTERN,
    Dependencies,
    HandlerType,
    actor_id,
    chat_id,
    clear_pending_captain_add,
    clear_pending_captain_reset,
    event_at_ms,
    parse_numbered_text,
    render_status,
    send_stage,
    send_text,
    update_id,
    user_data,
)
from quest_bot.models import CaptainPosition, ContentPart, ContentType, UserRole
from quest_bot.service import AdvanceResult

_PENDING_SKIP_KEY_PREFIX = "captain:pending-skip-stage:"
_PENDING_ANSWER_KEY_PREFIX = "captain:pending-answer:"


def _pending_skip_key(update: Update) -> str:
    return f"{_PENDING_SKIP_KEY_PREFIX}{chat_id(update)}"


def _remember_pending_skip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stage_number: int,
) -> None:
    user_data(context)[_pending_skip_key(update)] = stage_number


def _pending_skip_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int | None:
    value = user_data(context).get(_pending_skip_key(update))
    return value if type(value) is int else None


def _clear_pending_skip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    return user_data(context).pop(_pending_skip_key(update), None) is not None


def _pending_answer_key(update: Update) -> str:
    return f"{_PENDING_ANSWER_KEY_PREFIX}{chat_id(update)}"


def _remember_pending_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stage_number: int,
    task_number: int,
) -> None:
    user_data(context)[_pending_answer_key(update)] = (stage_number, task_number)


def _pending_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, int] | None:
    value = user_data(context).get(_pending_answer_key(update))
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(type(number) is int for number in value)
    ):
        return value
    return None


def _clear_pending_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[int, int] | None:
    value = user_data(context).pop(_pending_answer_key(update), None)
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and all(type(number) is int for number in value)
    ):
        return value
    return None


async def _send_help(update: Update, deps: Dependencies) -> None:
    user = deps.service.require_user(actor_id(update))
    await send_text(
        update,
        deps,
        messages.help_message(
            is_admin=user.role is UserRole.ADMIN,
            is_owner_admin=deps.service.is_owner_admin(user.user_id),
        ),
    )


async def _deliver_advance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: Dependencies,
    result: AdvanceResult,
    *,
    skipped: bool,
) -> None:
    _clear_pending_skip(update, context)
    _clear_pending_answer(update, context)
    if result.finished:
        prefix = (ContentPart(ContentType.TEXT, messages.SKIP_CONFIRMED),) if skipped else ()
        await deps.delivery.send_outro(
            chat_id(update),
            (
                *prefix,
                *result.outro_parts,
                ContentPart(ContentType.TEXT, messages.final_score(result.final_score)),
            ),
        )
        return
    if skipped:
        await send_text(update, deps, messages.SKIP_CONFIRMED)
    if result.presentation is not None:
        await send_stage(update, deps, result.presentation, answer_buttons=True)


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    result = deps.service.start(
        actor_id(update),
        event_at_ms=event_at_ms(update),
        source_update_id=update_id(update),
    )
    current_chat_id = chat_id(update)
    if result.started:
        snapshot = deps.service.status(actor_id(update))
        await send_text(
            update,
            deps,
            messages.quest_started(limit_minutes=snapshot.limit_minutes),
        )
        await _send_help(update, deps)
        await deps.delivery.send_parts(current_chat_id, result.intro_parts)
        await send_text(update, deps, messages.INTRO_POSITION)
        return

    if result.state.position.is_terminal:
        await send_text(update, deps, render_status(deps.service.status(actor_id(update))))
    elif result.state.position is CaptainPosition.INTRO:
        await send_text(update, deps, messages.QUEST_ALREADY_STARTED)
        await deps.delivery.send_parts(current_chat_id, result.intro_parts)
        await send_text(update, deps, messages.INTRO_POSITION)
    elif result.state.position is CaptainPosition.STAGE:
        await send_text(update, deps, messages.QUEST_ALREADY_STARTED)
        await send_stage(
            update,
            deps,
            deps.service.get_stage(actor_id(update)),
            answer_buttons=True,
        )


async def next_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    result = deps.service.advance(
        actor_id(update),
        event_at_ms=event_at_ms(update),
        source_update_id=update_id(update),
    )
    if result.needs_confirmation:
        current_stage = result.state.current_stage_number
        if current_stage is not None:
            _remember_pending_skip(update, context, current_stage)
        await send_text(update, deps, messages.skip_warning(result.unsolved_task_numbers))
        return
    await _deliver_advance(update, context, deps, result, skipped=False)


async def confirm_next_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    captain_id = actor_id(update)
    pending_stage = _pending_skip_stage(update, context)
    snapshot = deps.service.status(captain_id)
    if (
        pending_stage is None
        or snapshot.state.position is not CaptainPosition.STAGE
        or snapshot.state.current_stage_number != pending_stage
    ):
        _clear_pending_skip(update, context)
        await send_text(update, deps, messages.NO_SKIP_CONFIRMATION)
        return

    _clear_pending_skip(update, context)
    result = deps.service.advance(
        captain_id,
        event_at_ms=event_at_ms(update),
        source_update_id=update_id(update),
        confirm_skip=True,
    )
    await _deliver_advance(update, context, deps, result, skipped=True)


async def answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    _clear_pending_answer(update, context)
    parsed = parse_numbered_text(context)
    if parsed is None:
        await send_text(update, deps, messages.USAGE_ANSWER)
        return
    task_number, raw_answer = parsed
    if task_number <= 0:
        await send_text(update, deps, messages.USAGE_ANSWER)
        return

    await _submit_answer(update, deps, task_number, raw_answer)


async def _submit_answer(
    update: Update,
    deps: Dependencies,
    task_number: int,
    raw_answer: str,
) -> None:
    result = deps.service.answer(
        actor_id(update),
        task_number,
        raw_answer,
        event_at_ms=event_at_ms(update),
        source_update_id=update_id(update),
    )
    if result.correct:
        text = messages.answer_correct(points=result.points)
    else:
        text = messages.answer_incorrect(
            attempt_number=result.attempt_number,
            can_retry=result.can_retry,
        )
    await send_text(update, deps, text)


async def choose_answer_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    _, stage_text, task_text = query.data.split(":")
    stage_number = int(stage_text)
    task_number = int(task_text)

    captain_id = actor_id(update)
    snapshot = deps.service.status(captain_id)
    if snapshot.state.position.is_terminal:
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.TERMINAL_PLAY_REJECTED)
        return
    if snapshot.state.position is not CaptainPosition.STAGE:
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.ANSWER_NOT_AVAILABLE)
        return
    if snapshot.state.current_stage_number != stage_number:
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.ANSWER_BUTTON_EXPIRED)
        return

    presentation = deps.service.get_stage(captain_id)
    if not any(task.task_number == task_number for task in presentation.tasks):
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.ANSWER_BUTTON_EXPIRED)
        return

    _remember_pending_answer(update, context, stage_number, task_number)
    await send_text(
        update,
        deps,
        messages.answer_prompt(task_number),
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="Ваша відповідь",
        ),
    )


async def answer_message_or_unknown(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    pending = _pending_answer(update, context)
    if pending is None:
        await send_text(update, deps, messages.UNKNOWN_MESSAGE)
        return

    message = update.effective_message
    if message is None or message.text is None:
        await send_text(update, deps, messages.ANSWER_TEXT_REQUIRED)
        return

    stage_number, task_number = pending
    snapshot = deps.service.status(actor_id(update))
    if snapshot.state.position.is_terminal:
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.TERMINAL_PLAY_REJECTED)
        return
    if (
        snapshot.state.position is not CaptainPosition.STAGE
        or snapshot.state.current_stage_number != stage_number
    ):
        _clear_pending_answer(update, context)
        await send_text(update, deps, messages.ANSWER_BUTTON_EXPIRED)
        return

    _clear_pending_answer(update, context)
    await _submit_answer(update, deps, task_number, message.text)


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    await send_text(
        update,
        deps,
        render_status(deps.service.status(actor_id(update))),
    )


async def stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    captain_id = actor_id(update)
    snapshot = deps.service.status(captain_id)
    if snapshot.state.position is CaptainPosition.INTRO:
        await deps.delivery.send_parts(
            chat_id(update),
            deps.service.get_intro(captain_id),
        )
        await send_text(update, deps, messages.INTRO_POSITION)
    elif snapshot.state.position is CaptainPosition.STAGE:
        await send_stage(
            update,
            deps,
            deps.service.get_stage(captain_id),
            answer_buttons=True,
        )
    else:
        await send_text(update, deps, render_status(snapshot))


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    await _send_help(update, deps)


async def credits(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_user(actor_id(update))
    await send_text(update, deps, messages.CREDITS)


async def captain_help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_admin(actor_id(update))
    await send_text(update, deps, messages.CAPTAIN_HELP)


async def setup_help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_admin(actor_id(update))
    await send_text(update, deps, messages.SETUP_HELP)


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_user(actor_id(update))
    if clear_pending_captain_reset(update, context) is not None:
        text = messages.RESET_CANCELLED
        reply_markup = None
    elif clear_pending_captain_add(update, context) is not None:
        text = messages.CAPTAIN_PICKER_CANCELLED
        reply_markup = ReplyKeyboardRemove()
    elif _clear_pending_answer(update, context) is not None:
        text = messages.ANSWER_CANCELLED
        reply_markup = None
    elif _clear_pending_skip(update, context):
        text = messages.SKIP_CANCELLED
        reply_markup = None
    else:
        text = messages.NOTHING_TO_CANCEL
        reply_markup = None
    await send_text(update, deps, text, reply_markup=reply_markup)


def build_handlers(
    deps: Dependencies,
) -> list[HandlerType]:
    """Build captain command handlers with dependencies bound explicitly."""

    return [
        CallbackQueryHandler(
            partial(choose_answer_task, deps=deps),
            pattern=TASK_ANSWER_CALLBACK_PATTERN,
        ),
        CommandHandler("start", partial(start, deps=deps)),
        CommandHandler("next_stage", partial(next_stage, deps=deps)),
        CommandHandler(
            "confirm_next_stage",
            partial(confirm_next_stage, deps=deps),
        ),
        CommandHandler("answer", partial(answer, deps=deps)),
        CommandHandler("status", partial(status, deps=deps)),
        CommandHandler("stage", partial(stage, deps=deps)),
        CommandHandler("help", partial(help_command, deps=deps)),
        CommandHandler("credits", partial(credits, deps=deps)),
        CommandHandler("captain_help", partial(captain_help_command, deps=deps)),
        CommandHandler("setup_help", partial(setup_help_command, deps=deps)),
        CommandHandler("cancel", partial(cancel, deps=deps)),
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            partial(answer_message_or_unknown, deps=deps),
        ),
    ]
