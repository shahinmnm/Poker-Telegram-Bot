#!/usr/bin/env python3

from telegram import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Bot,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from io import BytesIO
from typing import (
    Optional,
    Dict,
    Any,
    Tuple,
    List,
    Sequence,
    Set,
    Callable,
    Awaitable,
)
from dataclasses import dataclass, field
import asyncio
import datetime
import hashlib
import inspect
import logging
import json
import threading
from cachetools import FIFOCache, LRUCache
from pokerapp.config import DEFAULT_RATE_LIMIT_PER_MINUTE, DEFAULT_RATE_LIMIT_PER_SECOND
from pokerapp.winnerdetermination import HAND_NAMES_TRANSLATIONS
from pokerapp.desk import DeskImageGenerator
from pokerapp.cards import Cards, Card
from pokerapp.entities import (
    Game,
    GameState,
    Player,
    PlayerAction,
    MessageId,
    ChatId,
    Mention,
    Money,
    PlayerState,
)
from pokerapp.telegram_validation import TelegramPayloadValidator
from pokerapp.utils.debug_trace import trace_telegram_api_call
from pokerapp.utils.messaging_service import MessagingService
from pokerapp.utils.request_metrics import RequestCategory, RequestMetrics


logger = logging.getLogger(__name__)
debug_trace_logger = logging.getLogger("pokerbot.debug_trace")

_CARD_SPACER = "     "


def build_player_cards_keyboard(
    hole_cards: Sequence[str],
    community_cards: Sequence[str],
    current_stage: str,
) -> ReplyKeyboardMarkup:
    """Builds a personalized ReplyKeyboardMarkup for a player."""

    # Row 1: The player's unique hole cards.
    row1 = list(hole_cards) or ["⬜️", "⬜️"]

    # Row 2: The shared community cards on the board.
    row2 = list(community_cards) or ["⬜️"]

    # Row 3: Game stages, with the current stage highlighted by a '✅' emoji.
    stages_persian = ["پری فلاپ", "فلاپ", "ترن", "ریور"]
    stage_map = {
        "ROUND_PRE_FLOP": "پری فلاپ",
        "PRE_FLOP": "پری فلاپ",
        "PRE-FLOP": "پری فلاپ",
        "ROUND_FLOP": "فلاپ",
        "FLOP": "فلاپ",
        "ROUND_TURN": "ترن",
        "TURN": "ترن",
        "ROUND_RIVER": "ریور",
        "RIVER": "ریور",
    }

    current_stage_label = stage_map.get(current_stage.upper(), "")

    row3 = [
        f"✅ {label}" if label == current_stage_label else label
        for label in stages_persian
    ]

    # Construct and return the final keyboard object.
    return ReplyKeyboardMarkup(
        keyboard=[row1, row2, row3],
        resize_keyboard=True,  # Makes the keyboard fit the content.
        one_time_keyboard=False,  # The keyboard persists until replaced.
        selective=False,  # Group keyboard must be visible to everyone in chat.
    )


@dataclass(slots=True)
class TurnMessageUpdate:
    message_id: Optional[MessageId]
    call_label: str
    call_action: PlayerAction
    board_line: str

@dataclass(slots=True)
class RoleAnchorRecord:
    player_id: Any
    seat_index: int
    message_id: int
    base_text: str
    current_text: str
    payload_signature: str
    markup_signature: str
    turn_light: str = ""
    refresh_toggle: str = ""


@dataclass(slots=True)
class TurnAnchorRecord:
    message_id: Optional[int] = None
    payload_hash: str = ""


@dataclass(slots=True)
class ChatAnchorState:
    role_anchors: Dict[Any, RoleAnchorRecord] = field(default_factory=dict)
    turn_anchor: TurnAnchorRecord = field(default_factory=TurnAnchorRecord)
    edit_count: int = 0
    fallback_count: int = 0
    role_retry_count: int = 0
    current_stage: Optional[GameState] = None


