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

    def get_hand_value(self, player_cards: Cards, table_cards: Cards) -> Tuple[HandsOfPoker, Score, Tuple[Card, ...]]:
        """
        متد اصلی و عمومی کلاس.
        کارت‌های بازیکن و میز را گرفته، بهترین دست ۵ کارتی را پیدا کرده
        و (نوع دست، بالاترین امتیاز، کارت‌های آن دست) را برمی‌گرداند.
        """
        all_cards = player_cards + table_cards
        if len(all_cards) < 5:
            # return 0, tuple()  <-- قبلی
            return HandsOfPoker.HIGH_CARD, 0, tuple() # <-- جدید

        possible_hands = list(combinations(all_cards, 5))

        max_score = 0
        best_hand_type = HandsOfPoker.HIGH_CARD
        best_hand_cards: Tuple[Card, ...] = tuple()

        for hand_tuple in possible_hands:
            hand_cards = tuple(sorted(hand_tuple, key=lambda c: c.value, reverse=True))
            
            # calculate_hand_score حالا یک تاپل برمی‌گرداند
            current_score, current_hand_type = self._calculate_hand_score(hand_cards)

            if current_score > max_score:
                max_score = current_score
                best_hand_type = current_hand_type
                best_hand_cards = hand_cards
        
        # برگرداندن تاپل سه‌تایی
        return best_hand_type, max_score, best_hand_cards
        
    def get_hand_value_and_type(self, player_cards: Cards, table_cards: Cards) -> Tuple[Score, Tuple[Card, ...], HandsOfPoker]:
        """
        متد اصلی و عمومی کلاس.
        کارت‌های بازیکن و میز را گرفته، بهترین دست ۵ کارتی را پیدا کرده
        و بالاترین امتیاز، کارت‌های آن دست و نوع دست را برمی‌گرداند.
        """
        all_cards = player_cards + table_cards
        if len(all_cards) < 5:
            return 0, tuple(), HandsOfPoker.HIGH_CARD # Return a default hand type

        possible_hands = list(combinations(all_cards, 5))

        max_score = 0
        best_hand: Tuple[Card, ...] = tuple()
        best_hand_type: HandsOfPoker = HandsOfPoker.HIGH_CARD # Default value

        for hand in possible_hands:
            score, hand_type = self._calculate_hand_score_and_type(hand)
            if score > max_score:
                max_score = score
                best_hand = hand
                best_hand_type = hand_type

        return max_score, best_hand, best_hand_type

    def _calculate_hand_score_and_type(self, hand: Tuple[Card, ...]) -> Tuple[Score, HandsOfPoker]:
        """
        امتیاز و نوع یک دست ۵ کارتی مشخص را محاسبه و برمی‌گرداند.
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
                hand_type = HandsOfPoker.ROYAL_FLUSH
                score = self._calculate_score_value([], hand_type)
                return score, hand_type
            hand_type = HandsOfPoker.STRAIGHT_FLUSH
            score = self._calculate_score_value([straight_high_card], hand_type)
            return score, hand_type

        if grouped_values == [1, 4]:
            hand_type = HandsOfPoker.FOUR_OF_A_KIND
            score = self._calculate_score_value(grouped_keys, hand_type)
            return score, hand_type
        if grouped_values == [2, 3]:
            hand_type = HandsOfPoker.FULL_HOUSE
            score = self._calculate_score_value(grouped_keys, hand_type)
            return score, hand_type
        if is_flush:
            hand_type = HandsOfPoker.FLUSH
            score = self._calculate_score_value(values[::-1], hand_type)
            return score, hand_type
        if is_straight:
            hand_type = HandsOfPoker.STRAIGHT
            score = self._calculate_score_value([straight_high_card], hand_type)
            return score, hand_type
        if grouped_values == [1, 1, 3]:
            hand_type = HandsOfPoker.THREE_OF_A_KIND
            score = self._calculate_score_value(grouped_keys, hand_type)
            return score, hand_type
        if grouped_values == [1, 2, 2]:
            hand_type = HandsOfPoker.TWO_PAIR
            score = self._calculate_score_value(grouped_keys, hand_type)
            return score, hand_type
        if grouped_values == [1, 1, 1, 2]:
            hand_type = HandsOfPoker.PAIR
            score = self._calculate_score_value(grouped_keys, hand_type)
            return score, hand_type

        # This is the crucial part that was missing a proper return structure
        hand_type = HandsOfPoker.HIGH_CARD
        score = self._calculate_score_value(values[::-1], hand_type)
        return score, hand_type

    def _calculate_hand_score(self, hand: Tuple[Card, ...]) -> Tuple[Score, HandsOfPoker]:
        """
        امتیاز و نوع یک دست ۵ کارتی مشخص را محاسبه می‌کند.
        مقدار بازگشتی: (امتیاز عددی, نوع دست enum)
        """
        # دست باید از قبل بر اساس ارزش کارت مرتب شده باشد
        values = [card.value for card in hand]
        suits = [card.suit for card in hand]

        is_flush = len(set(suits)) == 1
        
        # بررسی استریت با توجه به اینکه values از قبل مرتب شده (نزولی)
        is_straight = all(values[i] - values[i+1] == 1 for i in range(len(values)-1))
        # حالت خاص استریت A-5 (wheel)
        if values == [14, 5, 4, 3, 2]:
             is_straight = True
             # برای محاسبه امتیاز، آس را با ارزش ۱ در نظر می‌گیریم
             values = [5, 4, 3, 2, 1]

        # بررسی گروه‌بندی کارت‌ها
        grouped_values, grouped_keys = self._group_hand_by_value(values)

        if is_straight and is_flush:
            if values[0] == 14: # رویال فلاش
                hand_type = HandsOfPoker.ROYAL_FLUSH
            else: # استریت فلاش
                hand_type = HandsOfPoker.STRAIGHT_FLUSH
            return self._calculate_score_value(values, hand_type), hand_type

        if grouped_values == [1, 4]:
            hand_type = HandsOfPoker.FOUR_OF_A_KIND
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_values == [2, 3]:
            hand_type = HandsOfPoker.FULL_HOUSE
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if is_flush:
            hand_type = HandsOfPoker.FLUSH
            return self._calculate_score_value(values, hand_type), hand_type
        if is_straight:
            hand_type = HandsOfPoker.STRAIGHT
            return self._calculate_score_value(values, hand_type), hand_type
        if grouped_values == [1, 1, 3]:
            hand_type = HandsOfPoker.THREE_OF_A_KIND
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_values == [1, 2, 2]:
            hand_type = HandsOfPoker.TWO_PAIR
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        if grouped_values == [1, 1, 1, 2]:
            hand_type = HandsOfPoker.PAIR
            return self._calculate_score_value(grouped_keys, hand_type), hand_type
        
        hand_type = HandsOfPoker.HIGH_CARD
        return self._calculate_score_value(values, hand_type), hand_type

    
# In class WinnerDetermination:
    def determine_winners_with_hand_details(self, game) -> List[Dict]:
        """
        برندگان را به همراه جزئیات کامل دست‌شان (نوع دست، بهترین ۵ کارت) برمی‌گرداند.
        """
        players_in_game = [p for p in game.players if p.state != PlayerState.FOLD]
        if not players_in_game:
            return []

        if len(players_in_game) == 1:
            winner = players_in_game[0]
            # جزئیات دست را حتی برای یک برنده هم محاسبه می‌کنیم
            hand_type, score, best_cards = self.get_hand_value(winner.cards, game.cards_table)
            return [{'player': winner, 'score': score, 'hand_type': hand_type, 'best_hand_cards': best_cards}]

        scores = {}
        for player in players_in_game:
            hand_type, score, best_cards = self.get_hand_value(player.cards, game.cards_table)
            scores[player] = {'score': score, 'hand_type': hand_type, 'best_hand_cards': best_cards}

        if not scores:
            return []
            
        max_score = max(s['score'] for s in scores.values())
        
        winners_details = []
        for player, details in scores.items():
            if details['score'] == max_score:
                winners_details.append({'player': player, **details})
        
        return winners_details


        if not scores:
            return []
            
        max_score = max(s['score'] for s in scores.values())
        
        winners_details = []
        for player, details in scores.items():
            if details['score'] == max_score:
                winners_details.append({
                    'player': player,
                    **details
                })
        
        return winners_details

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
