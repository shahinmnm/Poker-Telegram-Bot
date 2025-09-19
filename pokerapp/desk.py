#!/usr/bin/env python3


from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from threading import Lock

from cachetools import LRUCache
from PIL import Image

from pokerapp.cards import Cards, Card


class DeskImageGenerator:
    def __init__(
        self,
        card_assets: Path = Path("./assets/cards"),
        card_size=(84, 128),
        padding=10,
    ):
        self._card_assets = card_assets
        self._file_suit_mapping = {
            "♣": "C",
            "♦": "D",
            "♥": "H",
            "♠": "S",
        }
        self._card_size = card_size
        self._loaded_card_imgs = {}
        self._padding = padding
        self._desk_cache: LRUCache[tuple[Card, ...], bytes] = LRUCache(maxsize=64)
        self._desk_cache_lock = Lock()
        self._logger = logging.getLogger(__name__).getChild("desk_cache")

    def _get_file_name(self, card: Card) -> Path:
        return self._card_assets.joinpath(
            card.rank + self._file_suit_mapping[card.suit] + ".jpg",
        )

    def _load_card_image(self, card: Card) -> Image:
        if card in self._loaded_card_imgs:
            return self._loaded_card_imgs[card]

        im_file = self._get_file_name(card)
        im = Image.open(im_file)
        im = im.resize(self._card_size)

        self._loaded_card_imgs[card] = im

        return im

    def generate_desk(self, cards: Cards) -> Image:
        padding_horizontal = self._padding * (len(cards) - 1)
        desk_im = Image.new(mode="RGBA", size=(
            self._card_size[0] * len(cards) + padding_horizontal,
            self._card_size[1],
        ), color=(255, 255, 255, 0))

        offset_x = 0

        for card in cards:
            card_im = self._load_card_image(card)
            desk_im.paste(card_im, (offset_x, 0))

            offset_x += self._card_size[0] + self._padding

        return desk_im

    def render_cached_png(self, cards: Cards) -> bytes:
        """Return a PNG rendering of ``cards`` using an LRU cache."""

        key = tuple(cards)
        with self._desk_cache_lock:
            cached = self._desk_cache.get(key)
            if cached is not None:
                self._logger.debug(
                    "Desk image cache hit", extra={"cards_count": len(key)}
                )
                return cached
        image = self.generate_desk(cards)
        buffer = BytesIO()
        image.save(buffer, "PNG")
        data = buffer.getvalue()
        with self._desk_cache_lock:
            self._desk_cache[key] = data
            self._logger.debug(
                "Desk image cache store", extra={"cards_count": len(key)}
            )
        return data
