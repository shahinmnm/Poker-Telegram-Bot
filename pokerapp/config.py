import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


logger = logging.getLogger(__name__)


_BASE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_DIR = _BASE_DIR / "config"
_DEFAULT_GAME_CONSTANTS_PATH = _DEFAULT_CONFIG_DIR / "game_constants.yaml"
_DEFAULT_SYSTEM_CONSTANTS_PATH = _DEFAULT_CONFIG_DIR / "system_constants.json"
_DEFAULT_TRANSLATIONS_PATH = _DEFAULT_CONFIG_DIR / "data" / "translations.json"
_DEFAULT_REDIS_KEYS_PATH = _DEFAULT_CONFIG_DIR / "data" / "redis_keys.json"
_DEFAULT_EMOJIS_PATH = _DEFAULT_CONFIG_DIR / "data" / "emojis.json"
_DEFAULT_ROLES_PATH = _DEFAULT_CONFIG_DIR / "data" / "roles.json"
_DEFAULT_HANDS_PATH = _DEFAULT_CONFIG_DIR / "data" / "hands.json"

_DEFAULT_GAME_CONSTANTS_DATA: Dict[str, Any] = {
    "game": {
        "dice_mult": 10,
        "dice_delay_sec": 5,
        "bonuses": [5, 20, 40, 80, 160, 320],
        "min_players": 2,
        "max_players": 8,
        "small_blind": 5,
        "default_money": 1000,
        "max_time_for_turn_seconds": 120,
        "auto_start": {
            "max_updates_per_minute": 20,
            "min_update_interval_seconds": 3,
        },
    },
    "ui": {
        "description_file": "assets/description_bot.md",
        "stages_persian": ["پری فلاپ", "فلاپ", "ترن", "ریور"],
        "stage_map": {
            "ROUND_PRE_FLOP": "پری فلاپ",
            "PRE_FLOP": "پری فلاپ",
            "PRE-FLOP": "پری فلاپ",
            "ROUND_FLOP": "فلاپ",
            "FLOP": "فلاپ",
            "ROUND_TURN": "ترن",
            "TURN": "ترن",
            "ROUND_RIVER": "ریور",
            "RIVER": "ریور",
        },
    },
    "redis": {
        "private_match_queue_key": "pokerbot:private_matchmaking:queue",
        "private_match_user_key_prefix": "pokerbot:private_matchmaking:user:",
        "private_match_record_key_prefix": "pokerbot:private_matchmaking:match:",
        "private_match_queue_ttl": 180,
        "private_match_state_ttl": 3600,
        "player_report_cache_ttl_seconds": 300,
        "player_report_ttl_default_seconds": 180,
        "player_report_ttl_bonus_seconds": 60,
        "player_report_ttl_post_hand_seconds": 45,
    },
    "engine": {
        "key_old_players": "old_players",
        "key_chat_data_game": "game",
        "key_stop_request": "stop_request",
        "key_start_countdown_last_text": "start_countdown_last_text",
        "key_start_countdown_last_timestamp": "start_countdown_last_timestamp",
        "key_start_countdown_context": "start_countdown_context",
        "stop_confirm_callback": "stop:confirm",
        "stop_resume_callback": "stop:resume",
    },
    "locks": {
        "category_timeouts_seconds": {
            "engine_stage": 25.0,
            "chat": 8.0,
            "player_report": 5.0,
            "wallet": 5.0,
        }
    },
}

_DEFAULT_SYSTEM_CONSTANTS_DATA: Dict[str, Any] = {
    "default_webhook_listen": "127.0.0.1",
    "default_webhook_port": 3000,
    "default_webhook_path": "/telegram/webhook-poker2025",
    "default_rate_limit_per_second": 1,
    "default_rate_limit_per_minute": 20,
    "default_timezone_name": "Asia/Tehran",
}

_DEFAULT_TRANSLATIONS_DATA: Dict[str, Any] = {"default_language": "fa"}

