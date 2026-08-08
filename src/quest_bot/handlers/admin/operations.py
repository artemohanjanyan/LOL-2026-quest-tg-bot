"""Administrator live settings and delivery operations."""

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.errors import ContentValidationError
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    parse_integer_args,
    send_text,
)


async def set_scores(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    points = parse_integer_args(context)
    if not points:
        await send_text(update, deps, messages.USAGE_SET_SCORES)
        return
    try:
        saved = deps.service.set_scores(actor_id(update), points)
    except ContentValidationError:
        await send_text(update, deps, messages.USAGE_SET_SCORES)
        return
    await send_text(update, deps, messages.scores_updated(saved))


async def set_time_limit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=1)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_SET_TIME_LIMIT)
        return
    (minutes,) = numbers
    try:
        saved = deps.service.set_time_limit(actor_id(update), minutes)
    except ContentValidationError:
        await send_text(update, deps, messages.USAGE_SET_TIME_LIMIT)
        return
    await send_text(update, deps, messages.time_limit_updated(saved))


async def show_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    snapshot = deps.service.show_settings(actor_id(update))
    await send_text(
        update,
        deps,
        messages.quest_settings_summary(
            time_limit_minutes=snapshot.time_limit_minutes,
            score_steps=snapshot.score_steps,
            intro_part_count=snapshot.intro_part_count,
            success_outro_part_count=snapshot.success_outro_part_count,
            timeout_outro_part_count=snapshot.timeout_outro_part_count,
            stages=(
                (stage.stage.stage_number, stage.stage.name, stage.task_count)
                for stage in snapshot.stages
            ),
            ready=snapshot.ready,
        ),
    )


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("show_settings", partial(show_settings, deps=deps)),
        CommandHandler("set_scores", partial(set_scores, deps=deps)),
        CommandHandler("set_time_limit", partial(set_time_limit, deps=deps)),
    ]
