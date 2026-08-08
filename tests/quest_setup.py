from quest_bot.models import ContentPart, ContentType, OutroKind
from quest_bot.storage import QuestStore

ADMIN_ID = 101
CAPTAIN_ID = 202
OTHER_CAPTAIN_ID = 303
BASE_TIME_MS = 1_754_000_000_000


def seed_users(store: QuestStore) -> None:
    store.ensure_admin(ADMIN_ID, "organizer", BASE_TIME_MS)
    store.add_captain(CAPTAIN_ID, "passepartout", BASE_TIME_MS)


def seed_ready_quest(store: QuestStore) -> None:
    store.replace_intro_parts(
        [ContentPart(content_type=ContentType.TEXT, data="INTRO: Pack your carpetbag")]
    )
    store.replace_outro_parts(
        OutroKind.SUCCESS,
        [
            ContentPart(
                content_type=ContentType.TEXT,
                data="SUCCESS OUTRO: Reform Club reached",
            )
        ],
    )
    store.replace_outro_parts(
        OutroKind.TIMEOUT,
        [ContentPart(content_type=ContentType.TEXT, data="TIMEOUT OUTRO: The clock wins")],
    )
    store.set_stage(1, "Лондон")
    store.set_task(
        1,
        1,
        "80",
        [ContentPart(content_type=ContentType.TEXT, data="TASK ONE PROMPT: How many days?")],
    )
    store.set_task(
        1,
        3,
        "Філеас Фогг",
        [
            ContentPart(
                content_type=ContentType.TEXT,
                data="TASK THREE PROMPT: Name the traveller",
            ),
            ContentPart(
                content_type=ContentType.DOCUMENT,
                data="telegram-pdf-id",
                caption="Travel papers",
            ),
            ContentPart(
                content_type=ContentType.VIDEO,
                data="telegram-video-id",
                caption="A moving clue",
            ),
        ],
    )


def seed_second_stage(store: QuestStore) -> None:
    store.set_stage(4, "Суець")
    store.set_task(
        4,
        2,
        "Монголія",
        [ContentPart(content_type=ContentType.VIDEO_NOTE, data="telegram-video-note-id")],
    )
