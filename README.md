# LOL 2026 Quest Telegram Bot

[![Checks](https://github.com/artemohanjanyan/LOL-2026-quest-tg-bot/actions/workflows/checks.yml/badge.svg?branch=main&event=push)](https://github.com/artemohanjanyan/LOL-2026-quest-tg-bot/actions/workflows/checks.yml)

Telegram bot for the 2026 online quest inspired by *Around the World in Eighty Days*.

## Quest behavior

A captain starts the clock with `/start` and receives the separately configured
intro. `/next_stage` explicitly enters the first numbered stage. Each later
transition prints the stage name and every ordered task prompt; leaving unsolved
tasks requires `/confirm_next_stage`.

Answers use `/answer <task-number> <answer>`. Comparison applies Unicode NFKC,
case folding, and whitespace normalization, but does not alter punctuation,
alphabet, transliteration, token order, or accept aliases. The default scoring
steps are `10 8 5 0`; attempts beyond the configured steps earn zero. Answers
and score steps are evaluated from current live configuration, so edits
recalculate existing progress.

The default time limit is 80 minutes. It is intentionally enforced only when
the independent 15-second sweep runs: a command remains valid after the derived
deadline until that sweep atomically commits `timed_out`. The same transaction
does not perform network I/O; the configured timeout outro is sent immediately
after the commit. Each outro part gets up to three in-memory attempts, respecting
Telegram `retry_after` and using short backoff for network errors. Retry state is
not persisted and interrupted deliveries are not resumed after restart. Both
successful and timeout outros end with the captain's current score after all
configured content parts.

Every incoming Telegram update and outgoing message request/response is logged
at `INFO` level. This deliberately includes message text, answers, captions, and
Telegram file IDs, so production logs should be treated as sensitive. Raw Bot
API URLs are not logged, which keeps the bot token out of these records.

## Content and roles

Users are authorized by stable Telegram user ID. Administrators can use captain
commands and may enroll or deactivate captains without deleting their history.
Only the owner administrator configured through `QUEST_ADMIN_ID` may add other
administrators. Run `/help` for the role-specific command list.

For testing, `/reset_captain` prepares a progress reset that must be completed
with `/confirm_reset_captain` or abandoned with `/cancel`. A confirmed reset
deletes the captain's task attempts and returns them to `not_started`, while
retaining their transition history and recording the reset as a new transition.

Multipart `/set_intro`, `/set_success_outro`, `/set_timeout_outro`, `/set_task`,
and `/broadcast` workflows accept text, photo, sticker, voice, document, video,
and video-note messages. Send parts in order and publish with `/done`; `/cancel`
discards the in-memory draft. Task drafts additionally require
`/correct_answer <answer>`. Stage names are configured directly with
`/set_stage <number> <name>`; there is deliberately no stage-level prompt.

Configuration is global and live rather than versioned. Task attempts are
retained even when content is deleted, so recreating the same
`(stage-number, task-number)` reconnects and reevaluates its history.

## Development

Install Python and the locked environment:

```bash
uv python install
uv sync
```

Create `.env`:

```dotenv
TOKEN=1111111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
QUEST_DB_PATH=quest.db
QUEST_ADMIN_ID=123456789
QUEST_ADMIN_USERNAME=organizer
```

`QUEST_ADMIN_ID` is only a bootstrap: startup ensures that Telegram ID is an
active administrator. It may be omitted after the first successful start.

Run the bot and checks:

```bash
uv run lol-2026-quest-bot
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

Install the repository's commit checks once:

```bash
uv run pre-commit install
```

The bot stores times and reports durations in UTC. Quest conversations may run in any chat
where Telegram delivers them; configure group availability through BotFather for the intended
deployment.

SQLite 3.35 or newer is the durable source of truth. The process enables foreign
keys, WAL, and a bounded busy timeout, runs packaged versioned migrations at
startup, and holds an adjacent `.lock` file so two bot processes cannot use the
same quest database. Back up the database (including WAL state, or use SQLite's
backup facility) and rehearse backup and restart procedures before the event.
