from pathlib import Path

import pytest
from pydantic import ValidationError

from quest_bot.config import Settings

SETTING_ENVIRONMENT_VARIABLES = (
    "TOKEN",
    "QUEST_DB_PATH",
    "QUEST_ADMIN_ID",
    "QUEST_ADMIN_USERNAME",
    "QUEST_SWEEP_INTERVAL_SECONDS",
    "QUEST_DATABASE_BUSY_TIMEOUT_MS",
    "QUEST_DELIVERY_RATE_PER_SECOND",
)


@pytest.fixture(autouse=True)
def isolate_settings_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in SETTING_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_settings_load_aliases_and_prefixed_operational_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TOKEN", "test-token")
    monkeypatch.setenv("QUEST_DB_PATH", "custom.db")
    monkeypatch.setenv("QUEST_ADMIN_ID", "42")
    monkeypatch.setenv("QUEST_ADMIN_USERNAME", "@admin")
    monkeypatch.setenv("QUEST_SWEEP_INTERVAL_SECONDS", "9")
    monkeypatch.setenv("QUEST_DATABASE_BUSY_TIMEOUT_MS", "1234")
    monkeypatch.setenv("QUEST_DELIVERY_RATE_PER_SECOND", "7")

    settings = Settings()

    assert settings.token == "test-token"
    assert settings.database_path == Path("custom.db")
    assert settings.bootstrap_admin_id == 42
    assert settings.bootstrap_admin_username == "@admin"
    assert settings.sweep_interval_seconds == 9
    assert settings.database_busy_timeout_ms == 1234
    assert settings.delivery_rate_per_second == 7


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(ValidationError):
        Settings()
    with pytest.raises(ValidationError):
        Settings(token="test-token", sweep_interval_seconds=0)
    with pytest.raises(ValidationError):
        Settings(
            token="test-token",
            bootstrap_admin_id=42,
            bootstrap_admin_username="@",
        )


def test_settings_are_frozen() -> None:
    settings = Settings(token="test-token")

    with pytest.raises(ValidationError):
        settings.__setattr__("sweep_interval_seconds", 1)


def test_settings_load_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "TOKEN=dotenv-token\nQUEST_DB_PATH=dotenv.db\n",
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.token == "dotenv-token"
    assert settings.database_path == Path("dotenv.db")
