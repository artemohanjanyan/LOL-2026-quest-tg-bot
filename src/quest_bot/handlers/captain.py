"""Captain-facing Telegram command handlers."""

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    chat_id,
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


async def _deliver_advance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deps: Dependencies,
    result: AdvanceResult,
    *,
    skipped: bool,
) -> None:
    _clear_pending_skip(update, context)
    if result.finished:
        prefix = (ContentPart(ContentType.TEXT, messages.SKIP_CONFIRMED),) if skipped else ()
        await deps.delivery.send_outro(
            chat_id(update),
            (
                *prefix,
                ContentPart(ContentType.TEXT, messages.QUEST_FINISHED),
                *result.outro_parts,
            ),
        )
        return
    if skipped:
        await send_text(update, deps, messages.SKIP_CONFIRMED)
    if result.presentation is not None:
        await send_stage(update, deps, result.presentation)


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
        await send_stage(update, deps, deps.service.get_stage(actor_id(update)))


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
    parsed = parse_numbered_text(context)
    if parsed is None:
        await send_text(update, deps, messages.USAGE_ANSWER)
        return
    task_number, raw_answer = parsed
    if task_number <= 0:
        await send_text(update, deps, messages.USAGE_ANSWER)
        return

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
        text = messages.answer_incorrect(attempt_number=result.attempt_number)
    await send_text(update, deps, text)


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
        await send_stage(update, deps, deps.service.get_stage(captain_id))
    else:
        await send_text(update, deps, render_status(snapshot))


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    user = deps.service.require_user(actor_id(update))
    await send_text(
        update,
        deps,
        messages.help_message(
            is_admin=user.role is UserRole.ADMIN,
            is_owner_admin=deps.service.is_owner_admin(user.user_id),
        ),
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    deps.service.require_user(actor_id(update))
    if clear_pending_captain_reset(update, context) is not None:
        text = messages.RESET_CANCELLED
    elif _clear_pending_skip(update, context):
        text = messages.SKIP_CANCELLED
    else:
        text = messages.NOTHING_TO_CANCEL
    await send_text(update, deps, text)


def build_handlers(
    deps: Dependencies,
) -> list[HandlerType]:
    """Build captain command handlers with dependencies bound explicitly."""

    return [
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
        CommandHandler("cancel", partial(cancel, deps=deps)),
    ]
