"""Utilities for normalizing Telegram payloads before sending requests.

This module centralises the validation that every outgoing payload must pass
before it can be delivered to Telegram.  The helpers sanitise Markdown/HTML
content, enforce Telegram length limits, make sure strings are UTF-8
encodable and optionally verify that remote media URLs respond to a HEAD
request.  The functions never mutate the input values, instead returning
sanitised copies that can safely be passed to ``bot.send_*`` helpers.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram.constants import MessageLimit, ParseMode
from telegram.helpers import escape, escape_markdown


logger = logging.getLogger(__name__)


class TelegramPayloadValidator:
    """Validate and sanitise Telegram bot payloads."""

    def __init__(
        self,
        *,
        enable_url_head_check: bool = True,
        head_request_timeout: float = 5.0,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._enable_url_head_check = enable_url_head_check
        self._head_request_timeout = head_request_timeout
        self._logger = logger_ if logger_ is not None else logger

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def normalize_text(
        self,
        text: Optional[str],
        *,
        parse_mode: Optional[str],
        limit: int = MessageLimit.MAX_TEXT_LENGTH.value,
        context: Optional[Dict[str, Any]] = None,
        field: str = "text",
    ) -> Optional[str]:
        """Return a UTF-8 safe and trimmed copy of ``text``.

        ``None`` is returned when the value should not be sent (e.g. the
        resulting payload is empty after sanitisation).  All adjustments are
        logged together with the provided ``context`` to simplify debugging.
        """

        if text is None:
            return None

        original = str(text)
        sanitized = self._ensure_utf8(original, context=context, field=field)
        sanitized = self._sanitize_markup(
            sanitized, parse_mode=parse_mode, context=context, field=field
        )

        if sanitized and len(sanitized) > limit:
            truncated = sanitized[:limit]
            self._logger.warning(
                "Truncated %s exceeding limit", field, extra=self._build_extra(context, {
                    "original_length": len(sanitized),
                    "limit": limit,
                }),
            )
            sanitized = truncated

        if not sanitized and original:
            self._logger.error(
                "Sanitised %s is empty, dropping payload", field, extra=self._build_extra(context)
            )
            return None

        return sanitized

    def normalize_caption(
        self,
        caption: Optional[str],
        *,
        parse_mode: Optional[str],
        context: Optional[Dict[str, Any]] = None,
        field: str = "caption",
    ) -> Optional[str]:
        """Normalise a caption to Telegram's constraints."""

        return self.normalize_text(
            caption,
            parse_mode=parse_mode,
            limit=MessageLimit.CAPTION_LENGTH.value,
            context=context,
            field=field,
        )

    async def validate_remote_media(
        self,
        media: Any,
        *,
        context: Optional[Dict[str, Any]] = None,
        field: str = "media",
    ) -> bool:
        """Return ``True`` if ``media`` can be sent to Telegram.

        For non-string objects (files, file-like objects) the value is accepted
        without additional validation.  For strings the helper checks that the
        string is a valid HTTP(S) URL and optionally performs a HEAD request.
        """

        if not isinstance(media, str):
            return True

        if not self._looks_like_url(media):
            self._logger.error(
                "Rejected %s due to invalid URL", field, extra=self._build_extra(context, {field: media})
            )
            return False

        if not self._enable_url_head_check:
            return True

        try:
            is_reachable = await self._head_check(media)
        except Exception as exc:  # pragma: no cover - unexpected exceptions
            self._logger.warning(
                "HEAD check failed for %s", field, extra=self._build_extra(context, {
                    field: media,
                    "error": str(exc),
                }),
            )
            return False

        if not is_reachable:
            self._logger.warning(
                "Remote %s is unreachable", field, extra=self._build_extra(context, {field: media})
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_utf8(
        self,
        value: str,
        *,
        context: Optional[Dict[str, Any]],
        field: str,
    ) -> str:
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            cleaned = value.encode("utf-8", errors="ignore").decode("utf-8")
            self._logger.warning(
                "Removed non UTF-8 characters from %s", field, extra=self._build_extra(context)
            )
            return cleaned

    def _sanitize_markup(
        self,
        value: str,
        *,
        parse_mode: Optional[str],
        context: Optional[Dict[str, Any]],
        field: str,
    ) -> str:
        if not value:
            return value

        if parse_mode == ParseMode.HTML:
            escaped = escape(value)
            if escaped != value:
                self._logger.debug(
                    "Escaped HTML for %s", field, extra=self._build_extra(context)
                )
            return escaped

        if parse_mode in (ParseMode.MARKDOWN, ParseMode.MARKDOWN_V2):
            version = 2 if parse_mode == ParseMode.MARKDOWN_V2 else 1
            if self._is_probably_valid_markdown(value, version=version):
                return value
            escaped = escape_markdown(value, version=version)
            if escaped != value:
                self._logger.warning(
                    "Escaped invalid Markdown for %s", field, extra=self._build_extra(context)
                )
            return escaped

        return value

    @staticmethod
    def _build_extra(
        context: Optional[Dict[str, Any]], additional: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        data: Dict[str, Any] = {"context": context or {}}
        if additional:
            data.update(additional)
        return data

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def _head_check(self, url: str) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(self._blocking_head_request, url=url),
        )

    def _blocking_head_request(self, *, url: str) -> bool:
        request = Request(url, method="HEAD")
        try:
            with urlopen(request, timeout=self._head_request_timeout) as response:
                status = getattr(response, "status", 200)
                return status < 400 or status == 405
        except HTTPError as error:
            return error.code < 400 or error.code == 405
        except (URLError, ValueError):
            return False

    @staticmethod
    def _is_probably_valid_markdown(text: str, *, version: int) -> bool:
        if version not in (1, 2):
            return True

        if not TelegramPayloadValidator._has_balanced_brackets(text):
            return False
        if not TelegramPayloadValidator._has_balanced_delimiter(text, "*"):
            return False
        if not TelegramPayloadValidator._has_balanced_delimiter(text, "_"):
            return False
        if not TelegramPayloadValidator._has_balanced_delimiter(text, "`"):
            return False
        return True

    @staticmethod
    def _has_balanced_brackets(text: str) -> bool:
        depth = 0
        i = 0
        length = len(text)
        while i < length:
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                if depth == 0:
                    return False
                depth -= 1
                if i + 1 < length and text[i + 1] == "(":
                    closing = text.find(")", i + 2)
                    if closing == -1:
                        return False
            i += 1
        return depth == 0

    @staticmethod
    def _has_balanced_delimiter(text: str, delimiter: str) -> bool:
        count = 0
        i = 0
        while i < len(text):
            char = text[i]
            if char == "\\":
                i += 2
                continue
            if char == delimiter:
                count ^= 1
            i += 1
        return count == 0

