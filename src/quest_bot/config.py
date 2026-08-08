"""Validated process settings loaded once by the composition root."""

from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, PositiveInt, StringConstraints, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    database_path: Path = Field(default=Path("quest.db"), validation_alias="QUEST_DB_PATH")


class Settings(DatabaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUEST_")

    token: NonBlankString = Field(default="", validation_alias="TOKEN")
    bootstrap_admin_id: PositiveInt | None = Field(
        default=None,
        validation_alias="QUEST_ADMIN_ID",
    )
    bootstrap_admin_username: NonBlankString = Field(
        default="admin",
        validation_alias="QUEST_ADMIN_USERNAME",
    )
    sweep_interval_seconds: PositiveInt = 15
    database_busy_timeout_ms: PositiveInt = 5_000
    delivery_rate_per_second: PositiveInt = 20

    @model_validator(mode="after")
    def validate_bootstrap_admin(self) -> Self:
        if self.bootstrap_admin_id is not None and not self.bootstrap_admin_username.lstrip("@"):
            raise ValueError("bootstrap admin username must contain characters other than '@'")
        return self
