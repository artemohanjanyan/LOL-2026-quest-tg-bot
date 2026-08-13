"""Production composition root."""

import logging

from telegram import Update

from quest_bot import __version__
from quest_bot.app import create_application
from quest_bot.config import Settings
from quest_bot.models import utc_now_ms
from quest_bot.service import QuestService
from quest_bot.storage.sqlite import SQLiteQuestStore

POLLING_UPDATE_TYPES = (Update.MESSAGE, Update.CALLBACK_QUERY)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    settings = Settings.from_env()
    with SQLiteQuestStore.open(
        settings.database_path,
        busy_timeout_ms=settings.database_busy_timeout_ms,
        lock_instance=True,
    ) as store:
        if settings.bootstrap_admin_id is not None:
            store.ensure_admin(
                settings.bootstrap_admin_id,
                settings.bootstrap_admin_display_name,
                utc_now_ms(),
            )
        service = QuestService(store, owner_admin_id=settings.bootstrap_admin_id)
        application = create_application(settings, service)
        logging.getLogger(__name__).info(
            "Starting quest bot %s with database %s at schema %s and %ss sweep",
            __version__,
            settings.database_path,
            store.schema_version,
            settings.sweep_interval_seconds,
        )
        application.run_polling(allowed_updates=POLLING_UPDATE_TYPES)


if __name__ == "__main__":
    main()
