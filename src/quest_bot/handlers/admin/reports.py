"""Administrator progress and leaderboard reports."""

from __future__ import annotations

from functools import partial

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from quest_bot import messages
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    command_args,
    render_status,
    send_text,
)
from quest_bot.models import CaptainPosition


async def leaderboard(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    rows = deps.service.leaderboard(actor_id(update))
    if not rows:
        await send_text(update, deps, "У таблиці ще немає капітанів.")
        return
    lines = ["Таблиця експедиції:"]
    position_names = {
        CaptainPosition.NOT_STARTED: "ще не стартував",
        CaptainPosition.INTRO: "на вступі",
        CaptainPosition.STAGE: "у дорозі",
        CaptainPosition.FINISHED: "фінішував",
        CaptainPosition.TIMED_OUT: "час вичерпано",
    }
    for place, summary in enumerate(rows, start=1):
        position = position_names[summary.state.position]
        lines.append(
            f"{place}. @{summary.user.username} — {summary.total_score} балів; "
            f"{summary.solved_tasks}/{summary.total_tasks}; {position}"
        )
    await send_text(update, deps, "\n".join(lines))


async def progress(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if len(args) != 1:
        await send_text(update, deps, messages.USAGE_PROGRESS)
        return
    snapshot = deps.service.captain_status(actor_id(update), args[0])
    prefix = f"Капітан @{snapshot.user.username}\n"
    text = render_status(snapshot)
    if snapshot.state.position is CaptainPosition.STAGE:
        stage_number = snapshot.state.current_stage_number
        assert stage_number is not None
        attempt_counts = deps.service.captain_attempt_counts(
            actor_id(update),
            args[0],
            stage_number=stage_number,
        )
        if attempt_counts:
            rendered = ", ".join(f"№{number}: {count} спроб" for number, count in attempt_counts)
            text = f"{text}\nСпроби на цьому етапі: {rendered}"
    await send_text(update, deps, prefix + text)


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("leaderboard", partial(leaderboard, deps=deps)),
        CommandHandler("progress", partial(progress, deps=deps)),
    ]
