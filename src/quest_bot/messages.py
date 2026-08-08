"""Plain-text Ukrainian copy for bot-generated messages.

Administrator-authored quest content is intentionally not handled here: it is
stored and delivered verbatim.  These helpers do not emit Telegram markup, so
captain and stage names can be interpolated without HTML/Markdown escaping.
"""

from collections.abc import Iterable

# Access and common validation -------------------------------------------------

UNKNOWN_USER = "А ви від кого? Вашого імені немає у списку мандрівників."
INACTIVE_USER = "Ваш маршрут призупинено організаторами. Зверніться до штабу експедиції."
PERMISSION_DENIED = "Ця команда доступна лише організаторам експедиції."
UNKNOWN_COMMAND = "Не впізнаю цю команду. Звіртеся з путівником: /help."
UNKNOWN_MESSAGE = (
    "Не впізнаю повідомлення. Для відповіді використайте /answer, "
    "а список команд доступний у /help."
)
TECHNICAL_ERROR = "На маршруті сталася технічна затримка. Спробуйте ще раз трохи згодом."
NOTHING_TO_CANCEL = "Немає операції, яку можна скасувати."


# Command help and usage -------------------------------------------------------

CAPTAIN_HELP = """Команди капітана:
/start — розпочати відлік і отримати вступ
/next_stage — перейти до наступного етапу
/confirm_next_stage — підтвердити перехід
  із нерозв’язаними завданнями
/answer <номер> <відповідь> — подати відповідь
/stage — знову показати поточний етап
/status — перевірити час і прогрес
/cancel — скасувати очікуване підтвердження
/help — показати цей маршрут"""

ADMIN_HELP = f"""{CAPTAIN_HELP}

Команди організатора:

Учасники, результати й зв’язок:
/add_captain <telegram_id> [username] — додати або активувати капітана
/remove_captain <telegram_id> — деактивувати капітана
/list_users — показати учасників
/leaderboard — показати таблицю результатів
/progress <username|telegram_id> — показати прогрес капітана
/broadcast — підготувати оголошення

Редагування маршруту:
/set_intro — налаштувати вступ
/set_success_outro — налаштувати успішний фінал
/set_timeout_outro — налаштувати фінал за часом
/set_stage <номер> <назва> — створити або перейменувати етап
/set_task <етап> <завдання> — налаштувати завдання
/delete_stage <номер> — видалити етап
/delete_task <етап> <завдання> — видалити завдання

Перевірка маршруту:
/show_settings — показати налаштування й готовність маршруту
/show_intro — показати вступ
/show_success_outro — показати успішний фінал
/show_timeout_outro — показати фінал за часом
/list_stages — показати етапи
/show_stage <номер> — показати етап
/show_task <етап> <завдання> — показати завдання

Правила експедиції:
/set_scores <бали...> — налаштувати шкалу балів
/set_time_limit <хвилини> — налаштувати тривалість подорожі

Робота з чернеткою:
/correct_answer <відповідь> — задати правильну відповідь
/done — опублікувати чернетку
/cancel — скасувати поточну операцію"""

OWNER_ADMIN_HELP = f"""{ADMIN_HELP}

Керування організаторами:
/add_admin <telegram_id> <username> — додати організатора"""

USAGE_ANSWER = "Формат: /answer <номер завдання> <відповідь>"
USAGE_ADD_ADMIN = "Формат: /add_admin <telegram_id> <username>"
USAGE_ADD_CAPTAIN = (
    "Формат: /add_captain <telegram_id> [username]. Username обов’язковий для нового капітана."
)
USAGE_REMOVE_CAPTAIN = "Формат: /remove_captain <telegram_id>"
USAGE_PROGRESS = "Формат: /progress <username або telegram_id>"
USAGE_SET_STAGE = "Формат: /set_stage <номер етапу> <назва>"
USAGE_SET_TASK = "Формат: /set_task <номер етапу> <номер завдання>"
USAGE_CORRECT_ANSWER = "Формат: /correct_answer <відповідь>"
USAGE_DELETE_STAGE = "Формат: /delete_stage <номер етапу>"
USAGE_DELETE_TASK = "Формат: /delete_task <номер етапу> <номер завдання>"
USAGE_SHOW_STAGE = "Формат: /show_stage <номер етапу>"
USAGE_SHOW_TASK = "Формат: /show_task <номер етапу> <номер завдання>"
USAGE_SET_SCORES = "Формат: /set_scores <бали за спроби через пробіл>"
USAGE_SET_TIME_LIMIT = "Формат: /set_time_limit <хвилини>"


