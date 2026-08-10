"""Administrator progress and leaderboard reports."""

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
from quest_bot.models import CaptainPosition, TaskProgress


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
    report = deps.service.captain_progress(actor_id(update), args[0])
    snapshot = report.snapshot
    lines = [f"Капітан @{snapshot.user.username}", render_status(snapshot)]
    solved_tasks = sum(task.solved for task in report.tasks)
    attempt_count = sum(task.attempt_count for task in report.tasks)
    lines.extend(
        (
            f"Загалом: {solved_tasks} із {len(report.tasks)} завдань розв’язано; "
            f"спроб: {attempt_count}.",
            "",
            "Деталі від старту:",
        )
    )
    if report.started_at_ms is None:
        lines.append("Подорож ще не розпочато.")
    elif not report.tasks:
        lines.append("Завдань ще немає.")
    else:
        stage_names = {stage.stage_number: stage.name for stage in report.stages}
        current_stage_number = None
        for task in report.tasks:
            if task.stage_number != current_stage_number:
                current_stage_number = task.stage_number
                name = stage_names.get(task.stage_number, "невідомий етап")
                lines.extend(("", f"Етап {task.stage_number}: {name}"))
            lines.append(_render_task_progress(task, report.started_at_ms))
    await send_text(update, deps, "\n".join(lines))


def _render_task_progress(task: TaskProgress, started_at_ms: int) -> str:
    if task.solved:
        assert task.solved_attempt_number is not None
        assert task.solved_at_ms is not None
        attempts = f"спроб: {task.solved_attempt_number}"
        if task.attempt_count != task.solved_attempt_number:
            attempts = (
                f"зарахована спроба: {task.solved_attempt_number}; "
                f"записано спроб: {task.attempt_count}"
            )
        return (
            f"№{task.task_number} — розв’язано {_relative_time(task.solved_at_ms, started_at_ms)}; "
            f"{attempts}; балів: {task.points}"
        )
    if task.attempt_count:
        assert task.last_attempt_at_ms is not None
        return (
            f"№{task.task_number} — не розв’язано; спроб: {task.attempt_count}; "
            f"остання {_relative_time(task.last_attempt_at_ms, started_at_ms)}"
        )
    return f"№{task.task_number} — без спроб"


def _relative_time(event_at_ms: int, started_at_ms: int) -> str:
    seconds = max(0, (event_at_ms - started_at_ms) // 1_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"+{hours}:{minutes:02d}:{seconds:02d}"
    return f"+{minutes:02d}:{seconds:02d}"


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("leaderboard", partial(leaderboard, deps=deps)),
        CommandHandler("progress", partial(progress, deps=deps)),
    ]
