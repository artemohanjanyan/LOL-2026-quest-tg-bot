"""Validated process settings loaded once by the composition root."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    database_path: Path
    bootstrap_admin_id: int | None = None
    bootstrap_admin_display_name: str = "@admin"
    sweep_interval_seconds: int = 15
    delivery_rate_per_second: int = 20

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("TOKEN must not be empty")
        if self.sweep_interval_seconds <= 0:
            raise ValueError("sweep interval must be positive")
        if self.delivery_rate_per_second <= 0:
            raise ValueError("delivery rate must be positive")
        if self.bootstrap_admin_id is not None and self.bootstrap_admin_id <= 0:
            raise ValueError("bootstrap admin ID must be positive")
        if self.bootstrap_admin_id is not None and not self.bootstrap_admin_display_name.strip():
            raise ValueError("bootstrap admin display name must not be empty")

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        token = os.getenv("TOKEN", "")
        database_path = Path(os.getenv("QUEST_DB_PATH", "quest.db"))
        raw_admin_id = os.getenv("QUEST_ADMIN_ID")
        admin_id = int(raw_admin_id) if raw_admin_id else None
        return cls(
            token=token,
            database_path=database_path,
            bootstrap_admin_id=admin_id,
            bootstrap_admin_display_name=os.getenv("QUEST_ADMIN_DISPLAY_NAME", "@admin"),
        )
