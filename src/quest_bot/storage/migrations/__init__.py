"""Packaged SQLite schema migrations."""

from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine


def upgrade_database(engine: Engine) -> None:
    """Upgrade a database to the packaged Alembic head revision."""

    config = Config()
    with (
        resources.as_file(resources.files(__package__)) as migration_path,
        engine.connect() as connection,
    ):
        config.set_main_option("script_location", str(migration_path))
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        connection.commit()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
