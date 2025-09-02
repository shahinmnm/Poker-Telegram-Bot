#!/usr/bin/env python3

import enum
from itertools import combinations
from typing import List, Tuple, Dict

from pokerapp.cards import Card, Cards
from pokerapp.entities import Score

HAND_RANK_MULTIPLIER = 15**5

class HandsOfPoker(enum.Enum):
    ROYAL_FLUSH = 10
    STRAIGHT_FLUSH = 9
    FOUR_OF_A_KIND = 8
    FULL_HOUSE = 7
    FLUSH = 6
    STRAIGHT = 5
    THREE_OF_A_KIND = 4
    TWO_PAIR = 3
    PAIR = 2
    HIGH_CARD = 1

# --- دیکشنری جدید برای نمایش نتایج ---
HAND_NAMES_TRANSLATIONS: Dict[HandsOfPoker, Dict[str, str]] = {
    HandsOfPoker.ROYAL_FLUSH:     {"fa": "رویال فلاش", "en": "Royal Flush", "emoji": "👑"},
    HandsOfPoker.STRAIGHT_FLUSH:  {"fa": "استریت فلاش", "en": "Straight Flush", "emoji": "💎"},
    HandsOfPoker.FOUR_OF_A_KIND:  {"fa": "کاره (چهار تایی)", "en": "Four of a Kind", "emoji": "💣"},
    HandsOfPoker.FULL_HOUSE:      {"fa": "فول هاوس", "en": "Full House", "emoji": "🏠"},
    HandsOfPoker.FLUSH:           {"fa": "فلاش (رنگ)", "en": "Flush", "emoji": "🎨"},
    HandsOfPoker.STRAIGHT:        {"fa": "استریت (ردیف)", "en": "Straight", "emoji": "🚀"},
    HandsOfPoker.THREE_OF_A_KIND: {"fa": "سه تایی", "en": "Three of a Kind", "emoji": "🧩"},
    HandsOfPoker.TWO_PAIR:        {"fa": "دو پِر", "en": "Two Pair", "emoji": "✌️"},
    HandsOfPoker.PAIR:            {"fa": "پِر (جفت)", "en": "Pair", "emoji": "🔗"},
    HandsOfPoker.HIGH_CARD:       {"fa": "کارت بالا", "en": "High Card", "emoji": "🃏"},
}


class WinnerDetermination:
    """
    این کلاس مسئولیت تعیین ارزش و امتیاز دست‌های پوکر را بر عهده دارد.
    """

    def get_hand_value(self, player_cards: Cards, table_cards: Cards) -> Tuple[Score, Tuple[Card, ...]]:
        """
        متد اصلی و عمومی کلاس.
        کارت‌های بازیکن و میز را گرفته، بهترین دست ۵ کارتی را پیدا کرده
        و بالاترین امتیاز ممکن به همراه کارت‌های آن دست را برمی‌گرداند.
        """
        all_cards = player_cards + table_cards
        if len(all_cards) < 5:
            return 0, tuple()

        possible_hands = list(combinations(all_cards, 5))

        max_score = 0
        best_hand: Tuple[Card, ...] = tuple()

        for hand in possible_hands:
            score = self._calculate_hand_score(hand)
            if score > max_score:
                max_score = score
                best_hand = hand
        
        return max_score, best_hand

    def _calculate_hand_score(self, hand: Tuple[Card, ...]) -> Score:
        """
        امتیاز یک دست ۵ کارتی مشخص را محاسبه می‌کند.
        """
        values = sorted([card.value for card in hand])
        suits = [card.suit for card in hand]

        is_flush = len(set(suits)) == 1
        
        is_straight = False
        unique_values = sorted(list(set(values)))
        straight_high_card = 0
        if len(unique_values) == 5:
            if unique_values[4] - unique_values[0] == 4:
                is_straight = True
                straight_high_card = unique_values[4]
            elif unique_values == [2, 3, 4, 5, 14]: # Ace-low straight
                is_straight = True
                straight_high_card = 5

        grouped_values, grouped_keys = self._group_hand_by_value(values)

        if is_straight and is_flush:
            if straight_high_card == 14 and values[0] == 10:
                return self._calculate_score_value([], HandsOfPoker.ROYAL_FLUSH)
            return self._calculate_score_value([straight_high_card], HandsOfPoker.STRAIGHT_FLUSH)

        if grouped_values == [1, 4]:
            return self._calculate_score_value(grouped_keys, HandsOfPoker.FOUR_OF_A_KIND)
        if grouped_values == [2, 3]:
            return self._calculate_score_value(grouped_keys, HandsOfPoker.FULL_HOUSE)
        if is_flush:
            return self._calculate_score_value(values[::-1], HandsOfPoker.FLUSH)
        if is_straight:
            return self._calculate_score_value([straight_high_card], HandsOfPoker.STRAIGHT)
        if grouped_values == [1, 1, 3]:
            return self._calculate_score_value(grouped_keys, HandsOfPoker.THREE_OF_A_KIND)
        if grouped_values == [1, 2, 2]:
            return self._calculate_score_value(grouped_keys, HandsOfPoker.TWO_PAIR)
        if grouped_values == [1, 1, 1, 2]:
            return self._calculate_score_value(grouped_keys, HandsOfPoker.PAIR)
        return self._calculate_score_value(values[::-1], HandsOfPoker.HIGH_CARD)

    @staticmethod
    def _calculate_score_value(hand_values: List[int], hand_type: HandsOfPoker) -> Score:
        score = HAND_RANK_MULTIPLIER * hand_type.value
        i = 1
        for val in hand_values:
            score += val * i
            i *= 15
        return score

    @staticmethod
    def _group_hand_by_value(hand_values: List[int]) -> Tuple[List[int], List[int]]:
        dict_hand = {}
        for i in hand_values:
            dict_hand[i] = dict_hand.get(i, 0) + 1
        sorted_dict_items = sorted(dict_hand.items(), key=lambda item: (item[1], item[0]))
        counts = [item[1] for item in sorted_dict_items]
        keys = [item[0] for item in sorted_dict_items]
        return (counts, keys)