_DEFAULT_REDIS_KEYS_DATA: Dict[str, Any] = {
    "engine": {
        "stage_lock_prefix": "stage:",
        "stop_request": "stop_request",
    },
    "player_report": {
        "cache_prefix": "pokerbot:player_report:",
    },
}

_DEFAULT_EMOJIS_DATA: Dict[str, Any] = {
    "dice": {
        "faces": ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"],
        "sequence": "⚀⚁⚂⚃⚄⚅",
        "roll": "🎲",
    },
    "suits": {
        "♠": "♠\ufe0f",
        "♥": "♥\ufe0f",
        "♦": "♦\ufe0f",
        "♣": "♣\ufe0f",
    },
    "chips": {
        "pot": "💰",
        "stack": "💵",
        "bet": "🎲",
        "profit": "💰",
        "winnings": "💵",
        "average_pot": "🏺",
    },
}

_DEFAULT_ROLES_DATA: Dict[str, Any] = {
    "default_language": "fa",
    "roles": {
        "dealer": {"fa": "دیلر", "en": "Dealer"},
        "small_blind": {"fa": "بلایند کوچک", "en": "Small blind"},
        "big_blind": {"fa": "بلایند بزرگ", "en": "Big blind"},
        "player": {"fa": "بازیکن", "en": "Player"},
    },
}

_DEFAULT_HANDS_DATA: Dict[str, Any] = {
    "default_language": "fa",
    "hands": {
        "ROYAL_FLUSH": {"fa": "رویال فلاش", "en": "Royal Flush", "emoji": "👑"},
        "STRAIGHT_FLUSH": {"fa": "استریت فلاش", "en": "Straight Flush", "emoji": "💎"},
        "FOUR_OF_A_KIND": {"fa": "کاره (چهار تایی)", "en": "Four of a Kind", "emoji": "💣"},
        "FULL_HOUSE": {"fa": "فول هاوس", "en": "Full House", "emoji": "🏠"},
        "FLUSH": {"fa": "فلاش (رنگ)", "en": "Flush", "emoji": "🎨"},
        "STRAIGHT": {"fa": "استریت (ردیف)", "en": "Straight", "emoji": "🚀"},
        "THREE_OF_A_KIND": {"fa": "سه تایی", "en": "Three of a Kind", "emoji": "🧩"},
        "TWO_PAIR": {"fa": "دو پِر", "en": "Two Pair", "emoji": "✌️"},
        "PAIR": {"fa": "پِر (جفت)", "en": "Pair", "emoji": "🔗"},
        "HIGH_CARD": {"fa": "کارت بالا", "en": "High Card", "emoji": "🃏"},
    },
}


def _resolve_config_path(candidate: Optional[str], default: Path) -> Path:
    if not candidate:
        return default
    path = Path(candidate)
    if not path.is_absolute():
        path = _BASE_DIR / path
    return path


def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in overrides.items():
        if (
            isinstance(value, dict)
            and isinstance(base.get(key), dict)
        ):
            base[key] = _deep_merge(dict(base[key]), value)
        else:
            base[key] = value
    return base


