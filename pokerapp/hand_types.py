# File: pokerapp/hand_types.py

from pokerapp.winnerdetermination import HandsOfPoker, HAND_RANK

# Map the enum to Persian
hand_type_names = {
    HandsOfPoker.ROYAL_FLUSH:   "رویال فلاش",
    HandsOfPoker.STRAIGHT_FLUSH:"استریت فلاش",
    HandsOfPoker.FOUR_OF_A_KIND:"چهار کارتی",
    HandsOfPoker.FULL_HOUSE:    "فول هاوس",
    HandsOfPoker.FLUSH:         "فلاش",
    HandsOfPoker.STRAIGHTS:     "استریت",
    HandsOfPoker.THREE_OF_A_KIND:"سه کارتی",
    HandsOfPoker.TWO_PAIR:      "دو پر",
    HandsOfPoker.PAIR:          "جفت",
    HandsOfPoker.HIGH_CARD:     "کارت بالا",
}

def get_hand_type_by_score(score: int) -> str:
    """
    Given a raw Score (int), extract the hand‐type enum and return its Persian name.
    """
    # HAND_RANK is the base multiplier (15**5); integer‐division yields the enum value
    kind_value = score // HAND_RANK
    kind = HandsOfPoker(kind_value)
    return hand_type_names.get(kind, "")
