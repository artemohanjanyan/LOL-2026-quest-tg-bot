from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import URL, engine_from_config, pool

from quest_bot.config import DatabaseSettings
from quest_bot.storage.schema import Base, BoolInt, EnumValue

config = context.config
target_metadata = Base.metadata

if config.attributes.get("connection") is None:
    database_url = URL.create(
        "sqlite+pysqlite",
        database=str(DatabaseSettings().database_path),
    )
    config.set_main_option(
        "sqlalchemy.url",
        database_url.render_as_string(hide_password=False).replace("%", "%%"),
    )


def render_item(
    type_: str,
    item: Any,
    _context: AutogenContext,
) -> str | Literal[False]:
    """Render application types as their stable database representation."""

    if type_ == "type" and isinstance(item, EnumValue):
        return "sa.Text()"
    if type_ == "type" and isinstance(item, BoolInt):
        return "sa.Integer()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        context.configure(
            connection=supplied_connection,
            target_metadata=target_metadata,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
