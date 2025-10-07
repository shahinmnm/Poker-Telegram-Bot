"""Locale-specific helpers for presenting numbers."""

from __future__ import annotations

from typing import Union

NumberLike = Union[int, float, str]

PERSIAN_DIGIT_MAP = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_persian_digits(value: NumberLike) -> str:
    """Convert ASCII digits in *value* to Persian digits.

    Args:
        value: The value whose digits should be translated. Non-string values
            are converted to ``str`` before translation.

    Returns:
        The string representation of ``value`` with Persian numerals.
    """

    return str(value).translate(PERSIAN_DIGIT_MAP)


__all__ = ["PERSIAN_DIGIT_MAP", "to_persian_digits"]