class AnchorRegistry:
    """Track role/turn anchors per chat to keep updates stable."""

    def __init__(self) -> None:
        self._registry: Dict[int, ChatAnchorState] = {}

    @staticmethod
    def _normalize_chat(chat_id: ChatId) -> int:
        try:
            return int(chat_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def reset_roles(self, chat_id: ChatId) -> ChatAnchorState:
        normalized = self._normalize_chat(chat_id)
        state = self._registry.setdefault(normalized, ChatAnchorState())
        state.role_anchors.clear()
        state.edit_count = 0
        state.fallback_count = 0
        state.role_retry_count = 0
        return state

    def set_stage(
        self, chat_id: ChatId, stage: Optional[GameState]
    ) -> ChatAnchorState:
        state = self.get_chat_state(chat_id)
        state.current_stage = stage
        return state

    def get_stage(self, chat_id: ChatId) -> Optional[GameState]:
        state = self.get_chat_state(chat_id)
        return state.current_stage

    def get_chat_state(self, chat_id: ChatId) -> ChatAnchorState:
        normalized = self._normalize_chat(chat_id)
        return self._registry.setdefault(normalized, ChatAnchorState())

    def register_role(
        self,
        chat_id: ChatId,
        *,
        player_id: Any,
        seat_index: int,
        message_id: int,
        base_text: str,
        payload_signature: str,
        markup_signature: str,
    ) -> RoleAnchorRecord:
        state = self.get_chat_state(chat_id)
        record = RoleAnchorRecord(
            player_id=player_id,
            seat_index=seat_index,
            message_id=message_id,
            base_text=base_text,
            current_text=base_text,
            payload_signature=payload_signature,
            markup_signature=markup_signature,
        )
        state.role_anchors[player_id] = record
        return record

    def update_role(
        self,
        chat_id: ChatId,
        player_id: Any,
        *,
        base_text: Optional[str] = None,
        message_id: Optional[int] = None,
        current_text: Optional[str] = None,
        payload_signature: Optional[str] = None,
        markup_signature: Optional[str] = None,
        turn_light: Optional[str] = None,
        refresh_toggle: Optional[str] = None,
    ) -> Optional[RoleAnchorRecord]:
        record = self.get_chat_state(chat_id).role_anchors.get(player_id)
        if record is None:
            return None
        if base_text is not None:
            record.base_text = base_text
        if message_id is not None:
            record.message_id = message_id
        if current_text is not None:
            record.current_text = current_text
        if payload_signature is not None:
            record.payload_signature = payload_signature
        if markup_signature is not None:
            record.markup_signature = markup_signature
        if turn_light is not None:
            record.turn_light = turn_light
        if refresh_toggle is not None:
            record.refresh_toggle = refresh_toggle
        return record

    def increment_role_retry(self, chat_id: ChatId) -> None:
        state = self.get_chat_state(chat_id)
        state.role_retry_count += 1

    def get_role_retry_count(self, chat_id: ChatId) -> int:
        state = self.get_chat_state(chat_id)
        return state.role_retry_count

    def get_role(self, chat_id: ChatId, player_id: Any) -> Optional[RoleAnchorRecord]:
        return self.get_chat_state(chat_id).role_anchors.get(player_id)

    def find_role_anchor_by_message(
        self, chat_id: ChatId, message_id: int
    ) -> Optional[RoleAnchorRecord]:
        """Return the role anchor record associated with ``message_id`` if any."""

        state = self.get_chat_state(chat_id)
        for record in state.role_anchors.values():
            if record.message_id == message_id:
                return record
        return None

    def remove_role(self, chat_id: ChatId, player_id: Any) -> None:
        state = self.get_chat_state(chat_id)
        state.role_anchors.pop(player_id, None)

    def set_turn_anchor(
        self, chat_id: ChatId, *, message_id: Optional[int], payload_hash: Optional[str] = None
    ) -> TurnAnchorRecord:
        state = self.get_chat_state(chat_id)
        anchor = state.turn_anchor
        anchor.message_id = message_id
        if payload_hash is not None:
            anchor.payload_hash = payload_hash
        return anchor

    def get_turn_anchor(self, chat_id: ChatId) -> TurnAnchorRecord:
        state = self.get_chat_state(chat_id)
        return state.turn_anchor

    def clear_chat(self, chat_id: ChatId) -> None:
        normalized = self._normalize_chat(chat_id)
        self._registry.pop(normalized, None)

    def increment_edit(self, chat_id: ChatId) -> None:
        state = self.get_chat_state(chat_id)
        state.edit_count += 1

    def increment_fallback(self, chat_id: ChatId) -> None:
        state = self.get_chat_state(chat_id)
        state.fallback_count += 1

    def get_counters(self, chat_id: ChatId) -> tuple[int, int]:
        state = self.get_chat_state(chat_id)
        return state.edit_count, state.fallback_count


class PokerBotViewer:
    _ZERO_WIDTH_SPACE = "\u2063"
    _FORCE_REFRESH_CHARS = ("\u200b", "\u200a")
    _INVISIBLE_CHARS = {
        "\u200b",  # zero width space
        "\u200a",  # hair space
        "\u200c",  # zero width non-joiner
        "\u200d",  # zero width joiner
        "\u200e",  # left-to-right mark
        "\u200f",  # right-to-left mark
        "\u2060",  # word joiner
        "\u2061",
        "\u2062",
        "\u2063",
    }
    _ACTIVE_ANCHOR_STATES = {
        GameState.ROUND_PRE_FLOP,
        GameState.ROUND_FLOP,
        GameState.ROUND_TURN,
        GameState.ROUND_RIVER,
    }
    _SUIT_EMOJI = {
        "♠": "♠\ufe0f",
        "♥": "♥\ufe0f",
        "♦": "♦\ufe0f",
        "♣": "♣\ufe0f",
    }

    @classmethod
    def _format_card_symbol(cls, card_value: Any) -> str:
        text = str(card_value)
        for suit, emoji in cls._SUIT_EMOJI.items():
            text = text.replace(suit, emoji)
        return text

    @classmethod
    def _format_cards_for_keyboard(cls, cards: Sequence[Any]) -> List[str]:
        formatted: List[str] = []
        for card in cards or []:
            try:
                rendered = cls._format_card_symbol(card)
            except Exception:
                continue
            if rendered:
                formatted.append(rendered)
        return formatted

    def _compose_anchor_keyboard(
        self,
        *,
        stage_name: str,
        hole_cards: Sequence[str],
        community_cards: Sequence[str],
    ) -> ReplyKeyboardMarkup:
        return build_player_cards_keyboard(
            hole_cards=hole_cards,
            community_cards=community_cards,
            current_stage=stage_name or "",
        )

    def _reply_keyboard_signature(
        self,
        *,
        text: str,
        reply_markup: ReplyKeyboardMarkup,
        stage_name: str,
        community_cards: Sequence[str],
        hole_cards: Optional[Sequence[str]] = None,
        turn_indicator: str = "",
    ) -> str:
        markup_signature = self._serialize_markup(reply_markup) or ""
        stage_token = (stage_name or "").upper()
        board_token = "|".join(str(card) for card in (community_cards or []))
        hole_token = "|".join(str(card) for card in (hole_cards or []))
        indicator_token = turn_indicator or ""
        payload = (
            f"{text}|{markup_signature}|stage={stage_token}|board={board_token}"
            f"|hole={hole_token}|indicator={indicator_token}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _extract_community_cards(self, game: Game) -> List[str]:
        community_cards_source: Optional[Sequence[Card]] = getattr(
            game, "community_cards", None
        )
        if community_cards_source is None:
            community_cards_source = getattr(game, "cards_table", [])
        try:
            return self._format_cards_for_keyboard(community_cards_source or [])
        except Exception:
            return []

    def _extract_player_hole_cards(self, player: Player) -> List[str]:
        hole_cards_source = getattr(player, "hole_cards", None)
        if hole_cards_source is None:
            hole_cards_source = getattr(player, "cards", [])
        try:
            return self._format_cards_for_keyboard(hole_cards_source or [])
        except Exception:
            return []

    @classmethod
    def _has_visible_text(cls, text: str) -> bool:
        if not text:
            return False
        for char in text:
            if char.isspace():
                continue
            if char in cls._INVISIBLE_CHARS:
                continue
            return True
        return False

    @staticmethod
    def _log_skip_empty(chat_id: ChatId, message_id: Optional[MessageId]) -> None:
        logger.info(
            "SKIP SEND: empty or invisible content for %s, msg %s",
            chat_id,
            message_id,
        )

    @staticmethod
    def _resolve_message_id(result: Any) -> Optional[int]:
        if isinstance(result, Message):
            maybe_id = getattr(result, "message_id", None)
            if isinstance(maybe_id, int):
                return maybe_id
        if isinstance(result, int):
            return result
        if isinstance(result, str):
            try:
                return int(result)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _is_private_chat(chat_id: ChatId) -> bool:
        if isinstance(chat_id, str):
            return chat_id.startswith("private:")
        return False

    @staticmethod
    def _build_hidden_mention(mention_markdown: Optional[Mention]) -> str:
        """Return an invisible Markdown mention for ``mention_markdown``."""

        if not mention_markdown:
            return ""

        try:
            label_end = mention_markdown.index("](")
            link_end = mention_markdown.index(")", label_end + 2)
            link = mention_markdown[label_end + 2 : link_end].strip()
            if link:
                return f"[{PokerBotViewer._ZERO_WIDTH_SPACE}]({link})"
        except ValueError:
            pass

        return mention_markdown

    @staticmethod
    def _build_context(method: str, **values: Any) -> Dict[str, Any]:
        context: Dict[str, Any] = {"method": method}
        for key, value in values.items():
            if value is not None:
                context[key] = value
        return context

    @staticmethod
    def _serialize_markup(
        markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> Optional[str]:
        if markup is None:
            return None
        serializer = getattr(markup, "to_dict", None)
        if callable(serializer):
            try:
                return json.dumps(serializer(), sort_keys=True, ensure_ascii=False)
            except TypeError:
                pass
        try:
            return json.dumps(markup, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(markup)

    def __init__(
        self,
        bot: Bot,
        admin_chat_id: Optional[int] = None,
        *,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        rate_limit_per_second: Optional[int] = DEFAULT_RATE_LIMIT_PER_SECOND,
        rate_limiter_delay: Optional[float] = 0.05,
        update_debounce: float = 0.25,
        request_metrics: Optional[RequestMetrics] = None,
    ):
        # ``update_debounce`` historically controlled how quickly message edits
        # were flushed to Telegram.  The messaging rewrite in mid-2023 stopped
        # consuming the parameter, but we now wire it back in so call sites can
        # opt into even tighter coalescing windows when needed.
        self._message_update_debounce_delay = max(0.0, float(update_debounce))

        self._bot = bot
        self._desk_generator = DeskImageGenerator()
        self._admin_chat_id = admin_chat_id
        self._validator = TelegramPayloadValidator(
            logger_=logger.getChild("validation")
        )
        self._request_metrics = request_metrics or RequestMetrics(
            logger_=logger.getChild("request_metrics")
        )
        # Legacy rate-limit attributes are retained for backwards compatibility
        # with configuration code but do not influence runtime behaviour.
        self._legacy_rate_limit_per_minute = rate_limit_per_minute
        self._legacy_rate_limit_per_second = rate_limit_per_second
        self._legacy_rate_limiter_delay = rate_limiter_delay

        self._message_update_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._message_update_guard = asyncio.Lock()
        self._message_payload_hashes: LRUCache[Tuple[int, int], str] = LRUCache(
            maxsize=1024
        )
        self._message_payload_cache_lock = asyncio.Lock()
        self._last_callback_updates: Dict[Tuple[int, int, str, int], str] = {}
        self._last_callback_edit: Dict[Tuple[int, str], str] = {}
        self._last_message_hash: Dict[int, str] = {}
        self._last_message_hash_lock = asyncio.Lock()
        self._deleted_messages: Set[int] = set()
        self._deleted_messages_lock = asyncio.Lock()
        self._role_anchor_deletions: Dict[int, str] = {}
        self._role_anchor_deletions_lock = asyncio.Lock()
        self._turn_message_cache: LRUCache[Tuple[int, int], str] = LRUCache(
            maxsize=256
        )
        self._turn_message_cache_lock = asyncio.Lock()
        self._inline_keyboard_cache: FIFOCache[str, InlineKeyboardMarkup] = FIFOCache(
            maxsize=32
        )
        self._inline_keyboard_cache_lock = asyncio.Lock()
        self._stage_payload_hashes: LRUCache[Tuple[int, int, str], str] = LRUCache(
            maxsize=4096
        )
        self._stage_payload_hash_lock = asyncio.Lock()
        self._prestart_countdown_tasks: Dict[Tuple[int, str], asyncio.Task[None]] = {}
        self._prestart_countdown_lock = asyncio.Lock()
        self._countdown_transition_pending: Set[int] = set()
        self._countdown_transition_lock = threading.Lock()
        self._pending_updates: Dict[
            Tuple[ChatId, Optional[MessageId]], Dict[str, Any]
        ] = {}
        self._pending_update_tasks: Dict[
            Tuple[ChatId, Optional[MessageId]], asyncio.Task[Optional[MessageId]]
        ] = {}
        self._pending_updates_lock = asyncio.Lock()

        self._anchor_registry = AnchorRegistry()
        self._anchor_locks: Dict[Tuple[int, str], asyncio.Lock] = {}
        self._anchor_lock_guard = asyncio.Lock()

        self._messenger = MessagingService(
            bot,
            logger_=logger.getChild("messaging_service"),
            request_metrics=self._request_metrics,
            deleted_messages=self._deleted_messages,
            deleted_messages_lock=self._deleted_messages_lock,
            last_message_hash=self._last_message_hash,
            last_message_hash_lock=self._last_message_hash_lock,
        )

    async def _cancel_prestart_countdown(
        self, chat_id: ChatId, game_id: Optional[int | str] = None
    ) -> None:
        normalized_chat = self._safe_int(chat_id)
        normalized_game = str(game_id) if game_id is not None else "0"
        key = (normalized_chat, normalized_game)
        task: Optional[asyncio.Task[None]] = None
        async with self._prestart_countdown_lock:
            task = self._prestart_countdown_tasks.pop(key, None)
            if task is not None:
                task.cancel()
                logger.info(
                    "[Countdown] Cancelled prestart countdown for chat %s game %s",
                    normalized_chat,
                    normalized_game,
                )
        if task is not None:
            self._mark_countdown_transition_pending(normalized_chat)

    def _mark_countdown_transition_pending(self, normalized_chat: int) -> None:
        if normalized_chat == 0:
            return
        with self._countdown_transition_lock:
            self._countdown_transition_pending.add(normalized_chat)

    def _clear_countdown_transition_pending(self, chat_id: ChatId) -> None:
        normalized_chat = self._safe_int(chat_id)
        if normalized_chat == 0:
            return
        with self._countdown_transition_lock:
            self._countdown_transition_pending.discard(normalized_chat)

    def _is_countdown_transition_pending(self, normalized_chat: int) -> bool:
        if normalized_chat == 0:
            return False
        with self._countdown_transition_lock:
            return normalized_chat in self._countdown_transition_pending

    async def _is_countdown_active(self, chat_id: ChatId) -> bool:
        """Return True when a prestart countdown task exists for the chat."""

        normalized_chat = self._safe_int(chat_id)
        if normalized_chat == 0:
            return False

        async with self._prestart_countdown_lock:
            for (chat_key, _), task in self._prestart_countdown_tasks.items():
                if chat_key != normalized_chat:
                    continue
                if task is None:
                    continue
                if not task.done():
                    return True

        return False

    @staticmethod
    def _describe_countdown_guard_source(
        *, task_active: bool, transition_pending: bool
    ) -> str:
        sources: List[str] = []
        if task_active:
            sources.append("task")
        if transition_pending:
            sources.append("transition")
        return ",".join(sources) if sources else "none"

    def _create_countdown_task(
        self,
        normalized_chat: int,
        normalized_game: str,
        anchor_message_id: Optional[int],
        end_time: float,
        payload_fn: Callable[[int], Tuple[str, Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]]],
        on_complete: Optional[Callable[[], Awaitable[None]]],
    ) -> asyncio.Task[None]:
        """
        Return an asyncio.Task that runs a per-second countdown until end_time.

        payload_fn(seconds_left:int) -> (text:str, reply_markup|None)

        The task uses schedule_message_update if available, otherwise falls back to _update_message.
        It cleans up self._prestart_countdown_tasks entry when finished or cancelled.
        """

        async def _run() -> None:
            current_message_id = anchor_message_id
            completed = False
            try:
                loop = asyncio.get_event_loop()
                while True:
                    now = loop.time()
                    seconds_left = max(0, int(round(end_time - now)))
                    try:
                        text, reply_markup = payload_fn(seconds_left)
                    except Exception:
                        logger.exception("[Countdown] payload_fn failed")
                        text, reply_markup = "", None
                    try:
                        schedule = getattr(self, "schedule_message_update", None)
                        if callable(schedule):
                            result = await schedule(
                                chat_id=normalized_chat,
                                message_id=current_message_id,
                                text=text,
                                reply_markup=reply_markup,
                                request_category=RequestCategory.ANCHOR,
                            )
                        else:
                            result = await self._update_message(
                                chat_id=normalized_chat,
                                message_id=current_message_id,
                                text=text,
                                reply_markup=reply_markup,
                                request_category=RequestCategory.ANCHOR,
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("[Countdown] failed to send countdown tick")
                    else:
                        if isinstance(result, int):
                            current_message_id = result
                        elif hasattr(result, "message_id"):
                            maybe_id = getattr(result, "message_id", None)
                            if isinstance(maybe_id, int):
                                current_message_id = maybe_id
                        elif result is not None:
                            try:
                                current_message_id = int(result)
                            except (TypeError, ValueError):
                                pass
                    if seconds_left <= 0:
                        completed = True
                        break
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                logger.debug(
                    "[Countdown] Task cancelled for chat %s game %s",
                    normalized_chat,
                    normalized_game,
                )
                raise
            finally:
                mark_transition_pending = False
                async with self._prestart_countdown_lock:
                    existing = self._prestart_countdown_tasks.get((normalized_chat, normalized_game))
                    if existing is asyncio.current_task():
                        self._prestart_countdown_tasks.pop((normalized_chat, normalized_game), None)
                        mark_transition_pending = True
                if mark_transition_pending:
                    self._mark_countdown_transition_pending(normalized_chat)
                logger.info(
                    "[Countdown] Prestart countdown finished for chat %s game %s",
                    normalized_chat,
                    normalized_game,
                )
                if completed and on_complete is not None:
                    try:
                        await on_complete()
                    except Exception:
                        logger.exception(
                            "[Countdown] Countdown completion handler failed",
                            extra={
                                "chat_id": normalized_chat,
                                "game_id": normalized_game,
                            },
                        )

        return asyncio.create_task(_run())

    async def start_prestart_countdown(
        self,
        chat_id: ChatId,
        game_id: int | str,
        anchor_message_id: Optional[MessageId],
        seconds: int,
        payload_fn: Callable[[int], Tuple[str, Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]]],
        on_complete: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        """
        Start or replace a per-chat prestart countdown.

        payload_fn(seconds_left:int) -> (text, reply_markup)
        """

        normalized_chat = self._safe_int(chat_id)
        normalized_game = str(game_id)
        end_time = asyncio.get_event_loop().time() + max(0, int(seconds))
        async with self._prestart_countdown_lock:
            key = (normalized_chat, normalized_game)
            old = self._prestart_countdown_tasks.get(key)
            if old is not None:
                old.cancel()
            task = self._create_countdown_task(
                normalized_chat,
                normalized_game,
                anchor_message_id,
                end_time,
                payload_fn,
                on_complete,
            )
            self._prestart_countdown_tasks[key] = task
        logger.info(
            "[Countdown] Started prestart countdown for chat %s game %s seconds=%s anchor=%s",
            normalized_chat,
            normalized_game,
            seconds,
            anchor_message_id,
        )

    def _payload_hash(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> str:
        markup_hash = self._serialize_markup(reply_markup) or ""
        payload = f"{text}|{markup_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def payload_signature(
        self,
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup],
    ) -> str:
        """Public helper exposing the stable payload hash for callers."""

        return self._payload_hash(text, reply_markup)

    @staticmethod
    def _safe_int(value: Optional[int | str]) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    async def _get_anchor_lock(self, chat_id: ChatId, kind: str) -> asyncio.Lock:
        normalized_chat = self._safe_int(chat_id)
        key = (normalized_chat, kind)
        async with self._anchor_lock_guard:
            lock = self._anchor_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._anchor_locks[key] = lock
            return lock

    async def schedule_message_update(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        key = (chat_id, message_id)
        loop = asyncio.get_running_loop()

        async with self._pending_updates_lock:
            pending_entry = self._pending_updates.get(key)
            future_obj = pending_entry.get("future") if pending_entry else None
            future: asyncio.Future[Optional[MessageId]]
            if isinstance(future_obj, asyncio.Future) and not future_obj.done():
                future = future_obj
            else:
                future = loop.create_future()

            if not future.done():
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": disable_web_page_preview,
                    "disable_notification": disable_notification,
                    "request_category": request_category,
                }
                self._pending_updates[key] = {"payload": payload, "future": future}

            pending_task = self._pending_update_tasks.get(key)
            if pending_task is not None:
                pending_task.cancel()

            task = asyncio.create_task(
                self._execute_debounced_update(key, self._message_update_debounce_delay)
            )
            self._pending_update_tasks[key] = task

        return await future

    async def _execute_debounced_update(
        self, key: Tuple[ChatId, Optional[MessageId]], delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)

            async with self._pending_updates_lock:
                active_task = self._pending_update_tasks.get(key)
                current_task = asyncio.current_task()
                if active_task is not current_task:
                    return

                entry = self._pending_updates.pop(key, None)
                self._pending_update_tasks.pop(key, None)

            if not entry:
                return

            payload = entry.get("payload", {})
            future = entry.get("future")

            try:
                result = await self._update_message(**payload)
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                logger.exception(
                    "Debounced update failed",
                    extra={
                        "chat_id": payload.get("chat_id"),
                        "message_id": payload.get("message_id"),
                        "request_category": payload.get("request_category"),
                    },
                )
            else:
                if future is not None and not future.done():
                    future.set_result(result)
        except asyncio.CancelledError:
            return

    async def _purge_pending_updates(
        self, chat_id: ChatId, message_id: Optional[MessageId]
    ) -> None:
        key = (chat_id, message_id)
        entry: Optional[Dict[str, Any]] = None
        task: Optional[asyncio.Task[Optional[MessageId]]] = None
        async with self._pending_updates_lock:
            entry = self._pending_updates.pop(key, None)
            task = self._pending_update_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        if entry:
            future = entry.get("future")
            if isinstance(future, asyncio.Future) and not future.done():
                future.cancel()
            logger.debug(
                "Purged pending updates",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )

    async def _acquire_message_lock(
        self, chat_id: ChatId, message_id: Optional[MessageId]
    ) -> asyncio.Lock:
        normalized_chat = self._safe_int(chat_id)
        normalized_message = (
            self._safe_int(message_id) if message_id is not None else -1
        )
        key = (normalized_chat, normalized_message)
        async with self._message_update_guard:
            lock = self._message_update_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._message_update_locks[key] = lock
        return lock

    @staticmethod
    def _normalize_stage_name(stage: Optional[str]) -> str:
        return stage if stage else "<unknown>"

    async def should_skip_stage_update(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        stage: str,
        payload_hash: str,
    ) -> bool:
        """Return ``True`` if the same stage payload was already broadcast."""

        if message_id is None:
            return False

        normalized_chat = self._safe_int(chat_id)
        normalized_message = self._safe_int(message_id)
        stage_key = (normalized_chat, normalized_message, stage)
        cached = await self._get_stage_payload_hash(stage_key)
        return cached == payload_hash

    def _should_skip_callback_update(
        self,
        key: Tuple[int, int, str, int],
        callback_id: str,
    ) -> bool:
        stored_token = self._last_callback_updates.get(key)
        return stored_token == callback_id

    def _store_callback_update_token(
        self,
        key: Tuple[int, int, str, int],
        callback_id: str,
    ) -> None:
        self._last_callback_updates[key] = callback_id

    async def _clear_callback_tokens_for_message(
        self,
        normalized_chat: int,
        normalized_message: int,
    ) -> None:
        to_remove = [
            key
            for key in self._last_callback_updates
            if key[0] == normalized_chat and key[1] == normalized_message
        ]
        for key in to_remove:
            self._last_callback_updates.pop(key, None)

        callback_throttle_keys = [
            key
            for key in self._last_callback_edit
            if key[0] == normalized_message
        ]
        for key in callback_throttle_keys:
            self._last_callback_edit.pop(key, None)

        await self._pop_last_text_hash(normalized_message)
        await self._clear_stage_payloads_for_message(
            normalized_chat, normalized_message
        )

    async def _get_payload_hash(
        self, key: Tuple[int, int]
    ) -> Optional[str]:
        async with self._message_payload_cache_lock:
            return self._message_payload_hashes.get(key)

    async def _set_payload_hash(self, key: Tuple[int, int], value: str) -> None:
        async with self._message_payload_cache_lock:
            self._message_payload_hashes[key] = value
            logger.debug(
                "Payload cache size %s/%s",
                self._message_payload_hashes.currsize,
                self._message_payload_hashes.maxsize,
            )

    async def _pop_payload_hash(self, key: Tuple[int, int]) -> None:
        async with self._message_payload_cache_lock:
            self._message_payload_hashes.pop(key, None)

    async def _get_last_text_hash(self, message_id: int) -> Optional[str]:
        async with self._last_message_hash_lock:
            return self._last_message_hash.get(message_id)

    async def _set_last_text_hash(self, message_id: int, value: str) -> None:
        async with self._last_message_hash_lock:
            self._last_message_hash[message_id] = value
            logger.debug(
                "Text hash cache size %s",
                len(self._last_message_hash),
            )

    async def _pop_last_text_hash(self, message_id: int) -> None:
        async with self._last_message_hash_lock:
            self._last_message_hash.pop(message_id, None)

    async def _is_message_deleted(self, message_id: int) -> bool:
        async with self._deleted_messages_lock:
            return message_id in self._deleted_messages

    async def _mark_message_deleted(self, message_id: int) -> None:
        async with self._deleted_messages_lock:
            self._deleted_messages.add(message_id)

    async def _unmark_message_deleted(self, message_id: int) -> None:
        async with self._deleted_messages_lock:
            self._deleted_messages.discard(message_id)

    async def _record_role_anchor_deletion(self, message_id: int, reason: str) -> None:
        async with self._role_anchor_deletions_lock:
            self._role_anchor_deletions[message_id] = reason

    async def _consume_role_anchor_deletion(self, message_id: int) -> Optional[str]:
        async with self._role_anchor_deletions_lock:
            return self._role_anchor_deletions.pop(message_id, None)

    async def _peek_role_anchor_deletion(self, message_id: int) -> Optional[str]:
        async with self._role_anchor_deletions_lock:
            return self._role_anchor_deletions.get(message_id)

    async def _get_last_edit_error(
        self, chat_id: ChatId, message_id: int
    ) -> Optional[str]:
        messenger = getattr(self, "_messenger", None)
        if messenger is None:
            return None
        accessor = getattr(messenger, "get_last_edit_error", None)
        if accessor is None:
            return None
        normalized_chat = self._safe_int(chat_id)
        if normalized_chat is None:
            return None
        try:
            result = accessor(normalized_chat, int(message_id))
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        if isinstance(result, str):
            return result
        return None

    async def _probe_message_existence(
        self, chat_id: ChatId, message_id: int
    ) -> Optional[bool]:
        bot = getattr(self, "_bot", None)
        if bot is None:
            return None
        inspector = getattr(bot, "get_chat_history", None)
        if inspector is None or not callable(inspector):
            return None
        try:
            history = await inspector(
                chat_id=chat_id,
                offset=0,
                limit=10,
                offset_id=message_id,
            )
        except Exception:
            return None
        if isinstance(history, Sequence):
            for item in history:
                maybe_id = getattr(item, "message_id", None)
                try:
                    if int(maybe_id) == int(message_id):
                        return True
                except (TypeError, ValueError):
                    continue
            return False
        return None

    def _strip_invisible_suffix(self, text: str) -> Tuple[str, str]:
        stripped = text.rstrip("".join(self._INVISIBLE_CHARS))
        suffix = text[len(stripped) :]
        return stripped, suffix

    @staticmethod
    def _detect_anchor_turn_light(text: str) -> str:
        normalized_text = text or ""
        turn_phrase = "نوبت این بازیکن است"
        for indicator in ("🟢", "🔴"):
            if f"{indicator} {turn_phrase}" in normalized_text:
                return indicator
        return ""

    def _force_anchor_text_refresh(
        self, text: str, *, last_toggle: Optional[str] = None
    ) -> Tuple[str, str, str]:
        base_text, suffix = self._strip_invisible_suffix(text)
        previous_toggle: Optional[str] = None
        if last_toggle in self._FORCE_REFRESH_CHARS:
            previous_toggle = last_toggle
        if not previous_toggle:
            for char in reversed(suffix):
                if char in self._FORCE_REFRESH_CHARS:
                    previous_toggle = char
                    break
        next_toggle = (
            self._FORCE_REFRESH_CHARS[1]
            if previous_toggle == self._FORCE_REFRESH_CHARS[0]
            else self._FORCE_REFRESH_CHARS[0]
        )
        refreshed_text = base_text + next_toggle
        return refreshed_text, base_text, next_toggle

    async def _should_create_anchor_fallback(
        self,
        *,
        chat_id: ChatId,
        message_id: int,
        player_id: Any,
        last_error: Optional[str],
        forced_refresh: bool,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        request_category: Optional[RequestCategory] = None,
        current_text: Optional[str] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        normalized_message = self._safe_int(message_id)
        diagnostics: Dict[str, Any] = {
            "chat_id": chat_id,
            "player_id": player_id,
            "message_id": message_id,
            "forced_refresh": forced_refresh,
            "retry_attempted": False,
            "retry_success": False,
            "retry_attempts": 0,
        }
        if normalized_message is None:
            diagnostics["decision"] = "missing_message_id"
            return True, "missing_message_id", diagnostics

        deleted_flag = await self._is_message_deleted(normalized_message)
        diagnostics["deleted_flag"] = deleted_flag

        record: Optional[RoleAnchorRecord] = None
        role_anchor_detected = False
        if request_category == RequestCategory.ANCHOR or request_category is None:
            record = self._anchor_registry.get_role(chat_id, player_id)
            if record is not None and record.message_id == normalized_message:
                role_anchor_detected = True
        diagnostics["has_record"] = record is not None
        diagnostics["registry_message_match"] = (
            record.message_id == normalized_message if record is not None else False
        )

        error_text = (last_error or "").lower()
        if error_text:
            diagnostics["last_error"] = error_text
            diagnostics["last_error_raw"] = last_error

        if role_anchor_detected:
            diagnostics["anchor_type"] = "role"
            diagnostics["anchor_no_fallback"] = True
            diagnostics["role_retry_count"] = self._anchor_registry.get_role_retry_count(
                chat_id
            )
            toggle_char = ""
            retry_success = False
            text_source: Optional[str] = (
                current_text
                or (record.current_text or record.base_text if record is not None else None)
            )
            if text_source:
                await asyncio.sleep(0.15)
                refreshed_text, base_text, toggle_char = self._force_anchor_text_refresh(
                    text_source, last_toggle=record.refresh_toggle if record else None
                )
                diagnostics["retry_attempted"] = True
                diagnostics["retry_attempts"] = 1
                diagnostics["retry_reason"] = "role_anchor_recovery"
                diagnostics["retry_invisible_char"] = toggle_char
                self._anchor_registry.increment_role_retry(chat_id)
                diagnostics["role_retry_count"] = (
                    self._anchor_registry.get_role_retry_count(chat_id)
                )
                try:
                    result = await self._update_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=refreshed_text,
                        reply_markup=reply_markup,
                        force_send=True,
                        request_category=RequestCategory.ANCHOR,
                    )
                except Exception as exc:
                    diagnostics["retry_exception"] = type(exc).__name__
                else:
                    resolved_id = self._resolve_message_id(result)
                    if resolved_id is not None:
                        retry_success = True
                        diagnostics["retry_success"] = True
                        diagnostics["decision"] = "edit_recovered"
                        if record is not None:
                            self._anchor_registry.update_role(
                                chat_id,
                                player_id,
                                message_id=resolved_id,
                                current_text=base_text,
                                refresh_toggle=toggle_char,
                            )
                        logger.info(
                            "Role anchor edit retry",
                            extra={
                                "chat_id": chat_id,
                                "player_id": player_id,
                                "message_id": message_id,
                                "retry_success": True,
                                "last_error": error_text,
                                "invisible_char": toggle_char,
                                "anchor_no_fallback": True,
                            },
                        )
                        return False, "edit_recovered", diagnostics
            else:
                diagnostics["retry_unavailable"] = "no_text"

            normalized_anchor = self._safe_int(message_id)
            retry_error: Optional[str] = None
            if normalized_anchor is not None:
                try:
                    retry_error = await self._get_last_edit_error(chat_id, normalized_anchor)
                except Exception:
                    retry_error = None
            if retry_error:
                error_text = retry_error.lower()
                diagnostics["retry_last_error"] = error_text
                diagnostics["last_error"] = error_text
                diagnostics["last_error_raw"] = retry_error

            if error_text and (
                "message can't be edited" in error_text
                or "telegram_not_editable" in error_text
            ):
                logger.warning(
                    "Role anchor not editable",
                    extra={
                        "chat_id": chat_id,
                        "player_id": player_id,
                        "message_id": message_id,
                        "last_error": error_text,
                        "anchor_no_fallback": True,
                    },
                )
                alternate_success = False
                diagnostics["alternate_markup_attempted"] = reply_markup is not None
                if reply_markup is not None:
                    try:
                        alternate_success = await self.edit_message_reply_markup(
                            chat_id=chat_id,
                            message_id=message_id,
                            reply_markup=reply_markup,
                        )
                    except Exception as exc:
                        diagnostics["alternate_exception"] = type(exc).__name__
                if alternate_success:
                    diagnostics["alternate_markup_success"] = True
                    diagnostics["decision"] = "edit_recovered"
                    logger.info(
                        "Role anchor alternate markup edit",
                        extra={
                            "chat_id": chat_id,
                            "player_id": player_id,
                            "message_id": message_id,
                            "retry_success": True,
                            "anchor_no_fallback": True,
                        },
                    )
                    return False, "edit_recovered", diagnostics
                diagnostics["alternate_markup_success"] = False

            if error_text and "message to edit not found" in error_text and not deleted_flag:
                diagnostics["anomaly"] = "message_not_found"
                logger.warning(
                    "Role anchor edit anomaly",
                    extra={
                        "chat_id": chat_id,
                        "player_id": player_id,
                        "message_id": message_id,
                        "anchor_no_fallback": True,
                    },
                )

            logger.info(
                "Role anchor fallback skipped",
                extra={
                    "chat_id": chat_id,
                    "player_id": player_id,
                    "message_id": message_id,
                    "retry_success": retry_success,
                    "last_error": error_text,
                    "invisible_char": toggle_char,
                    "anchor_no_fallback": True,
                },
            )
            diagnostics.setdefault("retry_success", False)
            diagnostics.setdefault("decision", "anchor_no_fallback")
            return False, diagnostics["decision"], diagnostics

        if deleted_flag:
            diagnostics["decision"] = "deleted_flag"
            return True, "deleted_flag", diagnostics

        history_probe = await self._probe_message_existence(chat_id, normalized_message)
        diagnostics["history_probe"] = history_probe

        effective_deleted = deleted_flag or history_probe is False
        diagnostics["effective_deleted"] = effective_deleted

        fallback_reason: Optional[str] = None
        if effective_deleted:
            fallback_reason = "registry_marked_deleted"

        if error_text and "too many requests" in error_text:
            diagnostics["decision"] = "rate_limited"
            return False, "rate_limited", diagnostics

        if error_text and "retry later" in error_text:
            diagnostics["decision"] = "retry_later"
            return False, "retry_later", diagnostics

        retry_reason: Optional[str] = None
        if error_text and "message is not modified" in error_text:
            if not forced_refresh:
                diagnostics["decision"] = "force_refresh"
                return False, "force_refresh", diagnostics
            retry_reason = "not_modified_after_refresh"

        if fallback_reason is None and history_probe is False and not effective_deleted:
            retry_reason = retry_reason or "history_probe_missing"
            fallback_reason = "history_missing"

        retry_attempts = 0

        async def attempt_forced_refresh(reason: str) -> bool:
            nonlocal retry_attempts, error_text, last_error
            if retry_attempts >= 1:
                diagnostics["retry_limit_reached"] = True
                return False
            text_source: Optional[str] = None
            if record is not None:
                text_source = record.current_text or record.base_text
            if not text_source:
                diagnostics["retry_unavailable"] = "no_text"
                return False
            retry_attempts += 1
            diagnostics["retry_attempted"] = True
            diagnostics["retry_attempts"] = retry_attempts
            diagnostics["retry_reason"] = reason
            refreshed_text, base_text, toggle_char = self._force_anchor_text_refresh(
                text_source, last_toggle=record.refresh_toggle if record else None
            )
            try:
                result = await self._update_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=refreshed_text,
                    reply_markup=None,
                    force_send=True,
                    request_category=RequestCategory.ANCHOR,
                )
            except Exception as exc:
                diagnostics["retry_exception"] = type(exc).__name__
            else:
                resolved_id = self._resolve_message_id(result)
                if resolved_id is not None:
                    diagnostics["retry_success"] = True
                    self._anchor_registry.update_role(
                        chat_id,
                        player_id,
                        message_id=resolved_id,
                        current_text=base_text,
                        refresh_toggle=toggle_char,
                    )
                    diagnostics["decision"] = "edit_recovered"
                    return True
            normalized_anchor = self._safe_int(message_id)
            if normalized_anchor:
                try:
                    retry_error = await self._get_last_edit_error(
                        chat_id, normalized_anchor
                    )
                except Exception:
                    retry_error = None
                if retry_error:
                    last_error = retry_error
                    error_text = retry_error.lower()
                    diagnostics["retry_last_error"] = error_text
                    diagnostics["last_error"] = error_text
                    diagnostics["last_error_raw"] = retry_error
            diagnostics["retry_success"] = False
            return False

        if retry_reason is not None and fallback_reason not in {
            "registry_marked_deleted",
            "telegram_not_found",
            "telegram_not_editable",
        }:
            if await attempt_forced_refresh(retry_reason):
                return False, "edit_recovered", diagnostics

        if fallback_reason is None:
            diagnostics["decision"] = "skip"
            return False, "skip", diagnostics

        if diagnostics.get("retry_attempted"):
            diagnostics.setdefault("retry_success", False)

        diagnostics["decision"] = fallback_reason
        logger.info(
            "Pre-fallback diagnostics",
            extra={
                **diagnostics,
                "fallback_reason": fallback_reason,
            },
        )
        record = None
        if request_category == RequestCategory.ANCHOR:
            record = self._resolve_role_anchor_record(chat_id, normalized_message)
        if record is None:
            await self._mark_message_deleted(normalized_message)
        else:
            self._log_anchor_preservation_skip(
                chat_id=chat_id,
                message_id=normalized_message,
                record=record,
                extra_details={"decision": diagnostics.get("decision")},
            )
        return True, fallback_reason, diagnostics
    async def _get_turn_cache_hash(
        self, key: Tuple[int, int]
    ) -> Optional[str]:
        async with self._turn_message_cache_lock:
            return self._turn_message_cache.get(key)

    async def _set_turn_cache_hash(self, key: Tuple[int, int], value: str) -> None:
        async with self._turn_message_cache_lock:
            self._turn_message_cache[key] = value
            logger.debug(
                "Turn cache size %s/%s",
                self._turn_message_cache.currsize,
                self._turn_message_cache.maxsize,
            )

    async def _pop_turn_cache_hash(self, key: Tuple[int, int]) -> None:
        async with self._turn_message_cache_lock:
            self._turn_message_cache.pop(key, None)

    async def _get_stage_payload_hash(
        self, key: Tuple[int, int, str]
    ) -> Optional[str]:
        async with self._stage_payload_hash_lock:
            return self._stage_payload_hashes.get(key)

    async def _set_stage_payload_hash(
        self, key: Tuple[int, int, str], value: str
    ) -> None:
        async with self._stage_payload_hash_lock:
            self._stage_payload_hashes[key] = value
            logger.debug(
                "Stage payload cache size %s/%s",
                self._stage_payload_hashes.currsize,
                self._stage_payload_hashes.maxsize,
            )

    async def _clear_stage_payloads_for_message(
        self, normalized_chat: int, normalized_message: int
    ) -> None:
        async with self._stage_payload_hash_lock:
            keys_to_remove = [
                key
                for key in self._stage_payload_hashes
                if key[0] == normalized_chat and key[1] == normalized_message
            ]
            for key in keys_to_remove:
                self._stage_payload_hashes.pop(key, None)

    def _detect_callback_context(
        self,
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        try:
            stack = inspect.stack(context=0)
        except Exception:
            return None, None, None

        callback_id: Optional[str] = None
        stage_name: Optional[str] = None
        user_id: Optional[int] = None
        try:
            for frame_info in stack:
                locals_ = frame_info.frame.f_locals

                callback = locals_.get("callback_query")
                if callback is None:
                    update = locals_.get("update")
                    if update is not None:
                        callback = getattr(update, "callback_query", None)

                if callback is not None:
                    if callback_id is None:
                        raw_id = getattr(callback, "id", None) or getattr(
                            callback, "query_id", None
                        )
                        if raw_id is None:
                            data = getattr(callback, "data", None)
                            if data is not None:
                                raw_id = f"data:{data}"
                        if raw_id is None:
                            from_user = getattr(callback, "from_user", None)
                            if from_user is not None:
                                user_identifier = getattr(from_user, "id", None)
                                if user_identifier is not None:
                                    raw_id = f"user:{user_identifier}"
                        if raw_id is not None:
                            callback_id = str(raw_id)

                    if user_id is None:
                        from_user = getattr(callback, "from_user", None)
                        if from_user is not None:
                            raw_user_id = getattr(from_user, "id", None)
                            if raw_user_id is not None:
                                try:
                                    user_id = int(raw_user_id)
                                except (TypeError, ValueError):
                                    user_id = None

                if stage_name is None:
                    game = locals_.get("game")
                    if game is not None:
                        state = getattr(game, "state", None)
                        if state is not None:
                            name = getattr(state, "name", None)
                            if isinstance(name, str):
                                stage_name = name
                            else:
                                stage_name = str(state)

                if callback_id is not None and stage_name is not None:
                    break

            return callback_id, stage_name, user_id
        except Exception:
            return None, None, None
        finally:
            del stack

    async def _update_message(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[MessageId],
        text: str,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = True,
        disable_notification: bool = False,
        force_send: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        context = self._build_context(
            "update_message", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping update_message due to invalid text",
                extra={"context": context},
            )
            return message_id

        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return message_id

        if (
            request_category == RequestCategory.ANCHOR
            and reply_markup is not None
            and not isinstance(reply_markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup))
        ):
            logger.warning(
                "Discarding unsupported reply markup for anchor update",
                extra={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "markup_type": type(reply_markup).__name__,
                },
            )
            reply_markup = None

        payload_hash = self._payload_hash(normalized_text, reply_markup)
        message_text_hash = hashlib.md5(normalized_text.encode("utf-8")).hexdigest()
        normalized_chat = self._safe_int(chat_id)
        normalized_existing_message = (
            self._safe_int(message_id) if message_id is not None else None
        )
        message_key: Optional[Tuple[int, int]] = (
            (normalized_chat, normalized_existing_message)
            if normalized_existing_message is not None
            else None
        )
        stripped_anchor_text: Optional[str] = None
        anchor_refresh_suffix: str = ""
        anchor_refresh_plan: Optional[Dict[str, Any]] = None
        callback_id: Optional[str] = None
        callback_stage_name = self._normalize_stage_name(request_category.value)
        callback_user_id: Optional[int] = None
        callback_token_key: Optional[Tuple[int, int, str, int]] = None
        callback_throttle_key: Optional[Tuple[int, str]] = None
        if request_category == RequestCategory.ANCHOR:
            stripped_anchor_text, anchor_refresh_suffix = self._strip_invisible_suffix(
                normalized_text
            )
        if normalized_existing_message is not None:
            callback_id, detected_stage, detected_user_id = (
                self._detect_callback_context()
            )
            if callback_id is not None:
                callback_stage_name = self._normalize_stage_name(detected_stage)
                callback_user_id = self._safe_int(detected_user_id)
                callback_token_key = (
                    normalized_chat,
                    normalized_existing_message,
                    callback_stage_name,
                    callback_user_id,
                )
                callback_throttle_key = (
                    normalized_existing_message,
                    callback_stage_name,
                )
                last_callback_token = self._last_callback_edit.get(
                    callback_throttle_key
                )
                if last_callback_token == callback_id:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to callback throttling"
                    )
                    return message_id
                if self._should_skip_callback_update(
                    callback_token_key, callback_id
                ):
                    logger.debug(
                        "Skipping update_message due to callback throttling",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "callback_id": callback_id,
                            "game_stage": callback_stage_name,
                            "callback_user_id": callback_user_id,
                            "trigger": "callback_query",
                        },
                    )
                    return message_id
        stage_key: Optional[Tuple[int, int, str]] = (
            (normalized_chat, normalized_existing_message, callback_stage_name)
            if normalized_existing_message is not None
            else None
        )
        previous_payload_hash: Optional[str] = None
        if normalized_existing_message is not None:
            if await self._is_message_deleted(normalized_existing_message):
                debug_trace_logger.info(
                    f"Skipping editMessageText for message_id={message_id} because it was deleted"
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id
            previous_text_hash = await self._get_last_text_hash(
                normalized_existing_message
            )
            if previous_text_hash == message_text_hash and not force_send:
                payload_changed = False
                if message_key is not None:
                    previous_payload_hash = await self._get_payload_hash(message_key)
                    payload_changed = previous_payload_hash != payload_hash

                anchor_markup_only = (
                    request_category == RequestCategory.ANCHOR
                    and payload_changed
                    and isinstance(reply_markup, InlineKeyboardMarkup)
                )
                force_full_anchor_update = (
                    request_category == RequestCategory.ANCHOR
                    and payload_changed
                    and not isinstance(reply_markup, InlineKeyboardMarkup)
                )

                if anchor_markup_only:
                    logger.debug(
                        "Anchor text unchanged; will refresh inline markup",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                        },
                    )
                elif not force_full_anchor_update:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to no content change"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
        turn_cache_key = message_key if request_category == RequestCategory.TURN else None

        is_reply_keyboard = isinstance(reply_markup, ReplyKeyboardMarkup)
        should_resend_reply_keyboard = (
            is_reply_keyboard and normalized_existing_message is not None
        )

        if (
            request_category == RequestCategory.ANCHOR
            and is_reply_keyboard
            and normalized_existing_message is not None
        ):
            anchor_record = self._resolve_role_anchor_record(
                chat_id, normalized_existing_message
            )
            stripped_text = stripped_anchor_text or normalized_text
            markup_signature = self._serialize_markup(reply_markup) or ""
            refresh_toggle = getattr(anchor_record, "refresh_toggle", None)
            if anchor_refresh_suffix:
                for char in reversed(anchor_refresh_suffix):
                    if char in self._FORCE_REFRESH_CHARS:
                        refresh_toggle = char
                        break
            if refresh_toggle is None:
                refresh_toggle = getattr(anchor_record, "refresh_toggle", "") or ""
            keyboard_changed = True
            text_changed = True
            if anchor_record is not None:
                cached_markup = getattr(anchor_record, "markup_signature", "") or ""
                keyboard_changed = cached_markup != markup_signature
                cached_text = (
                    getattr(anchor_record, "current_text", None)
                    or getattr(anchor_record, "base_text", "")
                    or ""
                )
                text_changed = stripped_text != cached_text
                if not keyboard_changed:
                    should_resend_reply_keyboard = False
            else:
                should_resend_reply_keyboard = True
            anchor_refresh_plan = {
                "record": anchor_record,
                "markup_signature": markup_signature,
                "stripped_text": stripped_text,
                "keyboard_changed": keyboard_changed,
                "text_changed": text_changed,
                "refresh_toggle": refresh_toggle,
                "turn_light": self._detect_anchor_turn_light(stripped_text),
            }

        if stage_key is not None and not force_send and not is_reply_keyboard:
            cached_stage_hash = await self._get_stage_payload_hash(stage_key)
            if cached_stage_hash == payload_hash:
                logger.debug(
                    "Skipping update_message due to stage throttle",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "game_stage": callback_stage_name,
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        if message_key is not None and not force_send:
            if previous_payload_hash is None:
                previous_payload_hash = await self._get_payload_hash(message_key)
            if previous_payload_hash == payload_hash:
                logger.debug(
                    "Skipping update_message due to identical payload",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "callback_id": callback_id,
                        "game_stage": callback_stage_name,
                        "callback_user_id": callback_user_id,
                        "trigger": "callback_query"
                        if callback_id is not None
                        else "automatic",
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        if turn_cache_key is not None and not force_send:
            cached_turn_hash = await self._get_turn_cache_hash(turn_cache_key)
            if cached_turn_hash == payload_hash:
                logger.debug(
                    "Skipping turn update due to LRU cache hit",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            if normalized_existing_message is not None:
                if await self._is_message_deleted(normalized_existing_message):
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} because it was deleted"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
                previous_text_hash = await self._get_last_text_hash(
                    normalized_existing_message
                )
                if previous_text_hash == message_text_hash and not force_send:
                    payload_changed = False
                    if message_key is not None:
                        if previous_payload_hash is None:
                            previous_payload_hash = await self._get_payload_hash(
                                message_key
                            )
                        payload_changed = previous_payload_hash != payload_hash

                    anchor_markup_only = (
                        request_category == RequestCategory.ANCHOR
                        and payload_changed
                        and isinstance(reply_markup, InlineKeyboardMarkup)
                    )
                    force_full_anchor_update = (
                        request_category == RequestCategory.ANCHOR
                        and payload_changed
                        and not isinstance(reply_markup, InlineKeyboardMarkup)
                    )

                    if anchor_markup_only:
                        if reply_markup is not None and not isinstance(
                            reply_markup, (InlineKeyboardMarkup, ReplyKeyboardMarkup)
                        ):
                            logger.debug(
                                "Ignoring unsupported reply markup during anchor refresh",
                                extra={
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                    "markup_type": type(reply_markup).__name__,
                                },
                            )
                            reply_markup = None
                        callback_token_registered = False
                        if (
                            callback_id is not None
                            and callback_throttle_key is not None
                        ):
                            self._last_callback_edit[
                                callback_throttle_key
                            ] = callback_id
                            callback_token_registered = True
                        try:
                            updated = await self.edit_message_reply_markup(
                                chat_id=chat_id,
                                message_id=message_id,
                                reply_markup=reply_markup,
                            )
                        except Exception:
                            if callback_token_registered:
                                self._last_callback_edit.pop(
                                    callback_throttle_key, None
                                )
                            raise
                        if not updated and callback_token_registered:
                            self._last_callback_edit.pop(
                                callback_throttle_key, None
                            )
                        if updated:
                            await self._set_last_text_hash(
                                normalized_existing_message, message_text_hash
                            )
                            if message_key is not None:
                                await self._set_payload_hash(
                                    message_key, payload_hash
                                )
                            if stage_key is not None:
                                await self._set_stage_payload_hash(
                                    stage_key, payload_hash
                                )
                            if turn_cache_key is not None:
                                await self._set_turn_cache_hash(
                                    message_key, payload_hash
                                )
                            await self._unmark_message_deleted(
                                normalized_existing_message
                            )
                            if (
                                callback_id is not None
                                and callback_token_key is not None
                            ):
                                self._store_callback_update_token(
                                    callback_token_key, callback_id
                                )
                            if (
                                callback_id is not None
                                and callback_throttle_key is not None
                            ):
                                self._last_callback_edit[
                                    callback_throttle_key
                                ] = callback_id
                            return message_id
                    if not force_full_anchor_update:
                        debug_trace_logger.info(
                            f"Skipping editMessageText for message_id={message_id} due to no content change"
                        )
                        await self._request_metrics.record_skip(
                            chat_id=normalized_chat,
                            category=request_category,
                        )
                        return message_id
            if message_key is not None and not force_send:
                if previous_payload_hash is None:
                    previous_payload_hash = await self._get_payload_hash(
                        message_key
                    )
                if previous_payload_hash == payload_hash:
                    logger.debug(
                        "Skipping update_message inside lock due to identical payload",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "callback_id": callback_id,
                            "game_stage": callback_stage_name,
                            "callback_user_id": callback_user_id,
                            "trigger": "callback_query"
                            if callback_id is not None
                            else "automatic",
                        },
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if stage_key is not None and not force_send:
                cached_stage_hash = await self._get_stage_payload_hash(stage_key)
                if cached_stage_hash == payload_hash:
                    logger.debug(
                        "Skipping update_message inside lock due to stage throttle",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "payload_hash": payload_hash,
                            "game_stage": callback_stage_name,
                        },
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if (
                callback_token_key is not None
                and callback_id is not None
                and callback_throttle_key is not None
            ):
                last_callback_token = self._last_callback_edit.get(
                    callback_throttle_key
                )
                if last_callback_token == callback_id:
                    debug_trace_logger.info(
                        f"Skipping editMessageText for message_id={message_id} due to callback throttling"
                    )
                    await self._request_metrics.record_skip(
                        chat_id=normalized_chat,
                        category=request_category,
                    )
                    return message_id
            if (
                callback_token_key is not None
                and callback_id is not None
                and self._should_skip_callback_update(callback_token_key, callback_id)
            ):
                logger.debug(
                    "Skipping update_message inside lock due to callback throttling",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "payload_hash": payload_hash,
                        "callback_id": callback_id,
                        "game_stage": callback_stage_name,
                        "callback_user_id": callback_user_id,
                        "trigger": "callback_query",
                    },
                )
                await self._request_metrics.record_skip(
                    chat_id=normalized_chat,
                    category=request_category,
                )
                return message_id

            callback_token_registered = False
            try:
                if message_id is None or should_resend_reply_keyboard:
                    result = await self._messenger.send_message(
                        chat_id=chat_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
                        request_category=request_category,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                        disable_notification=disable_notification,
                    )
                    new_message_id: Optional[MessageId] = getattr(
                        result, "message_id", None
                    )
                    if (
                        should_resend_reply_keyboard
                        and normalized_existing_message is not None
                    ):
                        resolved_new_id = (
                            self._safe_int(new_message_id)
                            if new_message_id is not None
                            else None
                        )
                        if (
                            resolved_new_id is not None
                            and resolved_new_id != normalized_existing_message
                            and normalized_chat != 0
                        ):
                            async def _delete_replaced_message() -> None:
                                if request_category == RequestCategory.ANCHOR:
                                    anchor_record = self._resolve_role_anchor_record(
                                        chat_id, normalized_existing_message
                                    )
                                    stage_value = self._anchor_registry.get_stage(chat_id)
                                    resolved_stage = self._resolve_game_state(stage_value)
                                    stage_name = getattr(resolved_stage, "name", None)
                                    countdown_active = False
                                    if resolved_stage == GameState.INITIAL:
                                        countdown_active = await self._is_countdown_active(
                                            chat_id
                                        )
                                    countdown_transition_pending = (
                                        self._is_countdown_transition_pending(
                                            self._safe_int(chat_id)
                                        )
                                    )
                                    countdown_guard_source = (
                                        self._describe_countdown_guard_source(
                                            task_active=countdown_active,
                                            transition_pending=countdown_transition_pending,
                                        )
                                    )
                                    if (
                                        anchor_record is not None
                                        and (
                                            resolved_stage in self._ACTIVE_ANCHOR_STATES
                                            or (
                                                resolved_stage == GameState.INITIAL
                                                and countdown_active
                                            )
                                        )
                                    ):
                                        self._log_anchor_preservation_skip(
                                            chat_id=chat_id,
                                            message_id=normalized_existing_message,
                                            record=anchor_record,
                                            extra_details={
                                                "stage": stage_name,
                                                "reason": "countdown_guard",
                                                "countdown_active": countdown_active,
                                                "countdown_guard_task_active": countdown_active,
                                                "countdown_guard_transition_pending": countdown_transition_pending,
                                                "countdown_guard_source": countdown_guard_source,
                                            },
                                        )
                                        logger.info(
                                            "[AnchorPersistence] Skipped deletion for role anchor during INITIAL countdown",
                                            extra={
                                                "chat_id": chat_id,
                                                "message_id": normalized_existing_message,
                                                "player_id": getattr(
                                                    anchor_record, "player_id", None
                                                ),
                                                "stage": stage_name,
                                                "countdown_active": countdown_active,
                                                "countdown_guard_task_active": countdown_active,
                                                "countdown_guard_transition_pending": countdown_transition_pending,
                                                "countdown_guard_source": countdown_guard_source,
                                            },
                                        )
                                        return
                                    try:
                                        await self.delete_message(
                                            chat_id=chat_id,
                                            message_id=normalized_existing_message,
                                            allow_anchor_deletion=True,
                                            anchor_reason="anchor_refresh",
                                        )
                                    except Exception:
                                        logger.warning(
                                            "Failed to delete replaced anchor message",
                                            extra={
                                                "chat_id": chat_id,
                                                "message_id": normalized_existing_message,
                                            },
                                        )
                                    return

                                try:
                                    await self.delete_message(
                                        chat_id=chat_id,
                                        message_id=normalized_existing_message,
                                    )
                                except Exception:
                                    logger.warning(
                                        "Failed to delete replaced reply keyboard message",
                                        extra={
                                            "chat_id": chat_id,
                                            "message_id": message_id,
                                        },
                                    )

                            asyncio.create_task(_delete_replaced_message())
                else:
                    if (
                        callback_id is not None
                        and callback_throttle_key is not None
                    ):
                        self._last_callback_edit[
                            callback_throttle_key
                        ] = callback_id
                        callback_token_registered = True
                    result = await self._messenger.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=normalized_text,
                        reply_markup=reply_markup,
                        force=force_send,
                        request_category=request_category,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    if hasattr(result, "message_id"):
                        new_message_id = result.message_id  # type: ignore[assignment]
                    elif isinstance(result, int):
                        new_message_id = result
                    else:
                        new_message_id = message_id
            except (BadRequest, Forbidden, RetryAfter, TelegramError) as exc:
                if callback_token_registered and callback_throttle_key is not None:
                    self._last_callback_edit.pop(callback_throttle_key, None)
                logger.error(
                    "Failed to update message",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return message_id

            if new_message_id is None:
                return message_id

            normalized_new_message = self._safe_int(new_message_id)
            new_message_key = (normalized_chat, normalized_new_message)
            await self._set_payload_hash(new_message_key, payload_hash)
            await self._set_last_text_hash(normalized_new_message, message_text_hash)
            await self._unmark_message_deleted(normalized_new_message)
            if turn_cache_key is not None:
                await self._set_turn_cache_hash(new_message_key, payload_hash)
            new_stage_key = (
                normalized_chat,
                normalized_new_message,
                callback_stage_name,
            )
            await self._set_stage_payload_hash(new_stage_key, payload_hash)
            if callback_id is not None:
                new_callback_token_key = (
                    normalized_chat,
                    normalized_new_message,
                    callback_stage_name,
                    callback_user_id if callback_user_id is not None else 0,
                )
                self._store_callback_update_token(
                    new_callback_token_key,
                    callback_id,
                )
                self._last_callback_edit[
                    (normalized_new_message, callback_stage_name)
                ] = callback_id
            if (
                anchor_refresh_plan is not None
                and anchor_refresh_plan.get("record") is not None
                and not anchor_refresh_plan.get("keyboard_changed", True)
                and normalized_existing_message is not None
                and normalized_new_message == normalized_existing_message
            ):
                record = anchor_refresh_plan["record"]
                player_id = getattr(record, "player_id", None)
                if player_id is not None:
                    stripped_text = (
                        anchor_refresh_plan.get("stripped_text") or normalized_text
                    )
                    markup_signature = (
                        anchor_refresh_plan.get("markup_signature") or ""
                    )
                    refresh_toggle = anchor_refresh_plan.get("refresh_toggle")
                    if refresh_toggle is None:
                        refresh_toggle = getattr(record, "refresh_toggle", "") or ""
                    turn_light = anchor_refresh_plan.get("turn_light") or self._detect_anchor_turn_light(
                        stripped_text
                    )
                    payload_signature = (
                        getattr(record, "payload_signature", "") or ""
                    )
                    self._anchor_registry.update_role(
                        chat_id,
                        player_id,
                        current_text=stripped_text,
                        markup_signature=markup_signature,
                        payload_signature=payload_signature,
                        turn_light=turn_light,
                        refresh_toggle=refresh_toggle,
                    )
            if (
                message_key is not None
                and new_message_key != message_key
            ):
                await self._pop_payload_hash(message_key)
                if turn_cache_key is not None:
                    await self._pop_turn_cache_hash(message_key)
                await self._clear_callback_tokens_for_message(
                    message_key[0],
                    message_key[1],
                )
            return new_message_id

    @staticmethod
    def _render_cards(cards: Sequence[Card]) -> str:
        if not cards:
            return "—"
        return _CARD_SPACER.join(str(card) for card in cards)

    @classmethod
    def _format_card_line(cls, label: str, cards: Sequence[Card]) -> str:
        return f"{label}: {cls._render_cards(cards)}"

    @classmethod
    def _build_anchor_text(
        cls,
        *,
        display_name: str,
        mention_markdown: Optional[Mention],
        seat_number: int | str,
        role_label: str,
    ) -> str:
        hidden_mention = cls._build_hidden_mention(mention_markdown)
        lines = [
            f"🎮 {display_name}{hidden_mention}",
            f"🪑 صندلی: {seat_number}",
            f"🎖️ نقش: {role_label}",
        ]
        # کارت‌های بازیکن در کیبورد پاسخ اختصاصی نمایش داده می‌شوند.
        return "\n".join(lines)

    async def send_player_role_anchors(
        self,
        *,
        game: Game,
        chat_id: ChatId,
    ) -> None:
        """Send one anchor message per player with their current role."""

        if chat_id is None:
            return

        raw_stage = getattr(game, "state", GameState.INITIAL)
        stage = self._record_chat_stage(chat_id, raw_stage) or GameState.INITIAL
        stage_name = stage.name

        if self._is_private_chat(chat_id):
            return

        players: Sequence[Optional[Player]] = getattr(game, "players", [])
        ordered_players = sorted(
            (player for player in players if player is not None),
            key=lambda p: p.seat_index if p.seat_index is not None else 999,
        )

        community_cards = self._extract_community_cards(game)

        role_lock = await self._get_anchor_lock(chat_id, "role_anchor_lock")

        async with role_lock:
            state = self._anchor_registry.get_chat_state(chat_id)

            for player in ordered_players:
                seat_index = player.seat_index if player.seat_index is not None else -1
                seat_number: int | str = seat_index + 1 if seat_index >= 0 else "?"

                role_label = (
                    getattr(player, "role_label", None)
                    or self._describe_player_role(game, player)
                )

                display_name = str(
                    getattr(player, "display_name", None)
                    or getattr(player, "full_name", None)
                    or getattr(player, "username", None)
                    or getattr(player, "mention_markdown", "بازیکن")
                )

                hole_cards = self._extract_player_hole_cards(player)
                keyboard = self._compose_anchor_keyboard(
                    stage_name=stage_name,
                    hole_cards=hole_cards,
                    community_cards=community_cards,
                )

                logger.info(
                    "Dispatching anchor keyboard",
                    extra={
                        "chat_id": chat_id,
                        "player_id": getattr(player, "user_id", None),
                        "stage": stage_name,
                        "hole_cards": hole_cards,
                        "community_cards": community_cards,
                    },
                )

                text = self._build_anchor_text(
                    display_name=display_name,
                    mention_markdown=getattr(player, "mention_markdown", None),
                    seat_number=seat_number,
                    role_label=role_label,
                )

                payload_signature = self._reply_keyboard_signature(
                    text=text,
                    reply_markup=keyboard,
                    stage_name=stage_name,
                    community_cards=community_cards,
                    hole_cards=hole_cards,
                    turn_indicator="",
                )
                markup_signature = self._serialize_markup(keyboard) or ""
                player_id = getattr(player, "user_id", None)
                record = state.role_anchors.get(player_id)
                normalized_anchor: Optional[int] = None
                if record is not None:
                    record.seat_index = seat_index
                    normalized_anchor = record.message_id
                    if normalized_anchor is not None:
                        deletion_reason = await self._consume_role_anchor_deletion(
                            normalized_anchor
                        )
                        if deletion_reason is not None:
                            normalized_anchor = None
                        elif await self._is_message_deleted(normalized_anchor):
                            await self._unmark_message_deleted(normalized_anchor)

                if normalized_anchor is None:
                    try:
                        message_id = await self.send_message_return_id(
                            chat_id=chat_id,
                            text=text,
                            reply_markup=keyboard,
                            request_category=RequestCategory.ANCHOR,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to send player anchor",
                            extra={
                                "chat_id": chat_id,
                                "player_id": player_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        continue

                    if message_id is None:
                        continue

                    try:
                        normalized_anchor = int(message_id)
                    except (TypeError, ValueError):
                        continue

                    new_record = self._anchor_registry.register_role(
                        chat_id,
                        player_id=player_id,
                        seat_index=seat_index,
                        message_id=normalized_anchor,
                        base_text=text,
                        payload_signature=payload_signature,
                        markup_signature=markup_signature,
                    )
                    if new_record is not None:
                        new_record.seat_index = seat_index
                    await self._unmark_message_deleted(normalized_anchor)
                else:
                    updated_record = self._anchor_registry.update_role(
                        chat_id,
                        player_id,
                        base_text=text,
                        message_id=normalized_anchor,
                        current_text=text,
                        payload_signature=payload_signature,
                        markup_signature=markup_signature,
                        turn_light="",
                        refresh_toggle="",
                    )
                    if updated_record is not None:
                        updated_record.seat_index = seat_index
                    await self._unmark_message_deleted(normalized_anchor)

                if normalized_anchor is None:
                    continue

                player.anchor_message = (chat_id, normalized_anchor)
                player.anchor_role = role_label
                player.role_label = role_label
                player.anchor_keyboard_signature = payload_signature
                player.private_keyboard_message = None
                player.private_keyboard_signature = None

                await asyncio.sleep(0.075)

    @staticmethod
    def _describe_player_role(game: Game, player: Player) -> str:
        seat_index = player.seat_index if player.seat_index is not None else -1
        roles: List[str] = []
        if seat_index == getattr(game, "dealer_index", -1):
            roles.append("دیلر")
        if seat_index == getattr(game, "small_blind_index", -1):
            roles.append("بلایند کوچک")
        if seat_index == getattr(game, "big_blind_index", -1):
            roles.append("بلایند بزرگ")
        if not roles:
            roles.append("بازیکن")
        # Preserve insertion order while removing duplicates.
        return "، ".join(dict.fromkeys(roles))

    @staticmethod
    def _get_player_anchor_message_id(
        chat_id: ChatId, player: Player
    ) -> Optional[int]:
        anchor_meta = getattr(player, "anchor_message", None)
        if (
            isinstance(anchor_meta, tuple)
            and len(anchor_meta) >= 2
            and anchor_meta[0] == chat_id
        ):
            try:
                return int(anchor_meta[1])
            except (TypeError, ValueError):
                return None
        return None

    def _resolve_role_anchor_record(
        self, chat_id: ChatId, message_id: Optional[int]
    ) -> Optional[RoleAnchorRecord]:
        if message_id is None:
            return None
        normalized_message = self._safe_int(message_id)
        if normalized_message <= 0:
            return None
        try:
            return self._anchor_registry.find_role_anchor_by_message(
                chat_id, normalized_message
            )
        except Exception:
            return None

    @staticmethod
    def _resolve_game_state(state: Any) -> Optional[GameState]:
        if isinstance(state, GameState):
            return state
        if state is None:
            return None
        if isinstance(state, str):
            candidate = state.strip()
            if not candidate:
                return None
            normalized = candidate.upper().replace("-", "_").replace(" ", "_")
            try:
                return GameState[normalized]
            except KeyError:
                pass
        try:
            return GameState(state)
        except Exception:
            return None

    def _record_chat_stage(
        self, chat_id: ChatId, state: Any
    ) -> Optional[GameState]:
        resolved = self._resolve_game_state(state)
        self._anchor_registry.set_stage(chat_id, resolved)
        if resolved is not None and resolved != GameState.INITIAL:
            self._clear_countdown_transition_pending(chat_id)
        return resolved

    def _log_anchor_preservation_skip(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[int],
        record: Optional[RoleAnchorRecord],
        extra_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if message_id is None:
            return
        normalized_message = self._safe_int(message_id)
        extra: Dict[str, Any] = {
            "chat_id": chat_id,
            "player_id": getattr(record, "player_id", None),
            "seat_index": getattr(record, "seat_index", None),
        }
        if extra_details:
            extra.update(extra_details)
        logger.info(
            "[AnchorPersistence] Skipped deletion for role anchor message_id=%s mid-hand",
            normalized_message,
            extra=extra,
        )

    def _should_block_anchor_deletion(
        self,
        *,
        chat_id: ChatId,
        message_id: Optional[int],
        allow_anchor_deletion: bool = False,
        game: Optional[Game] = None,
        reason: Optional[str] = None,
    ) -> bool:
        record = self._resolve_role_anchor_record(chat_id, message_id)
        if record is None:
            return False

        normalized_message = self._safe_int(message_id)

        resolved_stage = None
        if game is not None:
            resolved_stage = self._resolve_game_state(getattr(game, "state", None))
        if resolved_stage is None:
            stage = self._anchor_registry.get_stage(chat_id)
            resolved_stage = self._resolve_game_state(stage)

        stage_label = getattr(resolved_stage, "name", None)
        normalized_reason = (reason or "").strip() or None

        if self._is_anchor_deletion_authorized(
            stage=resolved_stage,
            allow_anchor_deletion=allow_anchor_deletion,
            reason=normalized_reason,
        ):
            return False

        details: Dict[str, Any] = {"stage": stage_label}
        if normalized_reason:
            details["reason"] = normalized_reason
        else:
            details["reason"] = "guarded"

        self._log_anchor_preservation_skip(
            chat_id=chat_id,
            message_id=normalized_message,
            record=record,
            extra_details=details,
        )

        logger.info(
            "[AnchorPersistence] Prevented deletion of role anchor message_id=%s at stage=%s",
            normalized_message,
            stage_label,
            extra={
                "chat_id": chat_id,
                "player_id": getattr(record, "player_id", None),
                "reason": normalized_reason or "guarded",
            },
        )

        return True

    def _is_anchor_deletion_authorized(
        self,
        *,
        stage: Optional[GameState],
        allow_anchor_deletion: bool,
        reason: Optional[str],
    ) -> bool:
        if not allow_anchor_deletion:
            return False

        normalized_reason = (reason or "").strip().lower()

        if normalized_reason == "player_leave":
            return True

        if normalized_reason == "hand_end":
            allowed_states: Set[GameState] = {GameState.INITIAL}
            showdown_state = getattr(GameState, "ROUND_SHOWDOWN", None)
            if isinstance(showdown_state, GameState):
                allowed_states.add(showdown_state)
            finished_state = getattr(GameState, "FINISHED", None)
            if isinstance(finished_state, GameState):
                allowed_states.add(finished_state)
            return stage in allowed_states

        if normalized_reason in {"anchor_refresh", "anchor_resend"}:
            return True

        return False

    async def sync_player_private_keyboards(
        self,
        game: Game,
        include_inactive: bool = False,
        stage_name: Optional[str] = None,
        community_cards: Optional[Sequence[str]] = None,
        players: Optional[Sequence[Player]] = None,
    ) -> None:
        stage = getattr(game, "state", GameState.INITIAL)
        chat_id_for_stage = getattr(game, "chat_id", None)
        resolved_stage: Optional[GameState]
        if chat_id_for_stage is not None:
            resolved_stage = self._record_chat_stage(chat_id_for_stage, stage)
        else:
            resolved_stage = self._resolve_game_state(stage)
        if stage_name is None:
            resolved = resolved_stage or GameState.INITIAL
            stage_name = resolved.name

        if community_cards is None:
            community_cards_source: Optional[Sequence[Card]] = getattr(
                game, "community_cards", None
            )
            if community_cards_source is None:
                community_cards_source = getattr(game, "cards_table", [])
            try:
                community_cards = [str(card) for card in community_cards_source or []]
            except Exception:
                community_cards = []

        player_pool: Sequence[Optional[Player]]
        if players is not None:
            player_pool = players
        else:
            player_pool = getattr(game, "players", [])

        for player in player_pool:
            if player is None:
                continue
            if not include_inactive:
                is_active = getattr(player, "is_active", None)
                if callable(is_active):
                    try:
                        if not player.is_active():
                            continue
                    except Exception:
                        continue
                elif getattr(player, "state", None) not in (
                    PlayerState.ACTIVE,
                    PlayerState.ALL_IN,
                ):
                    continue

            await self._send_player_private_keyboard(
                game=game,
                player=player,
                stage_name=stage_name,
                community_cards=community_cards,
            )

    async def _send_player_private_keyboard(
        self,
        *,
        game: Game,
        player: Player,
        stage_name: str,
        community_cards: Sequence[str],
        role_label: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> None:
        private_chat_id = getattr(player, "private_chat_id", None)
        if not private_chat_id:
            return

        hole_cards_source = getattr(player, "hole_cards", None)
        if hole_cards_source is None:
            hole_cards_source = getattr(player, "cards", [])
        try:
            hole_cards = [str(card) for card in hole_cards_source or []]
        except Exception:
            hole_cards = []

        keyboard = build_player_cards_keyboard(
            hole_cards=hole_cards,
            community_cards=community_cards,
            current_stage=stage_name or "",
        )

        seat_index = player.seat_index if player.seat_index is not None else -1
        seat_number = seat_index + 1 if seat_index >= 0 else "?"

        resolved_role_label = (
            role_label
            or getattr(player, "role_label", None)
            or getattr(player, "anchor_role", None)
            or self._describe_player_role(game, player)
        )

        resolved_display_name = display_name or (
            getattr(player, "display_name", None)
            or getattr(player, "full_name", None)
            or getattr(player, "username", None)
            or getattr(player, "mention_markdown", "بازیکن")
        )
        resolved_display_name = str(resolved_display_name)

        text = self._build_anchor_text(
            display_name=resolved_display_name,
            mention_markdown=getattr(player, "mention_markdown", None),
            seat_number=seat_number,
            role_label=resolved_role_label,
        )

        signature_payload = self._reply_keyboard_signature(
            text=text,
            reply_markup=keyboard,
            stage_name=stage_name,
            community_cards=community_cards,
            hole_cards=hole_cards,
            turn_indicator="",
        )

        previous_signature = getattr(player, "private_keyboard_signature", None)

        message_meta = getattr(player, "private_keyboard_message", None)
        message_id: Optional[int] = None
        if (
            isinstance(message_meta, tuple)
            and len(message_meta) >= 2
            and message_meta[0] == private_chat_id
        ):
            try:
                message_id = int(message_meta[1])
            except (TypeError, ValueError):
                message_id = None

        if message_id is None:
            try:
                new_message_id = await self.send_message_return_id(
                    chat_id=private_chat_id,
                    text=text,
                    reply_markup=keyboard,
                    request_category=RequestCategory.GENERAL,
                )
            except Exception as exc:
                logger.error(
                    "Failed to send private keyboard",
                    extra={
                        "chat_id": private_chat_id,
                        "player_id": getattr(player, "user_id", None),
                        "error_type": type(exc).__name__,
                    },
                )
                return

            if new_message_id is None:
                return

            normalized_message_id = self._safe_int(new_message_id)
            player.private_keyboard_message = (private_chat_id, normalized_message_id)
            player.private_keyboard_signature = signature_payload
            logger.info(
                "Private keyboard sent",
                extra={
                    "chat_id": private_chat_id,
                    "player_id": getattr(player, "user_id", None),
                    "message_id": normalized_message_id,
                },
            )
            return

        if previous_signature == signature_payload:
            logger.debug(
                "Skipping private keyboard refresh (unchanged)",
                extra={
                    "chat_id": private_chat_id,
                    "player_id": getattr(player, "user_id", None),
                    "message_id": message_id,
                },
            )
            return

        try:
            updated = await self.edit_message_reply_markup(
                chat_id=private_chat_id,
                message_id=message_id,
                reply_markup=keyboard,
            )
        except Exception as exc:
            logger.error(
                "Failed to update private keyboard",
                extra={
                    "chat_id": private_chat_id,
                    "player_id": getattr(player, "user_id", None),
                    "message_id": message_id,
                    "error_type": type(exc).__name__,
                },
            )
            updated = False

        if updated:
            player.private_keyboard_signature = signature_payload
            logger.info(
                "Private keyboard updated",
                extra={
                    "chat_id": private_chat_id,
                    "player_id": getattr(player, "user_id", None),
                    "message_id": message_id,
                },
            )
            return

        try:
            fallback_id = await self.send_message_return_id(
                chat_id=private_chat_id,
                text=text,
                reply_markup=keyboard,
                request_category=RequestCategory.GENERAL,
            )
        except Exception as exc:
            logger.error(
                "Failed to resend private keyboard",
                extra={
                    "chat_id": private_chat_id,
                    "player_id": getattr(player, "user_id", None),
                    "message_id": message_id,
                    "error_type": type(exc).__name__,
                },
            )
            return

        if fallback_id is None:
            return

        normalized_fallback_id = self._safe_int(fallback_id)
        player.private_keyboard_message = (private_chat_id, normalized_fallback_id)
        player.private_keyboard_signature = signature_payload
        logger.info(
            "Private keyboard resent",
            extra={
                "chat_id": private_chat_id,
                "player_id": getattr(player, "user_id", None),
                "message_id": normalized_fallback_id,
            },
        )

    async def _refresh_role_anchor_in_place(
        self,
        *,
        chat_id: ChatId,
        player: Player,
        message_id: MessageId,
        base_text: str,
        display_text: str,
        keyboard: ReplyKeyboardMarkup,
        payload_signature: str,
        markup_signature: str,
        turn_light: str,
        stage_name: Optional[str],
    ) -> bool:
        player_id = getattr(player, "user_id", None)
        normalized_chat = self._safe_int(chat_id)
        normalized_message = self._safe_int(message_id)
        record = self._anchor_registry.get_role(chat_id, player_id)

        refreshed_text, base_plain, refresh_toggle = self._force_anchor_text_refresh(
            display_text,
            last_toggle=record.refresh_toggle if record else None,
        )
        context = self._build_context(
            "refresh_role_anchor",
            chat_id=chat_id,
            message_id=message_id,
            player_id=player_id,
        )
        normalized_text = self._validator.normalize_text(
            refreshed_text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.debug(
                "Skipping role anchor refresh due to invalid payload",
                extra={"chat_id": chat_id, "message_id": message_id},
            )
            return False
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return False

        stage_token = self._normalize_stage_name(stage_name)
        payload_hash = self._payload_hash(normalized_text, keyboard)
        message_text_hash = hashlib.md5(normalized_text.encode("utf-8")).hexdigest()
        message_key = (normalized_chat, normalized_message)

        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            if await self._is_message_deleted(normalized_message):
                logger.debug(
                    "Role anchor refresh skipped because message is marked deleted",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "player_id": player_id,
                    },
                )
                return False
            try:
                result = await self._messenger.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=normalized_text,
                    reply_markup=keyboard,
                    force=True,
                    request_category=RequestCategory.ANCHOR,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True,
                )
            except (BadRequest, Forbidden, RetryAfter, TelegramError) as exc:
                logger.debug(
                    "Role anchor refresh failed",
                    extra={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "player_id": player_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return False

            resolved_id = self._resolve_message_id(result)
            normalized_new_message = (
                self._safe_int(resolved_id)
                if resolved_id is not None
                else normalized_message
            )
            new_message_key = (normalized_chat, normalized_new_message)

            await self._set_payload_hash(new_message_key, payload_hash)
            await self._set_last_text_hash(normalized_new_message, message_text_hash)
            await self._unmark_message_deleted(normalized_new_message)
            new_stage_key = (
                normalized_chat,
                normalized_new_message,
                stage_token,
            )
            await self._set_stage_payload_hash(new_stage_key, payload_hash)

            if normalized_new_message != normalized_message:
                await self._pop_payload_hash(message_key)
                await self._clear_callback_tokens_for_message(
                    message_key[0],
                    message_key[1],
                )
                normalized_message = normalized_new_message
                message_key = new_message_key

        stripped_display_text, _ = self._strip_invisible_suffix(base_plain)
        self._anchor_registry.update_role(
            chat_id,
            player_id,
            base_text=base_text,
            message_id=normalized_message,
            current_text=stripped_display_text,
            payload_signature=payload_signature,
            markup_signature=markup_signature,
            turn_light=turn_light,
            refresh_toggle=refresh_toggle,
        )
        player.anchor_message = (chat_id, normalized_message)
        player.anchor_keyboard_signature = payload_signature
        self._anchor_registry.increment_edit(chat_id)

        logger.debug(
            "Role anchor refreshed in place",
            extra={
                "chat_id": chat_id,
                "player_id": player_id,
                "message_id": normalized_message,
                "stage": stage_name,
                "toggle": refresh_toggle,
            },
        )

        return True

    async def _send_new_role_anchor(
        self,
        *,
        chat_id: ChatId,
        player: Player,
        base_text: str,
        display_text: str,
        keyboard: ReplyKeyboardMarkup,
        payload_signature: str,
        markup_signature: str,
        turn_light: str,
        previous_message_id: Optional[int],
        fallback_reason: str,
    ) -> Optional[int]:
        player_id = getattr(player, "user_id", None)
        stage_value = self._anchor_registry.get_stage(chat_id)
        resolved_stage = self._resolve_game_state(stage_value)
        stage_name = getattr(resolved_stage, "name", None)
        normalized_previous: Optional[int] = None
        if previous_message_id is not None:
            normalized_previous = self._safe_int(previous_message_id)

        normalized_chat = self._safe_int(chat_id)
        countdown_task_active = False
        if resolved_stage == GameState.INITIAL:
            countdown_task_active = await self._is_countdown_active(chat_id)
        countdown_transition_pending = self._is_countdown_transition_pending(
            normalized_chat
        )
        countdown_active = (
            countdown_task_active or countdown_transition_pending
            if resolved_stage == GameState.INITIAL
            else countdown_task_active
        )
        countdown_guard_source = self._describe_countdown_guard_source(
            task_active=countdown_task_active,
            transition_pending=countdown_transition_pending,
        )

        countdown_guard_active = (
            resolved_stage in self._ACTIVE_ANCHOR_STATES
            or (
                resolved_stage == GameState.INITIAL and countdown_active
            )
        )
        if countdown_guard_active:
            countdown_log_extra = {
                "chat_id": chat_id,
                "message_id": normalized_previous,
                "player_id": player_id,
                "stage": stage_name,
                "countdown_active": countdown_active,
                "countdown_guard_task_active": countdown_task_active,
                "countdown_guard_transition_pending": countdown_transition_pending,
                "countdown_guard_source": countdown_guard_source,
            }
            if previous_message_id is not None:
                refresh_success = False
                try:
                    refresh_success = await self._refresh_role_anchor_in_place(
                        chat_id=chat_id,
                        player=player,
                        message_id=previous_message_id,
                        base_text=base_text,
                        display_text=display_text,
                        keyboard=keyboard,
                        payload_signature=payload_signature,
                        markup_signature=markup_signature,
                        turn_light=turn_light,
                        stage_name=stage_name,
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to refresh existing role anchor during countdown",
                        extra={
                            **countdown_log_extra,
                            "error_type": type(exc).__name__,
                            "reason": fallback_reason,
                        },
                    )
                if refresh_success:
                    logger.info(
                        "[AnchorPersistence] Skipped deletion for role anchor during INITIAL countdown",
                        extra=countdown_log_extra,
                    )
                    return normalized_previous

                logger.info(
                    "[AnchorPersistence] Countdown refresh failed; proceeding with fallback",
                    extra={
                        **countdown_log_extra,
                        "reason": fallback_reason,
                    },
                )

                if normalized_previous is not None:
                    message_exists: Optional[bool] = None
                    try:
                        message_exists = await self._probe_message_existence(
                            chat_id, normalized_previous
                        )
                    except Exception as probe_exc:
                        logger.debug(
                            "Failed to probe role anchor existence after refresh failure",
                            extra={
                                **countdown_log_extra,
                                "error_type": type(probe_exc).__name__,
                                "reason": fallback_reason,
                            },
                        )
                    if message_exists is False:
                        await self._mark_message_deleted(normalized_previous)
            else:
                logger.info(
                    "[AnchorPersistence] Skipped deletion for role anchor during INITIAL countdown",
                    extra=countdown_log_extra,
                )
                return None

        try:
            if previous_message_id:
                await self.delete_message(
                    chat_id=chat_id,
                    message_id=previous_message_id,
                )
                deletion_reason = await self._peek_role_anchor_deletion(
                    self._safe_int(previous_message_id)
                )
                message_exists = await self._probe_message_existence(
                    chat_id, self._safe_int(previous_message_id)
                )
                if (
                    deletion_reason is None
                    and message_exists is not False
                    and not await self._is_message_deleted(
                        self._safe_int(previous_message_id)
                    )
                ):
                    logger.info(
                        "[AnchorPersistence] Fallback skipped because anchor deletion was prevented",
                        extra={
                            "chat_id": chat_id,
                            "player_id": player_id,
                            "message_id": previous_message_id,
                            "reason": fallback_reason,
                        },
                    )
                    return None
        except Exception:
            logger.debug(
                "Failed to delete stale role anchor",
                extra={
                    "chat_id": chat_id,
                    "player_id": player_id,
                    "message_id": previous_message_id,
                },
            )

        try:
            new_message_id = await self.send_message_return_id(
                chat_id=chat_id,
                text=display_text,
                reply_markup=keyboard,
                request_category=RequestCategory.ANCHOR,
            )
        except Exception as exc:
            logger.error(
                "Failed to create replacement role anchor",
                extra={
                    "chat_id": chat_id,
                    "player_id": player_id,
                    "error_type": type(exc).__name__,
                },
            )
            return None

        if new_message_id is None:
            return None

        normalized_id = self._safe_int(new_message_id)
        player.anchor_message = (chat_id, normalized_id)
        player.anchor_keyboard_signature = payload_signature
        await self._unmark_message_deleted(normalized_id)

        self._anchor_registry.update_role(
            chat_id,
            player_id,
            base_text=base_text,
            message_id=normalized_id,
            current_text=display_text,
            payload_signature=payload_signature,
            markup_signature=markup_signature,
            turn_light=turn_light,
            refresh_toggle="",
        )

        self._anchor_registry.increment_fallback(chat_id)

        logger.debug(
            "Anchor fallback-new-msg",
            extra={
                "chat_id": chat_id,
                "player_id": player_id,
                "message_id": normalized_id,
                "reason": fallback_reason,
            },
        )

        return normalized_id

    async def update_player_anchors_and_keyboards(self, game: Game) -> None:
        chat_id = getattr(game, "chat_id", None)
        if chat_id is None:
            logger.warning(
                "Cannot update anchors without chat id",
                extra={"game_id": getattr(game, "id", None)},
            )
            return

        raw_stage = getattr(game, "state", GameState.INITIAL)
        stage = self._record_chat_stage(chat_id, raw_stage) or GameState.INITIAL
        stage_name = stage.name

        community_cards = self._extract_community_cards(game)

        if self._is_private_chat(chat_id):
            logger.debug(
                "Skipping anchor refresh for private chat",
                extra={"chat_id": chat_id},
            )
            return

        current_player: Optional[Player] = None
        current_index = getattr(game, "current_player_index", None)
        if isinstance(current_index, int) and current_index >= 0:
            try:
                current_player = game.get_player_by_seat(current_index)
            except Exception:
                current_player = None

        role_lock = await self._get_anchor_lock(chat_id, "role_anchor_lock")

        async with role_lock:
            for player in list(getattr(game, "players", [])):
                if player is None:
                    continue
                is_active = getattr(player, "is_active", None)
                if callable(is_active):
                    try:
                        if not player.is_active():
                            continue
                    except Exception:
                        continue
                elif getattr(player, "state", None) not in (
                    PlayerState.ACTIVE,
                    PlayerState.ALL_IN,
                ):
                    continue

                anchor_id = self._get_player_anchor_message_id(chat_id, player)
                if not anchor_id:
                    continue

                player_id = getattr(player, "user_id", None)
                record = self._anchor_registry.get_role(chat_id, player_id)
                if record is None:
                    logger.debug(
                        "Anchor record missing for player %s in chat %s", player_id, chat_id
                    )
                    continue

                hole_cards = self._extract_player_hole_cards(player)

                keyboard = self._compose_anchor_keyboard(
                    stage_name=stage_name,
                    hole_cards=hole_cards,
                    community_cards=community_cards,
                )

                seat_index = player.seat_index if player.seat_index is not None else -1
                seat_number = seat_index + 1 if seat_index >= 0 else "?"
                role_label = getattr(player, "role_label", None) or getattr(
                    player, "anchor_role", "بازیکن"
                )
                display_name = (
                    getattr(player, "display_name", None)
                    or getattr(player, "full_name", None)
                    or getattr(player, "username", None)
                    or player.mention_markdown
                )
                display_name = str(display_name)

                base_text = self._build_anchor_text(
                    display_name=display_name,
                    mention_markdown=getattr(player, "mention_markdown", None),
                    seat_number=seat_number,
                    role_label=role_label,
                )

                previous_text = record.current_text
                previous_markup = record.markup_signature
                previous_payload = record.payload_signature
                previous_light = record.turn_light or ""

                next_light = ""
                indicator_line = ""
                if current_player is player:
                    next_light = "🟢" if previous_light != "🟢" else "🔴"
                    indicator_line = f"\n\n{next_light} نوبت این بازیکن است."

                display_text = base_text + indicator_line
                markup_signature = self._serialize_markup(keyboard) or ""
                payload_signature = self._reply_keyboard_signature(
                    text=display_text,
                    reply_markup=keyboard,
                    stage_name=stage_name,
                    community_cards=community_cards,
                    hole_cards=hole_cards,
                    turn_indicator=next_light,
                )

                text_changed = display_text != previous_text
                keyboard_changed = markup_signature != previous_markup
                payload_changed = payload_signature != previous_payload
                reason_label = "forced" if text_changed and not keyboard_changed else "normal"

                if not (text_changed or keyboard_changed or payload_changed):
                    logger.debug(
                        "Anchor update skipped (unchanged) for player=%s chat=%s",
                        player_id,
                        chat_id,
                    )
                    continue

                logger.debug(
                    "Anchor update: player=%s | seat=%s | role=%s | hole_cards=%s | "
                    "community_cards=%s | is_turn=%s | chat_id=%s | anchor_id=%s | "
                    "reason=%s",
                    display_name,
                    seat_number,
                    role_label,
                    hole_cards,
                    community_cards,
                    current_player is player,
                    chat_id,
                    anchor_id,
                    reason_label,
                )

                logger.info(
                    "Updating anchor",
                    extra={
                        "chat_id": chat_id,
                        "player_id": player_id,
                        "message_id": anchor_id,
                        "stage": stage_name,
                        "hole_cards": hole_cards,
                        "community_cards": community_cards,
                        "text_changed": text_changed,
                        "keyboard_changed": keyboard_changed,
                    },
                )

                await self._purge_pending_updates(chat_id, anchor_id)

                new_message_id: Optional[int] = None
                edit_success = False
                intended_display_text = display_text
                intended_payload_signature = payload_signature
                attempt_text = intended_display_text
                attempt_payload_signature = intended_payload_signature
                final_display_text = intended_display_text
                final_payload_signature = intended_payload_signature
                forced_refresh_attempted = False
                rate_retry_performed = False
                last_edit_error: Optional[str] = None
                applied_refresh_toggle = record.refresh_toggle if record else ""

                while True:
                    try:
                        result = await self._update_message(
                            chat_id=chat_id,
                            message_id=anchor_id,
                            text=attempt_text,
                            reply_markup=keyboard,
                            force_send=True,
                            request_category=RequestCategory.ANCHOR,
                        )
                    except Exception as exc:
                        logger.error(
                            "Unexpected error editing anchor text",
                            extra={
                                "chat_id": chat_id,
                                "player_id": player_id,
                                "message_id": anchor_id,
                                "error_type": type(exc).__name__,
                            },
                        )
                        break

                    resolved_id = self._resolve_message_id(result)
                    if resolved_id is not None:
                        new_message_id = resolved_id
                        edit_success = True
                        final_display_text = attempt_text
                        final_payload_signature = attempt_payload_signature
                        break

                    normalized_anchor = self._safe_int(anchor_id)
                    last_edit_error = None
                    if normalized_anchor is not None:
                        last_edit_error = await self._get_last_edit_error(
                            chat_id, normalized_anchor
                        )

                    error_text = (last_edit_error or "").lower()
                    if (
                        error_text
                        and (
                            "too many requests" in error_text
                            or "retry later" in error_text
                            or "flood control" in error_text
                        )
                        and not rate_retry_performed
                    ):
                        rate_retry_performed = True
                        await asyncio.sleep(0.35)
                        continue

                    if not forced_refresh_attempted:
                        forced_refresh_attempted = True
                        (
                            attempt_text,
                            _,
                            forced_toggle,
                        ) = self._force_anchor_text_refresh(
                            intended_display_text,
                            last_toggle=record.refresh_toggle if record else None,
                        )
                        applied_refresh_toggle = forced_toggle
                        diagnostics_hint = {
                            "chat_id": chat_id,
                            "player_id": player_id,
                            "message_id": anchor_id,
                            "invisible_char": forced_toggle,
                        }
                        logger.debug(
                            "Role anchor local forced refresh",
                            extra=diagnostics_hint,
                        )
                        attempt_payload_signature = self._reply_keyboard_signature(
                            text=attempt_text,
                            reply_markup=keyboard,
                            stage_name=stage_name,
                            community_cards=community_cards,
                            hole_cards=hole_cards,
                            turn_indicator=next_light,
                        )
                        continue

                    break

                if edit_success and new_message_id is None:
                    new_message_id = anchor_id

                if not edit_success and new_message_id is None:
                    should_fallback, fallback_reason, diagnostics = (
                        await self._should_create_anchor_fallback(
                            chat_id=chat_id,
                            message_id=anchor_id,
                            player_id=player_id,
                            last_error=last_edit_error,
                            forced_refresh=forced_refresh_attempted,
                            reply_markup=keyboard,
                            request_category=RequestCategory.ANCHOR,
                            current_text=intended_display_text,
                        )
                    )
                    diagnostics.update(
                        {
                            "stage": stage_name,
                            "hole_cards": hole_cards,
                            "community_cards": community_cards,
                            "text_changed": text_changed,
                            "keyboard_changed": keyboard_changed,
                            "payload_changed": payload_changed,
                        }
                    )
                    if should_fallback:
                        logger.warning(
                            "Anchor fallback triggered",
                            extra=diagnostics,
                        )
                        new_message_id = await self._send_new_role_anchor(
                            chat_id=chat_id,
                            player=player,
                            base_text=base_text,
                            display_text=intended_display_text,
                            keyboard=keyboard,
                            payload_signature=intended_payload_signature,
                            markup_signature=markup_signature,
                            turn_light=next_light,
                            previous_message_id=anchor_id,
                            fallback_reason=fallback_reason,
                        )
                        refreshed_record = self._anchor_registry.get_role(
                            chat_id, player_id
                        )
                        if refreshed_record is not None:
                            applied_refresh_toggle = (
                                refreshed_record.refresh_toggle
                                or applied_refresh_toggle
                            )
                    else:
                        logger.info(
                            "Fallback prevented – message still valid, retried edit successfully",
                            extra=diagnostics,
                        )
                elif edit_success and (forced_refresh_attempted or rate_retry_performed):
                    logger.info(
                        "Fallback prevented – message still valid, retried edit successfully",
                        extra={
                            "chat_id": chat_id,
                            "player_id": player_id,
                            "message_id": anchor_id,
                            "forced_refresh": forced_refresh_attempted,
                            "rate_retry": rate_retry_performed,
                            "stage": stage_name,
                        },
                    )

                if new_message_id is not None:
                    applied_text = (
                        final_display_text if edit_success else intended_display_text
                    )
                    applied_signature = (
                        final_payload_signature
                        if edit_success
                        else intended_payload_signature
                    )
                    stripped_applied_text, _ = self._strip_invisible_suffix(applied_text)
                    self._anchor_registry.update_role(
                        chat_id,
                        player_id,
                        base_text=base_text,
                        message_id=new_message_id,
                        current_text=stripped_applied_text,
                        payload_signature=applied_signature,
                        markup_signature=markup_signature,
                        turn_light=next_light,
                        refresh_toggle=applied_refresh_toggle,
                    )
                    player.anchor_message = (chat_id, new_message_id)
                    player.anchor_keyboard_signature = applied_signature
                    player.anchor_role = role_label
                    player.role_label = role_label
                    if edit_success:
                        self._anchor_registry.increment_edit(chat_id)

                await asyncio.sleep(0.05)

    async def clear_all_player_anchors(self, game: Game) -> None:
        chat_id = getattr(game, "chat_id", None)
        if chat_id is None or self._is_private_chat(chat_id):
            return

        raw_stage = getattr(game, "state", GameState.INITIAL)
        stage = self._record_chat_stage(chat_id, raw_stage)
        stage_name = getattr(stage, "name", None)
        if stage in self._ACTIVE_ANCHOR_STATES:
            logger.info(
                "[AnchorPersistence] Skipped anchor cleanup during active stage",
                extra={"chat_id": chat_id, "stage": stage_name},
            )
            return

        message_ids_to_delete = getattr(game, "message_ids_to_delete", [])
        state = self._anchor_registry.get_chat_state(chat_id)
        self._anchor_registry.set_turn_anchor(chat_id, message_id=None, payload_hash="")

        active_players: List[Player] = [
            player for player in getattr(game, "players", []) if player is not None
        ]
        active_ids: Set[int] = set()
        for player in active_players:
            normalized_id = self._safe_int(getattr(player, "user_id", None))
            active_ids.add(normalized_id)

        # Remove anchors for players who are no longer seated.
        for player_id, record in list(state.role_anchors.items()):
            if self._safe_int(player_id) in active_ids:
                continue
            message_id = getattr(record, "message_id", None)
            if message_id:
                try:
                    await self._purge_pending_updates(chat_id, message_id)
                    await self.delete_message(
                        chat_id=chat_id,
                        message_id=message_id,
                        allow_anchor_deletion=True,
                        anchor_reason="player_leave",
                        game=game,
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to delete player anchor",
                        extra={
                            "chat_id": chat_id,
                            "player_id": player_id,
                            "message_id": message_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                try:
                    message_ids_to_delete.remove(message_id)
                except (ValueError, AttributeError):
                    pass
            state.role_anchors.pop(player_id, None)

        # Preserve anchors for active players and ensure their messages stay tracked.
        for player in active_players:
            anchor_id = self._get_player_anchor_message_id(chat_id, player)
            if not anchor_id:
                continue
            await self._unmark_message_deleted(anchor_id)
            try:
                message_ids_to_delete.remove(anchor_id)
            except (ValueError, AttributeError):
                pass
            record = state.role_anchors.get(getattr(player, "user_id", None))
            if record is not None:
                record.turn_light = ""
                record.refresh_toggle = ""

        if active_players:
            await self.update_player_anchors_and_keyboards(game)

    async def announce_player_seats(
        self,
        *,
        chat_id: ChatId,
        players: Sequence[Player],
        dealer_index: int,
        message_id: Optional[MessageId] = None,
    ) -> Optional[MessageId]:
        """Send or update the seat map for the current hand."""

        sorted_players = sorted(
            players,
            key=lambda p: (p.seat_index if p.seat_index is not None else 0),
        )

        lines: List[str] = ["👥 *بازیکنان حاضر در میز*", ""]
        for player in sorted_players:
            seat_no = (player.seat_index or 0) + 1
            dealer_suffix = " — دیلر" if player.seat_index == dealer_index else ""
            lines.append(
                f"`{seat_no:>2}` │ {player.mention_markdown}{dealer_suffix}"
            )

        if not sorted_players:
            lines.append("هنوز بازیکنی در میز حضور ندارد.")

        text = "\n".join(lines)
        return await self.schedule_message_update(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=None,
            parse_mode=ParseMode.MARKDOWN,
            disable_notification=True,
            request_category=RequestCategory.STAGE,
        )

    async def notify_admin(self, log_data: Dict[str, Any]) -> None:
        if not self._admin_chat_id:
            return
        context = self._build_context("notify_admin", chat_id=self._admin_chat_id)
        text = self._validator.normalize_text(
            json.dumps(log_data, ensure_ascii=False),
            parse_mode=None,
            context=context,
        )
        if text is None:
            logger.warning(
                "Dropping admin notification due to invalid payload",
                extra={"context": context},
            )
            return
        try:
            await self._messenger.send_message(
                chat_id=self._admin_chat_id,
                text=text,
                request_category=RequestCategory.GENERAL,
            )
        except Exception as e:
            logger.error(
                "Failed to notify admin",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": self._admin_chat_id,
                },
            )

    async def send_message_return_id(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        """Sends a message and returns its ID, or None if not applicable."""
        context = self._build_context("send_message_return_id", chat_id=chat_id)
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message_return_id due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, None)
            return None
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                message_id = message.message_id
                await self._unmark_message_deleted(self._safe_int(message_id))
                return message_id
        except Exception as e:
            logger.error(
                "Error sending message and returning ID",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"text": text},
                },
            )
        return None

    async def last_message_edit_at(
        self, chat_id: ChatId, message_id: MessageId
    ) -> Optional[datetime.datetime]:
        """Return the timestamp of the last successful edit for ``message_id``."""

        accessor = getattr(self._messenger, "last_edit_timestamp", None)
        if accessor is None:
            return None
        try:
            result = accessor(chat_id, message_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return None
        if isinstance(result, datetime.datetime):
            return result
        return None


    async def send_message(
        self,
        chat_id: ChatId,
        text: str,
        reply_markup: ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
        parse_mode: str = ParseMode.MARKDOWN,  # <--- پارامتر جدید اضافه شد
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        context = self._build_context("send_message", chat_id=chat_id)
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, None)
            return None
        try:
            message = await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
                disable_notification=True,
                disable_web_page_preview=True,
            )
            if isinstance(message, Message):
                message_id = message.message_id
                await self._unmark_message_deleted(self._safe_int(message_id))
                return message_id
        except Exception as e:
            logger.error(
                "Error sending message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "request_params": {"text": text},
                },
            )
        return None

    async def send_photo(self, chat_id: ChatId) -> None:
        try:
            with open("./assets/poker_hand.jpg", "rb") as f:
                await self._messenger.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    request_category=RequestCategory.MEDIA,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=True,
                )
        except Exception as e:
            logger.error(
                "Error sending photo",
                extra={"error_type": type(e).__name__, "chat_id": chat_id},
            )

    async def send_dice_reply(
        self, chat_id: ChatId, message_id: MessageId, emoji='🎲'
    ) -> Optional[Message]:
        context = self._build_context(
            "send_dice_reply", chat_id=chat_id, message_id=message_id
        )
        try:
            return await self._bot.send_dice(
                reply_to_message_id=message_id,
                chat_id=chat_id,
                disable_notification=True,
                emoji=emoji,
            )
        except Exception as e:
            logger.error(
                "Error sending dice reply",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"emoji": emoji},
                },
            )
            return None

    @property
    def request_metrics(self) -> RequestMetrics:
        """Expose the shared request metrics helper for orchestration code."""

        return self._request_metrics

    async def send_message_reply(
        self, chat_id: ChatId, message_id: MessageId, text: str
    ) -> None:
        context = self._build_context(
            "send_message_reply", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping send_message_reply due to invalid text",
                extra={"context": context},
            )
            return
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return
        try:
            await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_text,
                reply_markup=None,
                request_category=RequestCategory.GENERAL,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                reply_to_message_id=message_id,
            )
        except Exception as e:
            logger.error(
                "Error sending message reply",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "request_params": {"text": text},
                },
            )

    async def edit_message_text(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        text: str,
        reply_markup: ReplyKeyboardMarkup = None,
        parse_mode: str = ParseMode.MARKDOWN,
        disable_web_page_preview: bool = False,
        request_category: RequestCategory = RequestCategory.GENERAL,
    ) -> Optional[MessageId]:
        """Edit a message using the central ``MessagingService``.

        Args:
            request_category: Request category used for metrics and rate limiting.
        """
        context = self._build_context(
            "edit_message_text", chat_id=chat_id, message_id=message_id
        )
        normalized_text = self._validator.normalize_text(
            text,
            parse_mode=parse_mode,
            context=context,
        )
        if normalized_text is None:
            logger.warning(
                "Skipping edit_message_text due to invalid text",
                extra={"context": context},
            )
            return None
        if not self._has_visible_text(normalized_text):
            self._log_skip_empty(chat_id, message_id)
            return None
        try:
            result = await self._messenger.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=normalized_text,
                reply_markup=reply_markup,
                request_category=request_category,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
            return result
        except Exception as exc:
            logger.error(
                "Failed to edit message text",
                extra={
                    "error_type": type(exc).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "context": context,
                },
            )
            return None

    async def delete_message(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        *,
        allow_anchor_deletion: bool = False,
        anchor_reason: Optional[str] = None,
        game: Optional[Game] = None,
    ) -> None:
        """Delete a message while keeping the cache in sync."""
        normalized_message = self._safe_int(message_id)
        normalized_chat = self._safe_int(chat_id)

        resolved_stage = None
        if game is not None:
            resolved_stage = self._resolve_game_state(getattr(game, "state", None))
        if resolved_stage is None:
            resolved_stage = self._resolve_game_state(
                self._anchor_registry.get_stage(chat_id)
            )
        stage_name = getattr(resolved_stage, "name", None)

        countdown_task_active = False
        if resolved_stage == GameState.INITIAL:
            countdown_task_active = await self._is_countdown_active(chat_id)
        countdown_transition_pending = self._is_countdown_transition_pending(
            normalized_chat
        )
        countdown_active = (
            countdown_task_active or countdown_transition_pending
            if resolved_stage == GameState.INITIAL
            else countdown_task_active
        )
        countdown_guard_source = self._describe_countdown_guard_source(
            task_active=countdown_task_active,
            transition_pending=countdown_transition_pending,
        )

        anchor_record = self._resolve_role_anchor_record(chat_id, normalized_message)
        normalized_reason = (anchor_reason or "").strip().lower()

        if (
            anchor_record is not None
            and (
                resolved_stage in self._ACTIVE_ANCHOR_STATES
                or (resolved_stage == GameState.INITIAL and countdown_active)
            )
        ):
            self._log_anchor_preservation_skip(
                chat_id=chat_id,
                message_id=normalized_message,
                record=anchor_record,
                extra_details={
                    "stage": stage_name,
                    "reason": normalized_reason or "guarded",
                    "countdown_active": countdown_active,
                    "countdown_guard_task_active": countdown_task_active,
                    "countdown_guard_transition_pending": countdown_transition_pending,
                    "countdown_guard_source": countdown_guard_source,
                },
            )
            logger.info(
                "[AnchorPersistence] Skipped deletion for role anchor during INITIAL countdown",
                extra={
                    "chat_id": chat_id,
                    "message_id": normalized_message,
                    "player_id": getattr(anchor_record, "player_id", None),
                    "stage": stage_name,
                    "countdown_active": countdown_active,
                    "countdown_guard_task_active": countdown_task_active,
                    "countdown_guard_transition_pending": countdown_transition_pending,
                    "countdown_guard_source": countdown_guard_source,
                },
            )
            return

        if self._should_block_anchor_deletion(
            chat_id=chat_id,
            message_id=normalized_message,
            allow_anchor_deletion=allow_anchor_deletion,
            game=game,
            reason=anchor_reason,
        ):
            return

        normalized_reason = normalized_reason or None

        lock = await self._acquire_message_lock(chat_id, message_id)
        async with lock:
            try:
                await self._messenger.delete_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    request_category=RequestCategory.DELETE,
                )
                await self._mark_message_deleted(normalized_message)
                if anchor_record is not None:
                    reason_label = normalized_reason or "unspecified"
                    await self._record_role_anchor_deletion(
                        normalized_message, reason_label
                    )
                    logger.info(
                        "[AnchorPersistence] Deleted anchor message_id=%s reason=%s",
                        normalized_message,
                        reason_label,
                        extra={
                            "chat_id": chat_id,
                            "player_id": getattr(anchor_record, "player_id", None),
                            "stage": stage_name,
                        },
                    )
            except (BadRequest, Forbidden) as e:
                error_message = getattr(e, "message", None) or str(e) or ""
                normalized_error = error_message.lower()
                ignorable_messages = (
                    "message to delete not found",
                    "message can't be deleted",
                    "message cant be deleted",
                )
                if any(msg in normalized_error for msg in ignorable_messages):
                    logger.debug(
                        "Ignoring delete_message error",
                        extra={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "error_type": type(e).__name__,
                            "error_message": error_message,
                        },
                    )
                    return
                logger.warning(
                    "Failed to delete message",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "error_message": error_message,
                    },
                )
                return
            except Exception as e:
                logger.error(
                    "Error deleting message (%s)",
                    type(e).__name__,
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
                return
            finally:
                await self._clear_callback_tokens_for_message(
                    normalized_chat, normalized_message
                )

    async def send_single_card(
        self,
        chat_id: ChatId,
        card: Card,
        disable_notification: bool = True,
    ) -> None:
        """Send a single card image to the specified chat."""
        try:
            im_card = self._desk_generator._load_card_image(card)
            bio = BytesIO()
            bio.name = "card.png"
            im_card.save(bio, "PNG")
            bio.seek(0)
            await self._messenger.send_photo(
                chat_id=chat_id,
                photo=bio,
                request_category=RequestCategory.MEDIA,
                disable_notification=disable_notification,
            )
        except Exception as e:
            logger.error(
                "Error sending single card",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "card": card,
                },
            )

    async def send_desk_cards_img(
        self,
        chat_id: ChatId,
        cards: Cards,
        caption: str = "",
        disable_notification: bool = True,
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup: Optional[ReplyKeyboardMarkup | ReplyKeyboardRemove] = None,
    ) -> Optional[Message]:
        """Sends desk cards image and returns the message object."""
        context = self._build_context("send_desk_cards_img", chat_id=chat_id)
        normalized_caption = self._validator.normalize_caption(
            caption,
            parse_mode=parse_mode,
            context=context,
        )
        if caption and normalized_caption is None:
            logger.warning(
                "Skipping send_desk_cards_img due to invalid caption",
                extra={"context": context},
            )
            return None
        try:
            desk_bytes = self._desk_generator.render_cached_png(cards)
            bio = BytesIO(desk_bytes)
            bio.name = "desk.png"
            bio.seek(0)
            message = await self._messenger.send_photo(
                chat_id=chat_id,
                photo=bio,
                caption=normalized_caption,
                request_category=RequestCategory.MEDIA,
                parse_mode=parse_mode,
                disable_notification=disable_notification,
                reply_markup=reply_markup,
            )
            if isinstance(message, Message):
                return message
        except Exception as e:
            logger.error(
                "Error sending desk cards image",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
        return None

    async def edit_desk_cards_img(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        cards: Cards,
        caption: str = "",
        parse_mode: str = ParseMode.MARKDOWN,
        reply_markup: Optional[ReplyKeyboardMarkup | ReplyKeyboardRemove] = None,
    ) -> Optional[Message]:
        """Edit an existing desk image or send a new one on failure.

        Returns the newly sent :class:`telegram.Message` when a new photo is
        sent instead of editing, otherwise ``None``.
        """
        try:
            desk_bytes = self._desk_generator.render_cached_png(cards)
            bio = BytesIO(desk_bytes)
            bio.name = "desk.png"
            bio.seek(0)
            context = self._build_context(
                "edit_desk_cards_img", chat_id=chat_id, message_id=message_id
            )
            normalized_caption = self._validator.normalize_caption(
                caption,
                parse_mode=parse_mode,
                context=context,
            )
            if caption and normalized_caption is None:
                logger.warning(
                    "Skipping edit_desk_cards_img due to invalid caption",
                    extra={"context": context},
                )
                return None
            media = InputMediaPhoto(
                media=bio, caption=normalized_caption, parse_mode=parse_mode
            )
            await self._bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=media,
                reply_markup=reply_markup,
            )
            return None
        except BadRequest:
            # If editing fails (e.g. original message no longer exists or is
            # invalid), fall back to sending a new photo so the board can still
            # be updated.
            try:
                msg = await self.send_desk_cards_img(
                    chat_id=chat_id,
                    cards=cards,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return msg
            except Exception as e:
                logger.error(
                    "Error sending new desk cards image after edit failure",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Exception as e:
            logger.error(
                "Error editing desk cards image",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        return None

    @staticmethod
    def define_check_call_action(game: Game, player: Player) -> PlayerAction:
        if player.round_rate >= game.max_round_rate:
            return PlayerAction.CHECK
        return PlayerAction.CALL

    async def update_turn_message(
        self,
        *,
        chat_id: ChatId,
        game: Game,
        player: Player,
        money: Money,
        message_id: Optional[MessageId] = None,
        recent_actions: Optional[List[str]] = None,
    ) -> TurnMessageUpdate:
        """Send or edit the persistent turn message for the active player."""

        turn_lock = await self._get_anchor_lock(chat_id, "turn_anchor_lock")

        async with turn_lock:
            call_amount = max(game.max_round_rate - player.round_rate, 0)
            call_action = self.define_check_call_action(game, player)
            call_text = (
                f"{call_action.value} ({call_amount}$)"
                if call_action == PlayerAction.CALL and call_amount > 0
                else call_action.value
            )

            seat_number = (player.seat_index or 0) + 1
            board_line = self._format_card_line("🃏 Board", game.cards_table)

            stage_labels = {
                GameState.ROUND_PRE_FLOP: "Pre-Flop",
                GameState.ROUND_FLOP: "Flop",
                GameState.ROUND_TURN: "Turn",
                GameState.ROUND_RIVER: "River",
            }
            stage_name = stage_labels.get(game.state, "Pre-Flop")

            info_lines = [
                f"🎯 **نوبت:** {player.mention_markdown} (صندلی {seat_number})",
                f"🎰 **مرحله بازی:** {stage_name}",
                "",
                board_line,
                f"💰 **پات فعلی:** `{game.pot}$`",
                f"💵 **موجودی شما:** `{money}$`",
                f"🎲 **بِت فعلی شما:** `{player.round_rate}$`",
                f"📈 **حداکثر شرط این دور:** `{game.max_round_rate}$`",
                "",
                "⬇️ **از دکمه‌های زیر برای اقدام استفاده کنید.**",
            ]

            history = list(
                (recent_actions if recent_actions is not None else game.last_actions)[-5:]
            )
            if history:
                info_lines.append("")
                info_lines.append("🎬 **اکشن‌های اخیر:**")
                info_lines.extend(f"• {action}" for action in history)

            text = "\n".join(info_lines)

            reply_markup = await self._build_turn_keyboard(call_text, call_action)

            turn_anchor = self._anchor_registry.get_turn_anchor(chat_id)
            anchor_message_id = message_id or turn_anchor.message_id

            new_message_id = await self.schedule_message_update(
                chat_id=chat_id,
                message_id=anchor_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                disable_notification=anchor_message_id is not None,
                request_category=RequestCategory.TURN,
            )

            resolved_message_id = self._resolve_message_id(new_message_id)
            final_message_id = resolved_message_id or anchor_message_id
            normalized_final_id: Optional[int] = None
            if final_message_id is not None:
                try:
                    normalized_final_id = int(final_message_id)
                except (TypeError, ValueError):
                    normalized_final_id = self._safe_int(final_message_id)

            payload_hash = self._payload_hash(text, reply_markup)
            self._anchor_registry.set_turn_anchor(
                chat_id,
                message_id=normalized_final_id,
                payload_hash=payload_hash,
            )

            return TurnMessageUpdate(
                message_id=normalized_final_id,
                call_label=call_text,
                call_action=call_action,
                board_line=board_line,
            )

    async def _build_turn_keyboard(
        self, call_text: str, call_action: PlayerAction
    ) -> InlineKeyboardMarkup:
        """Return a cached inline keyboard for the active player's actions."""

        cache_key = f"{call_action.value}|{call_text}"
        async with self._inline_keyboard_cache_lock:
            cached = self._inline_keyboard_cache.get(cache_key)
            if cached is not None:
                logger.debug(
                    "Inline keyboard cache hit %s (size %s/%s)",
                    cache_key,
                    self._inline_keyboard_cache.currsize,
                    self._inline_keyboard_cache.maxsize,
                )
                return cached

            keyboard = [
                [
                    InlineKeyboardButton(
                        text=PlayerAction.FOLD.value,
                        callback_data=PlayerAction.FOLD.value,
                    ),
                    InlineKeyboardButton(
                        text=PlayerAction.ALL_IN.value,
                        callback_data=PlayerAction.ALL_IN.value,
                    ),
                    InlineKeyboardButton(
                        text=call_text,
                        callback_data=call_action.value,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=str(PlayerAction.SMALL.value),
                        callback_data=str(PlayerAction.SMALL.value),
                    ),
                    InlineKeyboardButton(
                        text=str(PlayerAction.NORMAL.value),
                        callback_data=str(PlayerAction.NORMAL.value),
                    ),
                    InlineKeyboardButton(
                        text=str(PlayerAction.BIG.value),
                        callback_data=str(PlayerAction.BIG.value),
                    ),
                ],
            ]
            markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
            self._inline_keyboard_cache[cache_key] = markup
            logger.debug(
                "Turn keyboard cache size %s/%s",
                self._inline_keyboard_cache.currsize,
                self._inline_keyboard_cache.maxsize,
            )
            return markup


    async def edit_message_reply_markup(
        self,
        chat_id: ChatId,
        message_id: MessageId,
        reply_markup: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
    ) -> bool:
        """Update a message's inline keyboard while handling common failures."""
        if not message_id:
            return False
        try:
            return await self._messenger.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                request_category=RequestCategory.INLINE,
            )
        except BadRequest as e:
            err = str(e).lower()
            if "message is not modified" in err:
                logger.info(
                    "Reply markup already up to date",
                    extra={"chat_id": chat_id, "message_id": message_id},
                )
                return True
            if "message to edit not found" not in err:
                logger.warning(
                    "BadRequest editing reply markup",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Forbidden as e:
            logger.info(
                "Cannot edit reply markup, bot unauthorized",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        except Exception as e:
            logger.error(
                "Unexpected error editing reply markup",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        return False

    async def remove_markup(self, chat_id: ChatId, message_id: MessageId) -> None:
        """حذف دکمه‌های اینلاین از یک پیام و فیلتر کردن ارورهای رایج."""
        if not message_id:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=message_id,
            )
        except BadRequest as e:
            err = str(e).lower()
            if "message to edit not found" in err or "message is not modified" in err:
                logger.info(
                    "Markup already removed or message not found",
                    extra={"message_id": message_id},
                )
            else:
                logger.warning(
                    "BadRequest removing markup",
                    extra={
                        "error_type": type(e).__name__,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    },
                )
        except Forbidden as e:
            logger.info(
                "Cannot edit markup, bot unauthorized",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
        except Exception as e:
            logger.error(
                "Unexpected error removing markup",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            )
    
    async def remove_message(self, chat_id: ChatId, message_id: MessageId) -> None:
        """Stub for backward compatibility; message deletion is disabled."""
        logger.debug(
            "remove_message called for chat_id %s message_id %s but deletion is disabled",
            chat_id,
            message_id,
        )

    async def remove_message_delayed(
        self, chat_id: ChatId, message_id: MessageId, delay: float = 3.0
    ) -> None:
        """Stub for backward compatibility; message deletion is disabled."""
        logger.debug(
            "remove_message_delayed called for chat_id %s message_id %s but deletion is disabled",
            chat_id,
            message_id,
        )
        
    async def send_showdown_results(self, chat_id: ChatId, game: Game, winners_by_pot: list) -> None:
        """
        پیام نهایی نتایج بازی را با فرمت زیبا ساخته و ارسال می‌کند.
        این نسخه برای مدیریت ساختار داده جدید Side Pot (لیست دیکشنری‌ها) به‌روز شده است.
        """
        final_message = "🏆 *نتایج نهایی و نمایش کارت‌ها*\n\n"

        if not winners_by_pot:
            final_message += "خطایی در تعیین برنده رخ داد. پات تقسیم نشد."
        else:
            # نام‌گذاری پات‌ها برای نمایش بهتر (اصلی، فرعی ۱، فرعی ۲ و...)
            pot_names = ["*پات اصلی*", "*پات فرعی ۱*", "*پات فرعی ۲*", "*پات فرعی ۳*"]
            
            # FIX: حلقه برای پردازش صحیح "لیست دیکشنری‌ها" اصلاح شد
            for i, pot_data in enumerate(winners_by_pot):
                pot_amount = pot_data.get("amount", 0)
                winners_info = pot_data.get("winners", [])

                if pot_amount == 0 or not winners_info:
                    continue
                
                # انتخاب نام پات بر اساس ترتیب آن
                pot_name = pot_names[i] if i < len(pot_names) else f"*پات فرعی {i}*"
                final_message += f"💰 {pot_name}: {pot_amount}$\n"
                
                win_amount_per_player = pot_amount // len(winners_info)

                for winner in winners_info:
                    player = winner.get("player")
                    if not player: continue # اطمینان از وجود بازیکن

                    hand_type = winner.get('hand_type')
                    hand_cards = winner.get('hand_cards', [])
                    
                    hand_name_data = HAND_NAMES_TRANSLATIONS.get(hand_type, {})
                    hand_display_name = f"{hand_name_data.get('emoji', '🃏')} {hand_name_data.get('fa', 'دست نامشخص')}"

                    final_message += (
                        f"  - {player.mention_markdown} با دست {hand_display_name} "
                        f"برنده *{win_amount_per_player}$* شد.\n"
                    )
                    final_message += (
                        f"    کارت‌ها: {self._render_cards(hand_cards)}\n"
                    )
                
                final_message += "\n" # یک خط فاصله برای جداسازی پات‌ها

        final_message += "⎯" * 20 + "\n"
        board_line = self._format_card_line("🃏 Board", game.cards_table)
        final_message += f"{board_line}\n\n"

        final_message += "🤚 *کارت‌های سایر بازیکنان:*\n"
        all_players_in_hand = game.players_by(states=(PlayerState.ACTIVE, PlayerState.ALL_IN, PlayerState.FOLD))

        # FIX: استخراج صحیح ID برندگان از ساختار داده جدید
        winner_user_ids = set()
        for pot_data in winners_by_pot:
            for winner_info in pot_data.get("winners", []):
                if "player" in winner_info:
                    winner_user_ids.add(winner_info["player"].user_id)

        for p in all_players_in_hand:
            if p.user_id not in winner_user_ids:
                card_display = (
                    self._render_cards(p.cards)
                    if p.cards
                    else 'کارت‌ها نمایش داده نشد'
                )
                state_info = " (فولد)" if p.state == PlayerState.FOLD else ""
                final_message += f"  - {p.mention_markdown}{state_info}: {card_display}\n"

        # ارسال تصویر نهایی میز همراه با کپشن نتایج. اگر طول پیام از
        # محدودیت کپشن تلگرام بیشتر شد، ادامه پیام در یک پیام متنی جداگانه
        # ارسال می‌شود تا تعداد پیام‌ها حداقل باقی بماند.
        caption_limit = 1024
        caption = final_message[:caption_limit]
        remove_keyboard = ReplyKeyboardRemove()
        await self.send_desk_cards_img(
            chat_id=chat_id,
            cards=game.cards_table,
            caption=caption,
            reply_markup=remove_keyboard,
        )
        if len(final_message) > caption_limit:
            await self.send_message(
                chat_id=chat_id,
                text=final_message[caption_limit:],
                reply_markup=remove_keyboard,
                parse_mode="Markdown",
            )

    async def send_new_hand_ready_message(self, chat_id: ChatId) -> None:
        """پیام آمادگی برای دست جدید را ارسال می‌کند."""
        message = (
            "♻️ دست به پایان رسید. بازیکنان باقی‌مانده برای دست بعد حفظ شدند.\n"
            "برای شروع دست جدید، /start را بزنید یا بازیکنان جدید می‌توانند با دکمهٔ «نشستن سر میز» وارد شوند."
        )
        context = self._build_context(
            "send_new_hand_ready_message", chat_id=chat_id
        )
        normalized_message = self._validator.normalize_text(
            message,
            parse_mode=ParseMode.MARKDOWN,
            context=context,
        )
        if normalized_message is None:
            logger.warning(
                "Skipping new hand ready message due to invalid text",
                extra={"context": context},
            )
            return
        reply_keyboard = ReplyKeyboardMarkup(
            keyboard=[["/start", "نشستن سر میز"], ["/stop"]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=False,
        )
        try:
            await self._messenger.send_message(
                chat_id=chat_id,
                text=normalized_message,
                parse_mode=ParseMode.MARKDOWN,
                disable_notification=True,
                disable_web_page_preview=True,
                reply_markup=reply_keyboard,
            )
        except Exception as e:
            logger.error(
                "Error sending new hand ready message",
                extra={
                    "error_type": type(e).__name__,
                    "chat_id": chat_id,
                },
            )
