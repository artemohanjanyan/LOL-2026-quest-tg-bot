"""Normalization rules used when comparing quest answers."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")


def normalize_answer(value: str) -> str:
    """Return the canonical comparison form of a configured or submitted answer.

    Comparison deliberately remains conservative: compatibility-equivalent
    Unicode characters and differences in case or whitespace compare equally,
    while punctuation, alphabet, transliteration, and token order are preserved.
    """

    normalized = unicodedata.normalize("NFKC", value)
    normalized = _WHITESPACE.sub(" ", normalized.strip())
    return normalized.casefold()