def help_message(*, is_admin: bool, is_owner_admin: bool = False) -> str:
    """Return role-specific help while keeping command names in English."""

    if not is_admin:
        return CAPTAIN_HELP
    return OWNER_ADMIN_HELP if is_owner_admin else ADMIN_HELP


# Start, progress, and stage presentation -------------------------------------

QUEST_NOT_READY = "Експедиція ще не готова до відправлення."
QUEST_ALREADY_STARTED = "Ваша подорож уже триває; годинник не починається вдруге."
INTRO_POSITION = "Ви на вступній зупинці. Коли будете готові, вирушайте далі командою /next_stage."
NO_CONFIGURED_STAGES = "Етапи ще не налаштовані."
NO_CURRENT_STAGE = "Поточний етап зник із мапи. Скористайтеся /next_stage, щоб рухатися далі."


def format_duration(total_seconds: int) -> str:
    """Format a non-negative duration compactly for Ukrainian system copy."""

    seconds = max(0, int(total_seconds))
    hours, remainder = divmod(seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def status_not_started(*, limit_minutes: int) -> str:
    return (
        "Подорож ще не розпочалася. "
        f"На весь маршрут відведено {limit_minutes} хв. Команда для старту: /start"
    )


def quest_started(*, limit_minutes: int) -> str:
    """Confirm the timer using the configured limit rather than a hard-coded one."""

    return (
        f"Відлік розпочато: на маршрут відведено {limit_minutes} хв. "
        "Годинник уже рушив слідом за вами — час у дорогу!"
    )


def _active_status_lines(*, elapsed_seconds: int, limit_minutes: int, score: int) -> list[str]:
    limit_seconds = max(0, limit_minutes * 60)
    remaining_seconds = max(0, limit_seconds - max(0, elapsed_seconds))
    lines = [
        f"Час у дорозі: {format_duration(elapsed_seconds)}",
        f"Орієнтир: {limit_minutes} хв",
    ]
    if elapsed_seconds < limit_seconds:
        lines.append(f"Залишилося до орієнтира: {format_duration(remaining_seconds)}")
    else:
        lines.append("Орієнтир уже позаду, але маршрут відкритий до перевірки годинника.")
    lines.append(f"Бали: {score}")
    return lines


def status_intro(*, elapsed_seconds: int, limit_minutes: int, score: int) -> str:
    lines = ["Позиція: вступ"]
    lines.extend(
        _active_status_lines(
            elapsed_seconds=elapsed_seconds,
            limit_minutes=limit_minutes,
            score=score,
        )
    )
    return "\n".join(lines)


def status_stage(
    *,
    elapsed_seconds: int,
    limit_minutes: int,
    score: int,
    stage_number: int,
    stage_name: str,
    solved_tasks: int,
    total_tasks: int,
) -> str:
    lines = [
        f"Позиція: етап {stage_number} — {stage_name}",
        f"Завдання: {solved_tasks} із {total_tasks} розв’язано",
    ]
    lines.extend(
        _active_status_lines(
            elapsed_seconds=elapsed_seconds,
            limit_minutes=limit_minutes,
            score=score,
        )
    )
    return "\n".join(lines)


def status_finished(*, score: int) -> str:
    return "\n".join(
        (
            "Позиція: подорож успішно завершено",
            f"Бали: {score}",
        )
    )


def status_timed_out(*, score: int) -> str:
    return "\n".join(
        (
            "Позиція: час подорожі вичерпано",
            f"Бали: {score}",
        )
    )


def stage_heading(stage_number: int, stage_name: str) -> str:
    return f"Етап {stage_number}: {stage_name}"


def task_heading(
    task_number: int,
    stage_name: str,
    ordinal: int,
    total: int,
) -> str:
    return f"Завдання {task_number} — {stage_name} ({ordinal} із {total})"


# Answers and transitions ------------------------------------------------------

TASK_NOT_FOUND = "Такого завдання немає на поточному етапі. Перевірте його номер."
TASK_ALREADY_SOLVED = "Це завдання вже розв’язано — рухайтеся далі за маршрутом."
ANSWER_NOT_AVAILABLE = "Відповідати можна лише на завдання поточного етапу."
TERMINAL_PLAY_REJECTED = "Ця подорож уже завершена; нові відповіді та переходи закрито."


def answer_correct(*, points: int) -> str:
    if points > 0:
        return f"Точна відповідь! Ви здобуваєте {points} балів і продовжуєте подорож."
    return "Точна відповідь! Завдання розв’язано, хоча ця спроба вже не додає балів."


def answer_incorrect(*, attempt_number: int) -> str:
    return f"Відповідь не збігається. Це була спроба №{attempt_number}; звірте курс і спробуйте ще."


def skip_warning(unsolved_task_numbers: Iterable[int]) -> str:
    numbers = ", ".join(str(number) for number in unsolved_task_numbers)
    return (
        f"Нерозв’язані завдання: {numbers}. "
        "Щоб залишити їх позаду, надішліть /confirm_next_stage. "
        "Щоб лишитися на етапі, надішліть /cancel."
    )


SKIP_CONFIRMED = "Перехід підтверджено. Нерозв’язані завдання залишаються позаду."
SKIP_CANCELLED = "Перехід скасовано. Ви залишаєтеся на поточному етапі."
NO_SKIP_CONFIRMATION = "Немає переходу, який очікує підтвердження."


# Terminal states and delivery -------------------------------------------------

QUEST_FINISHED = "Маршрут пройдено! Ви дісталися фінішу раніше, ніж експедиція зачинила шлях."


# Admin workflow ---------------------------------------------------------------

DRAFT_READY = "Чернетку відкрито. Надсилайте частини в потрібному порядку, потім /done."
DRAFT_CANCELLED = "Чернетку скасовано; опублікований маршрут не змінився."
DRAFT_PUBLISHED = "Чернетку опубліковано. Мапу експедиції оновлено."
NO_ACTIVE_DRAFT = "Немає відкритої чернетки."
CORRECT_ANSWER_SAVED = "Правильну відповідь додано до чернетки завдання."
CONTENT_PART_ADDED = "Частину додано до чернетки."
CONTENT_DELETED = "Вміст видалено з поточної мапи."
STAGE_NOT_FOUND = "Етап із таким номером не знайдено."
CAPTAIN_NOT_FOUND = "Капітана не знайдено. Перевірте username або Telegram ID."


def captain_added(username: str, telegram_id: int) -> str:
    return f"Капітана {username} ({telegram_id}) додано до експедиції."


def admin_added(username: str, telegram_id: int) -> str:
    return f"Організатора {username} ({telegram_id}) додано до штабу експедиції."


def captain_removed(username: str, telegram_id: int) -> str:
    return f"Капітана {username} ({telegram_id}) деактивовано; історію подорожі збережено."


def stage_saved(stage_number: int, stage_name: str) -> str:
    return f"Етап {stage_number} — {stage_name} збережено."


def scores_updated(points: Iterable[int]) -> str:
    rendered = ", ".join(str(point) for point in points)
    return f"Шкалу балів оновлено: {rendered}."


def time_limit_updated(minutes: int) -> str:
    return f"Орієнтир подорожі оновлено: {minutes} хв. Зміна вже діє для активних капітанів."


def quest_settings_summary(
    *,
    time_limit_minutes: int,
    score_steps: Iterable[int],
    intro_part_count: int,
    success_outro_part_count: int,
    timeout_outro_part_count: int,
    stages: Iterable[tuple[int, str, int]],
    ready: bool,
) -> str:
    def content_state(part_count: int) -> str:
        return f"налаштовано (частин: {part_count})" if part_count else "не налаштовано"

    lines = [
        "Путівник експедиції:",
        f"Ліміт часу: {time_limit_minutes} хв.",
        f"Шкала балів за спробами: {', '.join(str(point) for point in score_steps)}.",
        f"Вступ: {content_state(intro_part_count)}.",
        f"Успішний фінал: {content_state(success_outro_part_count)}.",
        f"Фінал за часом: {content_state(timeout_outro_part_count)}.",
    ]
    stage_rows = tuple(stages)
    if stage_rows:
        lines.append("Етапи:")
        lines.extend(
            f"{number}. {name} — завдань: {task_count}" for number, name, task_count in stage_rows
        )
    else:
        lines.append("Етапи: не налаштовано.")
    readiness = "маршрут готовий до старту" if ready else "маршрут ще не готовий до старту"
    lines.append(f"Готовність: {readiness}.")
    return "\n".join(lines)


def broadcast_complete(*, delivered: int, failed: int) -> str:
    return f"Оголошення завершено. Доставлено: {delivered}; помилок: {failed}."
