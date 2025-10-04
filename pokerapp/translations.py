"""Translation utilities for internationalized error messages."""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import json


class TranslationService:
    """Simple translation service for bot messages."""

    def __init__(self, translations_path: str = "config/data/translations.json"):
        self._translations: Dict[str, Any] = {}
        self._language_order: Sequence[str] = ("fa", "en")
        self._load_translations(translations_path)

    def _load_translations(self, path: str) -> None:
        """Load translations from JSON file."""

        try:
            with open(path, "r", encoding="utf-8") as file_obj:
                self._translations = json.load(file_obj)
        except FileNotFoundError:
            self._translations = {}
        default_language = "fa"
        candidate = self._translations.get("default_language")
        if isinstance(candidate, str) and candidate:
            default_language = candidate
        self._language_order = tuple(dict.fromkeys([default_language, "fa", "en"]))

    def _resolve_value(self, value: Any) -> Optional[str]:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for language in self._language_order:
                candidate = value.get(language)
                if isinstance(candidate, str) and candidate:
                    return candidate
        return None

    def get(self, key: str, default: str = "", **format_kwargs: Any) -> str:
        """Get translated message by dot-notation key."""

        keys = key.split(".")
        value: Any = self._translations

        for part in keys:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

        resolved = self._resolve_value(value)
        if resolved is None:
            resolved = default

        if format_kwargs:
            try:
                return resolved.format(**format_kwargs)
            except Exception:
                return resolved
        return resolved


_translation_service: Optional[TranslationService] = None


def init_translations(translations_path: str = "config/data/translations.json") -> None:
    """Initialize global translation service."""

    global _translation_service
    _translation_service = TranslationService(translations_path)


def translate(key: str, default: str = "", **format_kwargs: Any) -> str:
    """Get translated message (convenience function)."""

    if _translation_service is None:
        if format_kwargs:
            try:
                return default.format(**format_kwargs)
            except Exception:
                return default
        return default
    return _translation_service.get(key, default, **format_kwargs)

