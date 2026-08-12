from quest_bot.models import ContentPart, ContentType, OutroKind
from quest_bot.storage.sqlite import SQLiteQuestStore

ADMIN_ID = 101
CAPTAIN_ID = 202
OTHER_CAPTAIN_ID = 303
BASE_TIME_MS = 1_754_000_000_000


def seed_users(store: SQLiteQuestStore) -> None:
    store.ensure_admin(ADMIN_ID, "@organizer", BASE_TIME_MS)
    store.add_captain(CAPTAIN_ID, "@passepartout", BASE_TIME_MS)


def seed_ready_quest(store: SQLiteQuestStore) -> None:
    store.replace_intro_parts([ContentPart(ContentType.TEXT, "INTRO: Pack your carpetbag")])
    store.replace_outro_parts(
        OutroKind.SUCCESS,
        [ContentPart(ContentType.TEXT, "SUCCESS OUTRO: Reform Club reached")],
    )
    store.replace_outro_parts(
        OutroKind.TIMEOUT,
        [ContentPart(ContentType.TEXT, "TIMEOUT OUTRO: The clock wins")],
    )
    store.set_stage(1, "Лондон")
    store.set_task(
        1,
        1,
        "80",
        [ContentPart(ContentType.TEXT, "TASK ONE PROMPT: How many days?")],
    )
    store.set_task(
        1,
        3,
        "Філеас Фогг",
        [
            ContentPart(ContentType.TEXT, "TASK THREE PROMPT: Name the traveller"),
            ContentPart(ContentType.DOCUMENT, "telegram-pdf-id", "Travel papers"),
            ContentPart(ContentType.VIDEO, "telegram-video-id", "A moving clue"),
        ],
    )


def seed_second_stage(store: SQLiteQuestStore) -> None:
    store.set_stage(4, "Суець")
    store.set_task(
        4,
        2,
        "Монголія",
        [ContentPart(ContentType.VIDEO_NOTE, "telegram-video-note-id")],
    )
