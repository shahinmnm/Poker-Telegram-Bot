"""Helpers for working with Telegram Markdown v1."""

from __future__ import annotations

from typing import Optional

from telegram.helpers import escape_markdown as _escape_markdown


def escape_markdown_v1(text: Optional[str]) -> str:
    """Return ``text`` escaped for Telegram Markdown v1 messages."""
    if text is None:
        return ""
    return _escape_markdown(text, version=1)


__all__ = ["escape_markdown_v1"]
