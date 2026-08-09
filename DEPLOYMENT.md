# Production deployment

The recommended production setup is a single bot process managed by `systemd`,
with `uv` used during deployment to create a locked virtual environment. Run the
installed executable directly from that environment rather than synchronizing
dependencies on every service start.

The bot uses long polling, so it does not need a reverse proxy or an inbound
network port. It only needs outbound HTTPS access to Telegram.

## Server layout

One possible layout is:

```text
/opt/lol-2026-quest-tg-bot/          application checkout
/opt/lol-2026-quest-tg-bot/.venv/    locked Python environment
/etc/lol-2026-quest-tg-bot.env       secrets and process configuration
/var/lib/lol-2026-quest-tg-bot/      SQLite database, WAL, and lock file
```

Run the service as a dedicated unprivileged account such as `questbot`, not as
root. Keep the source tree read-only to that account and make only the state
directory writable.

The environment file should contain absolute production paths:

```dotenv
TOKEN=replace-with-the-current-token
QUEST_DB_PATH=/var/lib/lol-2026-quest-tg-bot/quest.db
QUEST_ADMIN_ID=123456789
QUEST_ADMIN_USERNAME=organizer
```

Protect it:

```bash
sudo chown root:root /etc/lol-2026-quest-tg-bot.env
sudo chmod 600 /etc/lol-2026-quest-tg-bot.env
```

## Installing with uv

Install the committed dependency set during deployment:

```bash
cd /opt/lol-2026-quest-tg-bot
uv sync --locked --no-dev --no-editable
```

`--locked` rejects an out-of-date lock file rather than changing dependencies on
the server. `--no-dev` excludes test and lint tools. `--no-editable` installs an
immutable copy of the application into `.venv`, so deployment changes take
effect only after another sync and restart.

The preferred runtime command is then:

```bash
/opt/lol-2026-quest-tg-bot/.venv/bin/lol-2026-quest-bot
```

Running through `uv` is also valid, but ordinary `uv run` checks and synchronizes
the environment before launching. If keeping `uv` in the service command, sync
explicitly during deployment and use:

```bash
uv run --locked --no-sync lol-2026-quest-bot
```

Directly invoking the `.venv` executable makes service startup independent of
package indexes, the uv cache, and dependency synchronization.

## systemd service

Create `/etc/systemd/system/lol-2026-quest-bot.service`:

```ini
[Unit]
Description=LOL 2026 Telegram Quest Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=questbot
Group=questbot
WorkingDirectory=/opt/lol-2026-quest-tg-bot
EnvironmentFile=/etc/lol-2026-quest-tg-bot.env
ExecStart=/opt/lol-2026-quest-tg-bot/.venv/bin/lol-2026-quest-bot

Restart=on-failure
RestartSec=5
TimeoutStopSec=30

StateDirectory=lol-2026-quest-tg-bot
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=lol-2026-quest-bot

[Install]
WantedBy=multi-user.target
```

`StateDirectory=` creates `/var/lib/lol-2026-quest-tg-bot` and keeps it writable
when `ProtectSystem=strict` is active. That directory must hold the database and
its associated `-wal`, `-shm`, and `.lock` files.

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lol-2026-quest-bot
```

Routine service commands:

```bash
sudo systemctl status lol-2026-quest-bot
sudo systemctl restart lol-2026-quest-bot
sudo systemctl stop lol-2026-quest-bot
sudo journalctl -u lol-2026-quest-bot -f
```

The application's database lock remains a second line of defense against two bot
instances using the same database.

## Updating the deployment

A simple manual update sequence is:

```bash
cd /opt/lol-2026-quest-tg-bot
git pull --ff-only
uv sync --locked --no-dev --no-editable
sudo systemctl restart lol-2026-quest-bot
sudo systemctl status lol-2026-quest-bot
```

Run the automated test suite before deploying a new commit. If the update changes
database migrations, back up the database before restarting into the new code.

## Log volume and retention

Logs normally consume disk space, not application memory. A persistent systemd
journal lives under `/var/log/journal`. A volatile journal under
`/run/log/journal` uses tmpfs and therefore memory-backed storage.

The application emits a successful timeout-sweep heartbeat every five minutes.
Other log volume grows in proportion to captain and administrator activity.

Inspect journal usage with:

```bash
journalctl --disk-usage
journalctl -u lol-2026-quest-bot --since "14 days ago"
```

Optional global journal limits can be placed in
`/etc/systemd/journald.conf.d/retention.conf`:

```ini
[Journal]
SystemMaxUse=500M
MaxRetentionSec=14day
```

Then apply them:

```bash
sudo systemctl restart systemd-journald
```

These limits apply to the whole system journal, not only this bot. If independent
per-bot retention is required, write the service output to a dedicated file and
manage it with `logrotate` instead.

## Database backups

The SQLite database contains captain roles, quest configuration, progress,
attempts, and transition history. Back it up before deployments and at least
daily during the event.

Do not copy only the main database file while the bot is running because recent
data may be in the WAL file. Use SQLite's online backup command:

```bash
sqlite3 /var/lib/lol-2026-quest-tg-bot/quest.db \
  ".backup '/var/backups/lol-2026-quest/latest.db'"
```

Keep timestamped copies outside the application checkout and periodically verify
that a backup opens successfully. The simplest fully consistent alternative is
to stop the service briefly, copy the complete database state, and start it
again.
