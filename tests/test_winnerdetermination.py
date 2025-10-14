#!/usr/bin/env python3

import unittest

from typing import Tuple

from pokerapp.cards import Cards, Card
from pokerapp.winnerdetermination import HandsOfPoker, WinnerDetermination


HANDS_FILE = "./tests/hands.txt"


class TestWinnerDetermination(unittest.TestCase):
    @classmethod
    def _parse_hands(cls, line: str) -> Tuple[Cards]:
        """ The first hand is the best """

        line_toks = line.split()

        first_hand = cls._parse_hand(line_toks[0])
        second_hand = cls._parse_hand(line_toks[1])

        if line_toks[2] == "2":
            return (second_hand, first_hand)

        return (first_hand, second_hand)

    @staticmethod
    def _parse_hand(hand: str) -> Cards:
        return [Card(c) for c in hand.split("'")]

    def test_determine_best_hand(self):
        """
        Test calculation of the best hand
        """

        with open(HANDS_FILE, "r") as f:
            game_lines = f.readlines()

        determinator = WinnerDetermination()
        for ln in game_lines:
            hands = TestWinnerDetermination._parse_hands(ln)
            best_type, _, best_cards = determinator.determine_best_hand(hands)
            self.assertListEqual(list(best_cards), list(hands[0]))

    def test_wheel_straight_flush_not_royal(self):
        determinator = WinnerDetermination()
        wheel_cards = [Card("A♣"), Card("2♣"), Card("3♣"), Card("4♣"), Card("5♣")]
        player_cards = wheel_cards[:2]
        table_cards = wheel_cards[2:] + [Card("9♦"), Card("K♥")]

        hand_type, score, best_cards = determinator.get_hand_value(player_cards, table_cards)

        self.assertEqual(hand_type, HandsOfPoker.STRAIGHT_FLUSH)
        self.assertNotEqual(hand_type, HandsOfPoker.ROYAL_FLUSH)
        expected_best_cards = tuple(sorted(wheel_cards, key=lambda c: c.value, reverse=True))
        self.assertEqual(best_cards, expected_best_cards)


if __name__ == '__main__':
    unittest.main()
