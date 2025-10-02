"""Translation utilities for internationalized error messages."""

from __future__ import annotations

from typing import Any, Dict, Optional

import json


class TranslationService:
    """Simple translation service for bot messages."""

    def __init__(self, translations_path: str = "config/data/translations.json"):
        self._translations: Dict[str, Any] = {}
        self._load_translations(translations_path)

    def _load_translations(self, path: str) -> None:
        """Load translations from JSON file."""

        try:
            with open(path, "r", encoding="utf-8") as file_obj:
                self._translations = json.load(file_obj)
        except FileNotFoundError:
            self._translations = {}

    def get(self, key: str, default: str = "") -> str:
        """Get translated message by dot-notation key."""

        keys = key.split(".")
        value: Any = self._translations

        for part in keys:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return default

        if value is None:
            return default
        return str(value)


_translation_service: Optional[TranslationService] = None


def init_translations(translations_path: str = "config/data/translations.json") -> None:
    """Initialize global translation service."""

    global _translation_service
    _translation_service = TranslationService(translations_path)


def translate(key: str, default: str = "") -> str:
    """Get translated message (convenience function)."""

    if _translation_service is None:
        return default
    return _translation_service.get(key, default)

