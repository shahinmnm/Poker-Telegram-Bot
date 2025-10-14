#!/usr/bin/env python3

import enum
from itertools import combinations
from typing import Dict, List, Tuple

from pokerapp.cards import Card, Cards
from pokerapp.entities import Score
from pokerapp.config import get_game_constants

HAND_RANK_MULTIPLIER = 15**5


_CONSTANTS = get_game_constants()
_HANDS_RESOURCE = _CONSTANTS.hands
if isinstance(_HANDS_RESOURCE, dict):
    _RAW_HAND_TRANSLATIONS = _HANDS_RESOURCE.get("hands", {})
    if not isinstance(_RAW_HAND_TRANSLATIONS, dict):
        _RAW_HAND_TRANSLATIONS = {}
    _HAND_DEFAULT_LANGUAGE = _HANDS_RESOURCE.get("default_language", "fa")
    if not isinstance(_HAND_DEFAULT_LANGUAGE, str) or not _HAND_DEFAULT_LANGUAGE:
        _HAND_DEFAULT_LANGUAGE = "fa"
else:
    _RAW_HAND_TRANSLATIONS = {}
    _HAND_DEFAULT_LANGUAGE = "fa"

HAND_LANGUAGE_ORDER: Tuple[str, ...] = tuple(
    dict.fromkeys([_HAND_DEFAULT_LANGUAGE, "fa", "en"])
)


def _load_hand_translations() -> Dict["HandsOfPoker", Dict[str, str]]:
    mapping: Dict[HandsOfPoker, Dict[str, str]] = {}
    for hand in HandsOfPoker:
        entry = _RAW_HAND_TRANSLATIONS.get(hand.name, {})
        if isinstance(entry, dict):
            mapping[hand] = dict(entry)
        else:
            mapping[hand] = {}
    return mapping

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

HAND_NAMES_TRANSLATIONS: Dict[HandsOfPoker, Dict[str, str]] = _load_hand_translations()

