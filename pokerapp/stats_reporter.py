"""Statistics reporting helpers for the poker game engine."""

from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, Optional, Sequence

from pokerapp.entities import ChatId, Game, Player, PlayerState
from pokerapp.stats import (
    BaseStatsService,
    NullStatsService,
    PlayerHandResult,
    PlayerIdentity,
)
from pokerapp.utils.player_report_cache import (
    PlayerReportCache as RedisPlayerReportCache,
)
from pokerapp.utils.common import normalize_player_ids
from pokerapp.utils.cache import AdaptivePlayerReportCache


class StatsReporter:
    """Encapsulate stats service interactions and report cache invalidation."""

    def __init__(
        self,
        *,
        stats_service: BaseStatsService,
        player_report_cache: Optional[RedisPlayerReportCache],
        adaptive_player_report_cache: Optional[AdaptivePlayerReportCache],
        safe_int: Callable[[ChatId], int],
        logger: logging.Logger,
    ) -> None:
        self._stats = stats_service
        self._player_report_cache = player_report_cache
        self._adaptive_player_report_cache = adaptive_player_report_cache
        self._safe_int = safe_int
        self._logger = logger

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    def _stats_enabled(self) -> bool:
        return not isinstance(self._stats, NullStatsService)

    async def hand_started(
        self,
        game: Game,
        chat_id: ChatId,
        build_identity_from_player: Callable[[Player], PlayerIdentity],
    ) -> None:
        """Notify the stats backend that a hand has started and invalidate caches."""

        if self._stats_enabled():
            players = [
                build_identity_from_player(player)
                for player in game.seated_players()
            ]
            await self._stats.start_hand(
                hand_id=game.id,
                chat_id=self._safe_int(chat_id),
                players=players,
            )
        await self.invalidate_players(game.seated_players())

    async def hand_finished_deferred(
        self,
        game: Game,
        chat_id: ChatId,
        *,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
        pot_total: int,
    ) -> None:
        """Report hand statistics outside of the stage lock."""

        if self._stats_enabled():
            results = self._build_hand_results(
                game=game, payouts=payouts, hand_labels=hand_labels
            )
            await self._stats.record_hand_finished_batch(
                hand_id=game.id,
                chat_id=self._safe_int(chat_id),
                results=results,
                pot_total=pot_total,
            )
        await self.invalidate_players(game.players, event_type="hand_finished")

    async def invalidate_players(
        self,
        players: Iterable[Player | int],
        *,
        event_type: Optional[str] = None,
    ) -> None:
        """Invalidate cached stats reports for the supplied ``players``."""

        normalized = normalize_player_ids(players)
        if not normalized:
            return

        if self._adaptive_player_report_cache:
            if event_type:
                self._adaptive_player_report_cache.invalidate_on_event(
                    normalized, event_type
                )
            else:
                self._adaptive_player_report_cache.invalidate_many(normalized)

        if self._player_report_cache:
            await self._player_report_cache.invalidate(normalized)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_hand_results(
        self,
        *,
        game: Game,
        payouts: Dict[int, int],
        hand_labels: Dict[int, Optional[str]],
    ) -> Sequence[PlayerHandResult]:
        results = []
        for player in game.seated_players():
            try:
                user_id = int(getattr(player, "user_id", 0) or 0)
            except (TypeError, ValueError):
                user_id = 0
            total_bet = int(getattr(player, "total_bet", 0))
            payout = int(payouts.get(user_id, 0))
            net_profit = payout - total_bet
            if net_profit > 0 or (payout > 0 and total_bet == 0):
                result_flag = "win"
            elif net_profit < 0:
                result_flag = "loss"
            else:
                result_flag = "push"
            label = hand_labels.get(user_id)
            if not label and result_flag == "win" and getattr(player, "state", None) == PlayerState.ALL_IN:
                label = "پیروزی با آل-این"
            was_all_in = getattr(player, "state", None) == PlayerState.ALL_IN
            results.append(
                PlayerHandResult(
                    user_id=user_id,
                    display_name=getattr(player, "mention_markdown", str(user_id)),
                    total_bet=total_bet,
                    payout=payout,
                    net_profit=net_profit,
                    hand_type=label,
                    was_all_in=was_all_in,
                    result=result_flag,
                )
            )
        return results