class GameConstants:
    def __init__(
        self,
        path: Optional[str] = None,
        *,
        defaults: Optional[Dict[str, Any]] = None,
        translations_path: Optional[str] = None,
        redis_keys_path: Optional[str] = None,
        emojis_path: Optional[str] = None,
        roles_path: Optional[str] = None,
        hands_path: Optional[str] = None,
        translation_defaults: Optional[Dict[str, Any]] = None,
        redis_key_defaults: Optional[Dict[str, Any]] = None,
        emoji_defaults: Optional[Dict[str, Any]] = None,
        role_defaults: Optional[Dict[str, Any]] = None,
        hand_defaults: Optional[Dict[str, Any]] = None,
    ) -> None:
        resolved_path = _resolve_config_path(
            path or os.getenv("POKERBOT_GAME_CONSTANTS_FILE"),
            _DEFAULT_GAME_CONSTANTS_PATH,
        )
        self._path: Path = resolved_path
        self._defaults: Dict[str, Any] = deepcopy(defaults or _DEFAULT_GAME_CONSTANTS_DATA)
        self._translations_path: Path = _resolve_config_path(
            translations_path or os.getenv("POKERBOT_TRANSLATIONS_FILE"),
            _DEFAULT_TRANSLATIONS_PATH,
        )
        self._redis_keys_path: Path = _resolve_config_path(
            redis_keys_path or os.getenv("POKERBOT_REDIS_KEYS_FILE"),
            _DEFAULT_REDIS_KEYS_PATH,
        )
        self._emojis_path: Path = _resolve_config_path(
            emojis_path or os.getenv("POKERBOT_EMOJIS_FILE"),
            _DEFAULT_EMOJIS_PATH,
        )
        self._roles_path: Path = _resolve_config_path(
            roles_path or os.getenv("POKERBOT_ROLES_FILE"),
            _DEFAULT_ROLES_PATH,
        )
        self._hands_path: Path = _resolve_config_path(
            hands_path or os.getenv("POKERBOT_HANDS_FILE"),
            _DEFAULT_HANDS_PATH,
        )
        self._translation_defaults: Dict[str, Any] = deepcopy(
            translation_defaults or _DEFAULT_TRANSLATIONS_DATA
        )
        self._redis_key_defaults: Dict[str, Any] = deepcopy(
            redis_key_defaults or _DEFAULT_REDIS_KEYS_DATA
        )
        self._emoji_defaults: Dict[str, Any] = deepcopy(
            emoji_defaults or _DEFAULT_EMOJIS_DATA
        )
        self._role_defaults: Dict[str, Any] = deepcopy(
            role_defaults or _DEFAULT_ROLES_DATA
        )
        self._hand_defaults: Dict[str, Any] = deepcopy(
            hand_defaults or _DEFAULT_HANDS_DATA
        )
        self._data: Dict[str, Any] = {}
        self._translations: Dict[str, Any] = {}
        self._redis_keys: Dict[str, Any] = {}
        self._emojis: Dict[str, Any] = {}
        self._roles: Dict[str, Any] = {}
        self._hands: Dict[str, Any] = {}
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    def reload(self) -> None:
        raw_data: Dict[str, Any] = {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
                if not isinstance(loaded, dict):
                    logger.warning(
                        "Game constants file did not contain a mapping; using defaults.",
                        extra={
                            "category": "config",
                            "config_path": str(self._path),
                            "stage": "game_constants_load",
                            "error_type": "InvalidMapping",
                        },
                    )
                else:
                    raw_data = loaded
        except FileNotFoundError:
            logger.warning(
                "Game constants file not found; using default values.",
                extra={
                    "category": "config",
                    "config_path": str(self._path),
                    "stage": "game_constants_load",
                    "error_type": "FileNotFoundError",
                },
            )
        except yaml.YAMLError as exc:
            logger.warning(
                "Failed to parse game constants file; using defaults.",
                extra={
                    "category": "config",
                    "config_path": str(self._path),
                    "stage": "game_constants_load",
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )

        merged = deepcopy(self._defaults)
        if raw_data:
            merged = _deep_merge(merged, raw_data)
        self._data = merged
        self._translations = self._load_json_resource(
            path=self._translations_path,
            defaults=self._translation_defaults,
            stage="translations_load",
        )
        self._redis_keys = self._load_json_resource(
            path=self._redis_keys_path,
            defaults=self._redis_key_defaults,
            stage="redis_keys_load",
        )
        self._emojis = self._load_json_resource(
            path=self._emojis_path,
            defaults=self._emoji_defaults,
            stage="emojis_load",
        )
        self._roles = self._load_json_resource(
            path=self._roles_path,
            defaults=self._role_defaults,
            stage="roles_load",
        )
        self._hands = self._load_json_resource(
            path=self._hands_path,
            defaults=self._hand_defaults,
            stage="hands_load",
        )

    def _load_json_resource(
        self,
        *,
        path: Path,
        defaults: Dict[str, Any],
        stage: str,
    ) -> Dict[str, Any]:
        loaded: Dict[str, Any] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            if isinstance(parsed, dict):
                loaded = parsed
            else:
                logger.warning(
                    "JSON resource did not contain an object; using defaults.",
                    extra={
                        "category": "config",
                        "config_path": str(path),
                        "stage": stage,
                        "error_type": "InvalidMapping",
                    },
                )
        except FileNotFoundError:
            logger.warning(
                "JSON resource file not found; using default values.",
                extra={
                    "category": "config",
                    "config_path": str(path),
                    "stage": stage,
                    "error_type": "FileNotFoundError",
                },
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Failed to parse JSON resource; using defaults.",
                extra={
                    "category": "config",
                    "config_path": str(path),
                    "stage": stage,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )

        merged = deepcopy(defaults)
        if loaded:
            merged = _deep_merge(merged, loaded)
        return merged

    def get(self, key: str, default: Any = None) -> Any:
        value = self._data.get(key, default)
        return deepcopy(value)

    def section(self, key: str) -> Dict[str, Any]:
        section = self._data.get(key, {})
        if isinstance(section, dict):
            return deepcopy(section)
        return {}

    @property
    def game(self) -> Dict[str, Any]:
        return self.section("game")

    @property
    def ui(self) -> Dict[str, Any]:
        return self.section("ui")

    @property
    def redis(self) -> Dict[str, Any]:
        return self.section("redis")

    @property
    def engine(self) -> Dict[str, Any]:
        return self.section("engine")

    @property
    def translations(self) -> Dict[str, Any]:
        return deepcopy(self._translations)

    @property
    def redis_keys(self) -> Dict[str, Any]:
        return deepcopy(self._redis_keys)

    @property
    def emojis(self) -> Dict[str, Any]:
        return deepcopy(self._emojis)

    @property
    def roles(self) -> Dict[str, Any]:
        return deepcopy(self._roles)

    @property
    def hands(self) -> Dict[str, Any]:
        return deepcopy(self._hands)


def _load_system_constants() -> Dict[str, Any]:
    resolved_path = _resolve_config_path(
        os.getenv("POKERBOT_SYSTEM_CONSTANTS_FILE"),
        _DEFAULT_SYSTEM_CONSTANTS_PATH,
    )
    loaded: Dict[str, Any] = {}
    try:
        with resolved_path.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
            if isinstance(parsed, dict):
                loaded = parsed
            else:
                logger.warning(
                    "System constants file did not contain a JSON object; ignoring it.",
                    extra={
                        "category": "config",
                        "config_path": str(resolved_path),
                        "stage": "system_constants_load",
                        "error_type": "InvalidMapping",
                    },
                )
    except FileNotFoundError:
        logger.warning(
            "System constants file not found; using built-in defaults.",
            extra={
                "category": "config",
                "config_path": str(resolved_path),
                "stage": "system_constants_load",
                "error_type": "FileNotFoundError",
            },
        )
    except json.JSONDecodeError as exc:
        logger.warning(
            "Failed to parse system constants file; using defaults.",
            extra={
                "category": "config",
                "config_path": str(resolved_path),
                "stage": "system_constants_load",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
    merged = deepcopy(_DEFAULT_SYSTEM_CONSTANTS_DATA)
    for key, value in loaded.items():
        if key in merged:
            merged[key] = value
    return merged


_SYSTEM_CONSTANTS = _load_system_constants()

DEFAULT_WEBHOOK_LISTEN = _SYSTEM_CONSTANTS["default_webhook_listen"]
DEFAULT_WEBHOOK_PORT = _SYSTEM_CONSTANTS["default_webhook_port"]
DEFAULT_WEBHOOK_PATH = _SYSTEM_CONSTANTS["default_webhook_path"]
# Telegram Bot API documentation recommends avoiding more than one message per
# second in a chat and limits groups to 20 messages per minute.
DEFAULT_RATE_LIMIT_PER_SECOND = _SYSTEM_CONSTANTS["default_rate_limit_per_second"]
DEFAULT_RATE_LIMIT_PER_MINUTE = _SYSTEM_CONSTANTS["default_rate_limit_per_minute"]
DEFAULT_TIMEZONE_NAME = _SYSTEM_CONSTANTS["default_timezone_name"]


GAME_CONSTANTS = GameConstants()


def get_game_constants() -> GameConstants:
    return GAME_CONSTANTS


class Config:
    def __init__(self):
        self.constants: GameConstants = GAME_CONSTANTS
        locks_section = self.constants.section("locks")
        raw_lock_timeouts = locks_section.get("category_timeouts_seconds")
        if not isinstance(raw_lock_timeouts, Mapping):
            raw_lock_timeouts = None
        self.LOCK_TIMEOUTS: Dict[str, float] = self._normalise_lock_timeouts(
            raw_lock_timeouts
        )
        self.REDIS_HOST: str = os.getenv(
            "POKERBOT_REDIS_HOST",
            default="localhost",
        )
        self.REDIS_PORT: str = int(os.getenv(
            "POKERBOT_REDIS_PORT",
            default="6379"
        ))
        self.REDIS_PASS: str = os.getenv(
            "POKERBOT_REDIS_PASS",
            default="",
        )
        self.REDIS_DB: int = int(os.getenv(
            "POKERBOT_REDIS_DB",
            default="0"
        ))
        redis_constants = self.constants.redis
        player_report_cache_ttl_env = os.getenv(
            "POKERBOT_PLAYER_REPORT_CACHE_TTL"
        )
        parsed_player_report_cache_ttl = self._parse_positive_int(
            player_report_cache_ttl_env,
            env_var="POKERBOT_PLAYER_REPORT_CACHE_TTL",
        )
        default_player_report_cache_ttl = int(
            redis_constants.get("player_report_cache_ttl_seconds", 300)
        )
        self.PLAYER_REPORT_CACHE_TTL: int = (
            parsed_player_report_cache_ttl
            if parsed_player_report_cache_ttl is not None
            else default_player_report_cache_ttl
        )
        self.PLAYER_REPORT_TTL_DEFAULT = max(
            self._parse_int_env(
                os.getenv("POKERBOT_PLAYER_REPORT_TTL_DEFAULT"),
                default=int(
                    redis_constants.get("player_report_ttl_default_seconds", 180)
                ),
                env_var="POKERBOT_PLAYER_REPORT_TTL_DEFAULT",
            ),
            0,
        )
        self.PLAYER_REPORT_TTL_BONUS = max(
            self._parse_int_env(
                os.getenv("POKERBOT_PLAYER_REPORT_TTL_BONUS"),
                default=int(
                    redis_constants.get("player_report_ttl_bonus_seconds", 60)
                ),
                env_var="POKERBOT_PLAYER_REPORT_TTL_BONUS",
            ),
            0,
        )
        self.PLAYER_REPORT_TTL_POST_HAND = max(
            self._parse_int_env(
                os.getenv("POKERBOT_PLAYER_REPORT_TTL_POST_HAND"),
                default=int(
                    redis_constants.get("player_report_ttl_post_hand_seconds", 45)
                ),
                env_var="POKERBOT_PLAYER_REPORT_TTL_POST_HAND",
            ),
            0,
        )
        database_url_env = os.getenv("POKERBOT_DATABASE_URL", "").strip()
        if database_url_env:
            self.DATABASE_URL = database_url_env
        else:
            sqlite_path_env = os.getenv("POKERBOT_SQLITE_PATH", "").strip()
            data_dir_env = os.getenv("POKERBOT_DATA_DIR", "").strip()
            if sqlite_path_env:
                sqlite_path = Path(sqlite_path_env).expanduser()
            elif data_dir_env:
                sqlite_path = Path(data_dir_env).expanduser() / "pokerbot_stats.sqlite3"
            else:
                sqlite_path = Path.cwd() / "pokerbot_stats.sqlite3"
            try:
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "Unable to create directory for SQLite database; using fallback path.",
                    extra={
                        "category": "config",
                        "config_path": str(sqlite_path.parent),
                        "stage": "database_path_resolve",
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
            resolved_sqlite_path = sqlite_path.resolve()
            self.DATABASE_URL = f"sqlite+aiosqlite:///{resolved_sqlite_path.as_posix()}"
        database_echo_raw = os.getenv("POKERBOT_DATABASE_ECHO", "0").strip().lower()
        self.DATABASE_ECHO: bool = database_echo_raw in {"1", "true", "yes", "on"}
        self.TOKEN: str = os.getenv(
            "POKERBOT_TOKEN",
            default="",
        )
        self.DEBUG: bool = bool(
            os.getenv("POKERBOT_DEBUG", default="0") == "1"
        )
        allow_polling_raw = os.getenv("POKERBOT_ALLOW_POLLING_FALLBACK")
        self.ALLOW_POLLING_FALLBACK: bool = (
            allow_polling_raw is not None
            and allow_polling_raw.strip().lower() in {"1", "true", "yes", "on"}
        )
        admin_chat_id = os.getenv("POKERBOT_ADMIN_CHAT_ID", "")
        self.ADMIN_CHAT_ID = int(admin_chat_id) if admin_chat_id else None
        self.WEBHOOK_LISTEN: str = (
            os.getenv("POKERBOT_WEBHOOK_LISTEN", DEFAULT_WEBHOOK_LISTEN).strip()
            or DEFAULT_WEBHOOK_LISTEN
        )
        self.WEBHOOK_PORT: int = self._parse_int_env(
            os.getenv("POKERBOT_WEBHOOK_PORT"),
            default=DEFAULT_WEBHOOK_PORT,
            env_var="POKERBOT_WEBHOOK_PORT",
        )
        webhook_path_env = os.getenv("POKERBOT_WEBHOOK_PATH")
        raw_webhook_path = (
            webhook_path_env.strip()
            if webhook_path_env is not None
            else DEFAULT_WEBHOOK_PATH
        )
        self.WEBHOOK_PATH: str = self._normalize_webhook_path(raw_webhook_path)
        raw_webhook_domain = os.getenv("POKERBOT_WEBHOOK_DOMAIN", "")
        self.WEBHOOK_DOMAIN: str = self._normalize_webhook_domain(raw_webhook_domain)
        explicit_public_url = os.getenv(
            "POKERBOT_WEBHOOK_PUBLIC_URL",
            default="",
        )
        self.WEBHOOK_PUBLIC_URL: str = self._build_public_url(
            explicit_public_url=explicit_public_url,
        )
        if not self.WEBHOOK_PUBLIC_URL:
            logger.warning(
                "Webhook public URL is not configured; falling back to webhook path/domain defaults.",
                extra={
                    "category": "config",
                    "stage": "webhook_configuration",
                    "error_type": "MissingWebhookPublicUrl",
                    "webhook_domain": self.WEBHOOK_DOMAIN,
                    "webhook_path": self.WEBHOOK_PATH,
                },
            )
        self.WEBHOOK_SECRET: str = os.getenv(
            "POKERBOT_WEBHOOK_SECRET",
            default="",
        )
        allowed_updates_raw, _allowed_updates_source = self._get_first_nonempty_env(
            "POKERBOT_WEBHOOK_ALLOWED_UPDATES",
            "POKERBOT_ALLOWED_UPDATES",
        )
        self.ALLOWED_UPDATES: Optional[List[str]] = self._parse_allowed_updates(
            allowed_updates_raw
        )
        allow_empty_dealer_raw = os.getenv("POKERBOT_ALLOW_EMPTY_DEALER")
        allow_empty_dealer = False
        if allow_empty_dealer_raw is not None:
            allow_empty_dealer = (
                allow_empty_dealer_raw.strip().lower() in {"1", "true", "yes", "on"}
            )
        self.ALLOW_EMPTY_DEALER: bool = self.DEBUG or allow_empty_dealer
        max_connections_raw, max_connections_source = self._get_first_nonempty_env(
            "POKERBOT_WEBHOOK_MAX_CONNECTIONS",
            "POKERBOT_MAX_CONNECTIONS",
        )
        self.MAX_CONNECTIONS: Optional[int] = self._parse_positive_int(
            max_connections_raw,
            env_var=max_connections_source,
        )
        rate_limit_per_minute_raw = os.getenv("POKERBOT_RATE_LIMIT_PER_MINUTE")
        parsed_rate_limit_per_minute = self._parse_positive_int(
            rate_limit_per_minute_raw,
            env_var="POKERBOT_RATE_LIMIT_PER_MINUTE",
        )
        if parsed_rate_limit_per_minute is None:
            self.RATE_LIMIT_PER_MINUTE: int = DEFAULT_RATE_LIMIT_PER_MINUTE
        else:
            self.RATE_LIMIT_PER_MINUTE = parsed_rate_limit_per_minute

        rate_limit_per_second_raw = os.getenv("POKERBOT_RATE_LIMIT_PER_SECOND")
        parsed_rate_limit_per_second = self._parse_positive_int(
            rate_limit_per_second_raw,
            env_var="POKERBOT_RATE_LIMIT_PER_SECOND",
        )
        if parsed_rate_limit_per_second is None:
            self.RATE_LIMIT_PER_SECOND: int = DEFAULT_RATE_LIMIT_PER_SECOND
        else:
            self.RATE_LIMIT_PER_SECOND = parsed_rate_limit_per_second

        telegram_max_retries_raw = os.getenv("POKERBOT_TELEGRAM_MAX_RETRIES")
        parsed_telegram_max_retries = self._parse_positive_int(
            telegram_max_retries_raw,
            env_var="POKERBOT_TELEGRAM_MAX_RETRIES",
        )
        self.TELEGRAM_MAX_RETRIES: int = (
            parsed_telegram_max_retries if parsed_telegram_max_retries is not None else 3
        )

        base_delay_raw = os.getenv("POKERBOT_TELEGRAM_RETRY_BASE_DELAY")
        parsed_base_delay = self._parse_positive_float(
            base_delay_raw,
            env_var="POKERBOT_TELEGRAM_RETRY_BASE_DELAY",
        )
        self.TELEGRAM_RETRY_BASE_DELAY: float = (
            parsed_base_delay if parsed_base_delay is not None else 1.0
        )

        max_delay_raw = os.getenv("POKERBOT_TELEGRAM_RETRY_MAX_DELAY")
        parsed_max_delay = self._parse_positive_float(
            max_delay_raw,
            env_var="POKERBOT_TELEGRAM_RETRY_MAX_DELAY",
        )
        self.TELEGRAM_RETRY_MAX_DELAY: float = (
            parsed_max_delay if parsed_max_delay is not None else 30.0
        )

        multiplier_raw = os.getenv("POKERBOT_TELEGRAM_RETRY_MULTIPLIER")
        parsed_multiplier = self._parse_positive_float(
            multiplier_raw,
            env_var="POKERBOT_TELEGRAM_RETRY_MULTIPLIER",
        )
        self.TELEGRAM_RETRY_MULTIPLIER: float = (
            parsed_multiplier if parsed_multiplier is not None else 2.0
        )

        timezone_env = os.getenv("POKERBOT_TIMEZONE", "").strip()
        timezone_candidate = timezone_env or DEFAULT_TIMEZONE_NAME
        try:
            ZoneInfo(timezone_candidate)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Configured timezone %s is invalid; falling back to default",
                timezone_candidate,
                extra={
                    "category": "config",
                    "stage": "timezone_configuration",
                    "error_type": "InvalidTimezone",
                },
            )
            timezone_candidate = DEFAULT_TIMEZONE_NAME
        self.TIMEZONE_NAME: str = timezone_candidate

    @staticmethod
    def _normalize_webhook_path(path: str) -> str:
        normalized_path = path.strip()
        if not normalized_path:
            return ""
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        return normalized_path

    @staticmethod
    def _normalize_webhook_domain(domain: str) -> str:
        normalized_domain = domain.strip()
        if not normalized_domain:
            return ""
        if not normalized_domain.startswith(("http://", "https://")):
            logger.debug(
                "POKERBOT_WEBHOOK_DOMAIN missing scheme; defaulting to https://%s",
                normalized_domain,
            )
            normalized_domain = f"https://{normalized_domain}"
        return normalized_domain.rstrip("/")

    def _build_public_url(self, explicit_public_url: str) -> str:
        explicit_public_url = explicit_public_url.strip()
        if explicit_public_url:
            logger.debug(
                "Using explicit WEBHOOK_PUBLIC_URL provided via POKERBOT_WEBHOOK_PUBLIC_URL."
            )
            return explicit_public_url

        if self.WEBHOOK_DOMAIN and self.WEBHOOK_PATH:
            combined_url = urljoin(
                f"{self.WEBHOOK_DOMAIN.rstrip('/')}/",
                self.WEBHOOK_PATH.lstrip("/"),
            )
            logger.debug(
                "Derived WEBHOOK_PUBLIC_URL from domain and path using %s and %s",
                self.WEBHOOK_DOMAIN,
                self.WEBHOOK_PATH,
            )
            return combined_url

        return ""

    @staticmethod
    def _normalise_lock_timeouts(
        raw_mapping: Optional[Mapping[str, Any]]
    ) -> Dict[str, float]:
        if not raw_mapping:
            return {}
        resolved: Dict[str, float] = {}
        for raw_key, raw_value in raw_mapping.items():
            if raw_key is None:
                continue
            key = str(raw_key)
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                continue
            if numeric <= 0:
                continue
            resolved[key] = float(numeric)
        return resolved

    @staticmethod
    def _get_first_nonempty_env(*keys: str) -> Tuple[Optional[str], Optional[str]]:
        for key in keys:
            value = os.getenv(key)
            if value is None:
                continue
            stripped = value.strip()
            if stripped:
                return stripped, key
        return None, None

    @staticmethod
    def _parse_allowed_updates(raw_value: Optional[str]) -> Optional[List[str]]:
        if not raw_value:
            return None
        updates = [
            update.strip()
            for update in raw_value.split(",")
            if update.strip()
        ]
        return updates or None

    @staticmethod
    def _parse_positive_int(
        raw_value: Optional[str], *, env_var: Optional[str]
    ) -> Optional[int]:
        if not raw_value:
            return None
        try:
            value = int(raw_value)
        except ValueError:
            if env_var:
                logger.warning(
                    "Invalid integer value '%s' for %s; ignoring it.",
                    raw_value,
                    env_var,
                )
            else:
                logger.warning(
                    "Invalid integer value '%s' provided for MAX_CONNECTIONS; ignoring it.",
                    raw_value,
                )
            return None
        if value <= 0:
            if env_var:
                logger.warning(
                    "%s must be greater than zero; ignoring %s.",
                    env_var,
                    raw_value,
                )
            else:
                logger.warning(
                    "MAX_CONNECTIONS must be greater than zero; ignoring %s.",
                    raw_value,
                )
            return None
        return value

    @staticmethod
    def _parse_int_env(
        raw_value: Optional[str], *, default: int, env_var: str
    ) -> int:
        if raw_value is None:
            return default
        raw_value = raw_value.strip()
        if not raw_value:
            return default
        try:
            return int(raw_value)
        except ValueError:
            logger.warning(
                "Invalid integer value '%s' for %s; falling back to default %s.",
                raw_value,
                env_var,
                default,
            )
            return default

    @staticmethod
    def _parse_positive_float(
        raw_value: Optional[str], *, env_var: Optional[str]
    ) -> Optional[float]:
        if not raw_value:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            if env_var:
                logger.warning(
                    "Invalid float value '%s' for %s; ignoring it.",
                    raw_value,
                    env_var,
                )
            else:
                logger.warning(
                    "Invalid float value '%s'; ignoring it.",
                    raw_value,
                )
            return None
        if value <= 0:
            if env_var:
                logger.warning(
                    "%s must be greater than zero; ignoring %s.",
                    env_var,
                    raw_value,
                )
            else:
                logger.warning(
                    "Float configuration value must be greater than zero; ignoring %s.",
                    raw_value,
                )
            return None
        return value
