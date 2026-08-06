"""Administrator quest-content drafts and inspection commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from quest_bot import messages
from quest_bot.errors import ContentValidationError
from quest_bot.handlers.common import (
    Dependencies,
    HandlerType,
    actor_id,
    chat_id,
    command_args,
    content_part_from_message,
    parse_integer_args,
    parse_numbered_text,
    send_stage,
    send_text,
    user_data,
)
from quest_bot.models import ContentPart, OutroKind

DRAFT_CONTENT = 1
DRAFT_KEY_PREFIX = "quest_content_draft:"


class DraftKind(StrEnum):
    INTRO = "intro"
    SUCCESS_OUTRO = "success_outro"
    TIMEOUT_OUTRO = "timeout_outro"
    TASK = "task"
    BROADCAST = "broadcast"


@dataclass(slots=True)
class ContentDraft:
    kind: DraftKind
    stage_number: int | None = None
    task_number: int | None = None
    correct_answer: str | None = None
    parts: list[ContentPart] = field(default_factory=list)


def _draft_key(update: Update) -> str:
    return f"{DRAFT_KEY_PREFIX}{chat_id(update)}"


def _get_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> ContentDraft | None:
    value = user_data(context).get(_draft_key(update))
    return value if isinstance(value, ContentDraft) else None


def _put_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft: ContentDraft,
) -> None:
    user_data(context)[_draft_key(update)] = draft


def _clear_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_data(context).pop(_draft_key(update), None)


async def _begin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
    kind: DraftKind,
) -> int:
    deps.service.require_admin(actor_id(update))
    _put_draft(update, context, ContentDraft(kind))
    await send_text(update, deps, messages.DRAFT_READY)
    return DRAFT_CONTENT


async def begin_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> int:
    deps.service.require_admin(actor_id(update))
    numbers = parse_integer_args(context, count=2)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_SET_TASK)
        return ConversationHandler.END
    stage_number, task_number = numbers
    if stage_number <= 0 or task_number <= 0:
        await send_text(update, deps, messages.USAGE_SET_TASK)
        return ConversationHandler.END
    _put_draft(
        update,
        context,
        ContentDraft(
            DraftKind.TASK,
            stage_number=stage_number,
            task_number=task_number,
        ),
    )
    await send_text(
        update,
        deps,
        messages.DRAFT_READY
        + "\nДодайте промпт завдання та задайте відповідь командою /correct_answer.",
    )
    return DRAFT_CONTENT


async def add_part(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> int:
    deps.service.require_admin(actor_id(update))
    draft = _get_draft(update, context)
    if draft is None:
        await send_text(update, deps, messages.NO_ACTIVE_DRAFT)
        return ConversationHandler.END
    message = update.effective_message
    part = content_part_from_message(message) if message is not None else None
    if part is None:
        await send_text(
            update,
            deps,
            "Цей тип повідомлення не входить до вантажу експедиції.",
        )
        return DRAFT_CONTENT
    draft.parts.append(part)
    await send_text(update, deps, messages.CONTENT_PART_ADDED)
    return DRAFT_CONTENT


async def correct_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> int:
    deps.service.require_admin(actor_id(update))
    draft = _get_draft(update, context)
    if draft is None or draft.kind is not DraftKind.TASK:
        await send_text(update, deps, messages.NO_ACTIVE_DRAFT)
        return DRAFT_CONTENT
    answer = " ".join(command_args(context))
    if not answer.strip():
        await send_text(update, deps, messages.USAGE_CORRECT_ANSWER)
        return DRAFT_CONTENT
    draft.correct_answer = answer
    await send_text(update, deps, messages.CORRECT_ANSWER_SAVED)
    return DRAFT_CONTENT


async def done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> int:
    draft = _get_draft(update, context)
    if draft is None:
        await send_text(update, deps, messages.NO_ACTIVE_DRAFT)
        return ConversationHandler.END
    admin_id = actor_id(update)
    deps.service.require_admin(admin_id)
    if not draft.parts:
        await send_text(update, deps, "Додайте хоча б одну частину перед /done.")
        return DRAFT_CONTENT
    try:
        if draft.kind is DraftKind.INTRO:
            deps.service.replace_intro(admin_id, draft.parts)
        elif draft.kind is DraftKind.SUCCESS_OUTRO:
            deps.service.replace_outro(admin_id, OutroKind.SUCCESS, draft.parts)
        elif draft.kind is DraftKind.TIMEOUT_OUTRO:
            deps.service.replace_outro(admin_id, OutroKind.TIMEOUT, draft.parts)
        elif draft.kind is DraftKind.TASK:
            if draft.correct_answer is None:
                await send_text(
                    update,
                    deps,
                    "Спершу задайте правильну відповідь командою /correct_answer.",
                )
                return DRAFT_CONTENT
            assert draft.stage_number is not None
            assert draft.task_number is not None
            deps.service.set_task(
                admin_id,
                draft.stage_number,
                draft.task_number,
                draft.correct_answer,
                draft.parts,
            )
        else:
            delivered = 0
            failed = 0
            for recipient in deps.service.active_recipients(admin_id):
                try:
                    await deps.delivery.send_parts(recipient.user_id, draft.parts)
                except TelegramError:
                    failed += 1
                else:
                    delivered += 1
            await send_text(
                update,
                deps,
                messages.broadcast_complete(delivered=delivered, failed=failed),
            )
    except ContentValidationError:
        await send_text(update, deps, "Чернетка не пройшла перевірку; виправте її вміст.")
        return DRAFT_CONTENT
    _clear_draft(update, context)
    if draft.kind is not DraftKind.BROADCAST:
        await send_text(update, deps, messages.DRAFT_PUBLISHED)
    return ConversationHandler.END


async def cancel_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> int:
    deps.service.require_admin(actor_id(update))
    _clear_draft(update, context)
    await send_text(update, deps, messages.DRAFT_CANCELLED)
    return ConversationHandler.END


async def set_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    parsed = parse_numbered_text(context)
    if parsed is None:
        await send_text(update, deps, messages.USAGE_SET_STAGE)
        return
    number, name = parsed
    try:
        stage = deps.service.set_stage(actor_id(update), number, name)
    except ContentValidationError:
        await send_text(update, deps, messages.USAGE_SET_STAGE)
        return
    await send_text(update, deps, messages.stage_saved(stage.stage_number, stage.name))


async def delete_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=1)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_DELETE_STAGE)
        return
    (number,) = numbers
    deleted = deps.service.delete_stage(actor_id(update), number)
    await send_text(
        update,
        deps,
        messages.CONTENT_DELETED if deleted else messages.NO_CURRENT_STAGE,
    )


async def delete_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=2)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_DELETE_TASK)
        return
    stage_number, task_number = numbers
    deleted = deps.service.delete_task(actor_id(update), stage_number, task_number)
    await send_text(update, deps, messages.CONTENT_DELETED if deleted else messages.TASK_NOT_FOUND)


async def list_stages(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    stages = deps.service.list_stages(actor_id(update))
    if not stages:
        await send_text(update, deps, messages.NO_STAGES)
        return
    await send_text(
        update,
        deps,
        "\n".join(f"{stage.stage_number} — {stage.name}" for stage in stages),
    )


async def show_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=1)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_SHOW_STAGE)
        return
    (stage_number,) = numbers
    presentation = deps.service.show_stage(actor_id(update), stage_number)
    await send_stage(update, deps, presentation)
    for task in presentation.tasks:
        await send_text(
            update,
            deps,
            f"Правильна відповідь до завдання {task.task_number}: {task.correct_answer_raw}",
        )


async def show_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
) -> None:
    numbers = parse_integer_args(context, count=2)
    if numbers is None:
        await send_text(update, deps, messages.USAGE_SHOW_TASK)
        return
    stage_number, task_number = numbers
    task = deps.service.show_task(actor_id(update), stage_number, task_number)
    await send_text(
        update,
        deps,
        f"Етап {stage_number}, завдання {task_number}.\n"
        f"Правильна відповідь: {task.correct_answer_raw}",
    )
    await deps.delivery.send_parts(chat_id(update), task.prompt_parts)


async def show_global_content(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    deps: Dependencies,
    kind: DraftKind,
) -> None:
    admin_id = actor_id(update)
    if kind is DraftKind.INTRO:
        parts = deps.service.get_intro_for_admin(admin_id)
    elif kind is DraftKind.SUCCESS_OUTRO:
        parts = deps.service.get_outro_for_admin(admin_id, OutroKind.SUCCESS)
    else:
        parts = deps.service.get_outro_for_admin(admin_id, OutroKind.TIMEOUT)
    if not parts:
        await send_text(update, deps, "Цей розділ маршруту ще порожній.")
        return
    await deps.delivery.send_parts(chat_id(update), parts)


def build_handlers(deps: Dependencies) -> list[HandlerType]:
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler(
                "set_intro",
                partial(_begin, deps=deps, kind=DraftKind.INTRO),
            ),
            CommandHandler(
                "set_success_outro",
                partial(_begin, deps=deps, kind=DraftKind.SUCCESS_OUTRO),
            ),
            CommandHandler(
                "set_timeout_outro",
                partial(_begin, deps=deps, kind=DraftKind.TIMEOUT_OUTRO),
            ),
            CommandHandler("set_task", partial(begin_task, deps=deps)),
            CommandHandler(
                "broadcast",
                partial(_begin, deps=deps, kind=DraftKind.BROADCAST),
            ),
        ],
        states={
            DRAFT_CONTENT: [
                CommandHandler("correct_answer", partial(correct_answer, deps=deps)),
                CommandHandler("done", partial(done, deps=deps)),
                CommandHandler("cancel", partial(cancel_draft, deps=deps)),
                MessageHandler(filters.ALL & ~filters.COMMAND, partial(add_part, deps=deps)),
            ]
        },
        fallbacks=[CommandHandler("cancel", partial(cancel_draft, deps=deps))],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=False,
    )
    return [
        conversation,
        CommandHandler("set_stage", partial(set_stage, deps=deps)),
        CommandHandler("delete_stage", partial(delete_stage, deps=deps)),
        CommandHandler("delete_task", partial(delete_task, deps=deps)),
        CommandHandler("list_stages", partial(list_stages, deps=deps)),
        CommandHandler("show_stage", partial(show_stage, deps=deps)),
        CommandHandler("show_task", partial(show_task, deps=deps)),
        CommandHandler(
            "show_intro",
            partial(show_global_content, deps=deps, kind=DraftKind.INTRO),
        ),
        CommandHandler(
            "show_success_outro",
            partial(show_global_content, deps=deps, kind=DraftKind.SUCCESS_OUTRO),
        ),
        CommandHandler(
            "show_timeout_outro",
            partial(show_global_content, deps=deps, kind=DraftKind.TIMEOUT_OUTRO),
        ),
    ]
