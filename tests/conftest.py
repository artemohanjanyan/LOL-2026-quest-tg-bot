from __future__ import annotations

import pytest

from tests.fakes import FakeTelegramRequest


@pytest.fixture
def telegram_request() -> FakeTelegramRequest:
    return FakeTelegramRequest()