class WinnerDetermination:
    """
    این کلاس مسئولیت تعیین ارزش و امتیاز دست‌های پوکر را بر عهده دارد.
    ترکیبی بهینه از هر دو نسخه قبلی و فعلی.
    """

    def get_hand_value(self, player_cards: Cards, table_cards: Cards) -> Tuple[HandsOfPoker, Score, Tuple[Card, ...]]:
        """
        متد اصلی و عمومی کلاس.
        کارت‌های بازیکن و میز را گرفته، بهترین دست ۵ کارتی را پیدا کرده
        و (نوع دست، بالاترین امتیاز، کارت‌های آن دست) را برمی‌گرداند.
        """
        all_cards = player_cards + table_cards
        if len(all_cards) < 5:
            return HandsOfPoker.HIGH_CARD, 0, tuple()

        possible_hands = list(combinations(all_cards, 5))

        max_score = 0
        best_hand_type = HandsOfPoker.HIGH_CARD
        best_hand_cards: Tuple[Card, ...] = tuple()

        for hand_tuple in possible_hands:
            # مرتب‌سازی کارت‌ها بر اساس ارزش به صورت نزولی
            hand_cards = tuple(sorted(hand_tuple, key=lambda c: c.value, reverse=True))
            current_score, current_hand_type = self._calculate_hand_score(hand_cards)

            if current_score > max_score:
                max_score = current_score
                best_hand_type = current_hand_type
                best_hand_cards = hand_cards
        
        # برگرداندن بهترین دست ممکن از بین تمام ترکیب‌ها
        return best_hand_type, max_score, best_hand_cards

    def determine_best_hand(self, hands: Tuple[Cards, ...]) -> Tuple[HandsOfPoker, Score, Tuple[Card, ...]]:
        """Determine the best hand among multiple 5-card hands."""
        best_type = HandsOfPoker.HIGH_CARD
        best_score = -1
        best_cards: Cards = []
        for hand in hands:
            hand_type, score, _ = self.get_hand_value(hand, [])
            if score > best_score:
                best_type, best_score, best_cards = hand_type, score, hand
        return best_type, best_score, tuple(best_cards)

    def _calculate_hand_score(self, hand: Tuple[Card, ...]) -> Tuple[Score, HandsOfPoker]:
        """
        امتیاز و نوع یک دست ۵ کارتی مشخص را محاسبه می‌کند.
        مقدار بازگشتی: (امتیاز عددی, نوع دست enum)
        """
        values = [card.value for card in hand] # از قبل مرتب شده نزولی
        suits = [card.suit for card in hand]
        is_flush = len(set(suits)) == 1

        # بررسی استریت با توجه به اینکه values از قبل مرتب شده (نزولی)
        is_straight = all(values[i] - values[i+1] == 1 for i in range(len(values)-1))
        
        # حالت خاص استریت A-5 (wheel)
        original_values_for_score = list(values) # کپی برای محاسبه امتیاز
        if values == [14, 5, 4, 3, 2]:
             is_straight = True
             # برای محاسبه امتیاز، آس را با ارزش ۱ در نظر می‌گیریم تا بعد از ۵ قرار گیرد
             original_values_for_score = [5, 4, 3, 2, 1]

        grouped_counts, grouped_keys = self._group_hand_by_value(values)

        if is_straight and is_flush:
            if values == [14, 13, 12, 11, 10]: # رویال فلاش
                hand_type = HandsOfPoker.ROYAL_FLUSH
            else: # استریت فلاش (شامل حالت A-2-3-4-5)
                hand_type = HandsOfPoker.STRAIGHT_FLUSH
            return self._calculate_score_value(original_values_for_score, hand_type), hand_type

        if grouped_counts == [1, 4]:
            hand_type = HandsOfPoker.FOUR_OF_A_KIND
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_counts == [2, 3]:
            hand_type = HandsOfPoker.FULL_HOUSE
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if is_flush:
            hand_type = HandsOfPoker.FLUSH
            return self._calculate_score_value(original_values_for_score, hand_type), hand_type
        if is_straight:
            hand_type = HandsOfPoker.STRAIGHT
            return self._calculate_score_value(original_values_for_score, hand_type), hand_type
        if grouped_counts == [1, 1, 3]:
            hand_type = HandsOfPoker.THREE_OF_A_KIND
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_counts == [1, 2, 2]:
            hand_type = HandsOfPoker.TWO_PAIR
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_counts == [1, 1, 1, 2]:
            hand_type = HandsOfPoker.PAIR
            return self._calculate_score_value(grouped_keys, hand_type), hand_type

        hand_type = HandsOfPoker.HIGH_CARD
        return self._calculate_score_value(original_values_for_score, hand_type), hand_type

    @staticmethod
    def _calculate_score_value(hand_values: List[int], hand_type: HandsOfPoker) -> Score:
        """
        امتیاز را محاسبه می‌کند. استفاده از توان ۱۵ باعث می‌شود ارزش کارت‌های
        مهم‌تر (مثل کارتِ Pair) وزن بیشتری از کیکرها داشته باشد.
        """
        score = HAND_RANK_MULTIPLIER * hand_type.value
        # hand_values باید از قبل بر اساس اهمیت مرتب شده باشد (از بیشترین به کمترین)
        power = len(hand_values) - 1
        for val in hand_values:
            score += val * (15 ** power)
            power -= 1
        return score

    @staticmethod
    def _group_hand_by_value(hand_values: List[int]) -> Tuple[List[int], List[int]]:
        """
        کارت‌ها را بر اساس ارزششان گروه‌بندی می‌کند.
        خروجی: (تعداد تکرارها, ارزش کارت‌ها) مرتب شده بر اساس اهمیت.
        مثال برای فول هاوس: ([2, 3], [کارت پِر, کارت سه‌تایی])
        """
        dict_hand = {}
        for i in hand_values:
            dict_hand[i] = dict_hand.get(i, 0) + 1
        # counts مرتب‌شده بر اساس تعداد تکرار به صورت صعودی
        counts = sorted(dict_hand.values())
        # keys مرتب‌شده بر اساس اهمیت (تعداد تکرار و سپس ارزش کارت) به صورت نزولی
        keys = [item[0] for item in sorted(dict_hand.items(), key=lambda item: (item[1], item[0]), reverse=True)]
        return (counts, keys)
