from __future__ import annotations

import pytest

from quest_bot.normalization import normalize_answer


@pytest.mark.parametrize(
    ("submitted", "configured"),
    [
        ("  Phileas   FOGG ", "phileas fogg"),
        ("１２３", "123"),
        ("ПАРИЖ\n\tЛОНДОН", "париж лондон"),
    ],
)
def test_answer_normalization_allows_defined_lenience(
    submitted: str,
    configured: str,
) -> None:
    assert normalize_answer(submitted) == normalize_answer(configured)


@pytest.mark.parametrize(
    ("submitted", "configured"),
    [
        ("Kyiv", "Київ"),
        ("12, 34", "12 34"),
        ("80 20", "20 80"),
    ],
)
def test_answer_normalization_keeps_meaningful_differences(
    submitted: str,
    configured: str,
) -> None:
    assert normalize_answer(submitted) != normalize_answer(configured)
