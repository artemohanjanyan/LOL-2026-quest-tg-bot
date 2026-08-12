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
from quest_bot.models import CaptainPosition, CaptainTransition, TaskProgress
from quest_bot.service import AnswerActivity

_REPORT_MESSAGE_LIMIT = 4_000


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
            f"{place}. {summary.user.display_name} — {summary.total_score} балів; "
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
    lines = [f"Капітан {snapshot.user.display_name}", render_status(snapshot)]
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


async def activity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    args = command_args(context)
    if len(args) != 1:
        await send_text(update, deps, messages.USAGE_ACTIVITY)
        return
    report = deps.service.captain_activity(actor_id(update), args[0])
    snapshot = report.snapshot
    lines = [f"Капітан {snapshot.user.display_name}", *render_status(snapshot).splitlines(), ""]
    if report.started_at_ms is None:
        lines.append("Хронології ще немає: подорож не розпочато.")
    else:
        lines.append(
            "Хронологія поточної подорожі (відповіді оцінено за поточними налаштуваннями):"
        )
        stage_names = {stage.stage_number: stage.name for stage in report.stages}
        events: list[CaptainTransition | AnswerActivity] = [
            *report.transitions,
            *report.answers,
        ]
        events.sort(key=_activity_sort_key)
        lines.extend(
            _render_activity_event(event, report.started_at_ms, stage_names) for event in events
        )
    for chunk in _split_report("\n".join(lines)):
        await send_text(update, deps, chunk)


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


def _activity_sort_key(event: CaptainTransition | AnswerActivity) -> tuple[int, int, int]:
    if isinstance(event, AnswerActivity):
        item = event.attempt
        return (item.event_at_ms, item.recorded_at_ms, item.source_update_id)
    return (
        event.event_at_ms,
        event.recorded_at_ms,
        event.source_update_id if event.source_update_id is not None else 2**63 - 1,
    )


def _render_activity_event(
    event: CaptainTransition | AnswerActivity,
    started_at_ms: int,
    stage_names: dict[int, str],
) -> str:
    if isinstance(event, AnswerActivity):
        attempt = event.attempt
        if event.credited:
            outcome = f"зараховано, {event.points} балів"
        elif event.currently_correct:
            outcome = "правильно за поточними налаштуваннями, повторно не зараховано"
        else:
            outcome = "неправильно"
        action = (
            f"етап {attempt.stage_number}, завдання {attempt.task_number}, "
            f"спроба {attempt.attempt_number}: «{attempt.raw_answer}» — {outcome}"
        )
    elif event.to_position is CaptainPosition.INTRO:
        action = "розпочато подорож"
    elif event.to_position is CaptainPosition.STAGE:
        assert event.to_stage_number is not None
        destination = _stage_label(event.to_stage_number, stage_names)
        if event.from_position is CaptainPosition.INTRO:
            action = f"відкрито {destination}"
        elif event.from_stage_number is not None:
            origin = _stage_label(event.from_stage_number, stage_names)
            action = f"перехід: {origin} → {destination}"
        else:
            action = f"перехід до {destination}"
        if event.skipped_unsolved_tasks:
            action += "; залишено нерозв’язані завдання"
    elif event.to_position is CaptainPosition.FINISHED:
        action = "подорож успішно завершено"
        if event.skipped_unsolved_tasks:
            action += "; залишено нерозв’язані завдання"
    elif event.to_position is CaptainPosition.TIMED_OUT:
        action = "час подорожі вичерпано"
    else:
        action = "прогрес скинуто"
    event_at_ms = (
        event.event_at_ms if isinstance(event, CaptainTransition) else event.attempt.event_at_ms
    )
    return f"{_relative_time(event_at_ms, started_at_ms)} — {action}"


def _stage_label(stage_number: int, stage_names: dict[int, str]) -> str:
    name = stage_names.get(stage_number)
    return f"етап {stage_number} «{name}»" if name is not None else f"етап {stage_number}"


def _split_report(text: str, limit: int = _REPORT_MESSAGE_LIMIT) -> tuple[str, ...]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at + (remaining[split_at : split_at + 1] == "\n") :]
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    return [
        CommandHandler("leaderboard", partial(leaderboard, deps=deps)),
        CommandHandler("progress", partial(progress, deps=deps)),
        CommandHandler("activity", partial(activity, deps=deps)),
    ]
