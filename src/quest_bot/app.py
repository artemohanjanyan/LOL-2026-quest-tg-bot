"""python-telegram-bot application factory and periodic job wiring."""

from __future__ import annotations

import asyncio
import logging

from telegram.ext import AIORateLimiter, ApplicationBuilder, ContextTypes
from telegram.request import BaseRequest

from quest_bot.config import Settings
from quest_bot.delivery import TelegramDelivery
from quest_bot.handlers.common import ApplicationType, Dependencies
from quest_bot.handlers.registry import COMMANDS, register_handlers
from quest_bot.service import QuestService

LOGGER = logging.getLogger(__name__)
DEPENDENCIES_KEY = "quest_dependencies"
SWEEP_INTERVAL_KEY = "quest_sweep_interval"
OUTRO_CONCURRENCY = 5


def _dependencies(application: ApplicationType) -> Dependencies:
    value = application.bot_data[DEPENDENCIES_KEY]
    if not isinstance(value, Dependencies):
        raise RuntimeError("quest dependencies were not configured")
    return value


async def _run_timeout_sweep(deps: Dependencies) -> None:
    sweep = deps.service.sweep_timeouts()
    if not sweep.expired_user_ids:
        return
    semaphore = asyncio.Semaphore(OUTRO_CONCURRENCY)

    async def deliver(user_id: int) -> tuple[int, int]:
        async with semaphore:
            report = await deps.delivery.send_outro(user_id, sweep.outro_parts)
            return report.sent_parts, report.failed_parts

    reports = await asyncio.gather(*(deliver(user_id) for user_id in sweep.expired_user_ids))
    LOGGER.info(
        "Quest timeout sweep completed",
        extra={
            "expired_captains": len(sweep.expired_user_ids),
            "delivered_parts": sum(report[0] for report in reports),
            "failed_parts": sum(report[1] for report in reports),
        },
    )


async def _timeout_sweep(context: ContextTypes.DEFAULT_TYPE) -> None:
    value = context.application.bot_data[DEPENDENCIES_KEY]
    if not isinstance(value, Dependencies):
        raise RuntimeError("quest dependencies were not configured")
    await _run_timeout_sweep(value)


async def _post_init(application: ApplicationType) -> None:
    deps = _dependencies(application)
    await application.bot.set_my_commands(COMMANDS)
    await _run_timeout_sweep(deps)
    interval = int(application.bot_data[SWEEP_INTERVAL_KEY])
    if application.job_queue is None:
        raise RuntimeError("PTB JobQueue extra is required")
    application.job_queue.run_repeating(
        _timeout_sweep,
        interval=interval,
        first=interval,
        name="quest-timeout-sweep",
    )


def create_application(
    settings: Settings,
    service: QuestService,
    *,
    request: BaseRequest | None = None,
) -> ApplicationType:
    """Build an application without opening files or starting long polling."""

    builder = (
        ApplicationBuilder()
        .token(settings.token)
        .concurrent_updates(False)
        .rate_limiter(
            AIORateLimiter(
                overall_max_rate=settings.delivery_rate_per_second,
                overall_time_period=1,
            )
        )
        .post_init(_post_init)
    )
    if request is not None:
        builder = builder.request(request)
    application = builder.build()
    deps = Dependencies(service, TelegramDelivery(application.bot))
    application.bot_data[DEPENDENCIES_KEY] = deps
    application.bot_data[SWEEP_INTERVAL_KEY] = settings.sweep_interval_seconds
    register_handlers(application, deps)
    return application
