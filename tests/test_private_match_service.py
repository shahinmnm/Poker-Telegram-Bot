import logging
from unittest.mock import MagicMock

import fakeredis.aioredis

from pokerapp.private_match_service import PrivateMatchService
from pokerapp.config import get_game_constants


def test_private_match_service_constants():
    service = PrivateMatchService(
        kv=fakeredis.aioredis.FakeRedis(),
        table_manager=MagicMock(),
        logger=logging.getLogger("test.private_match"),
        constants=get_game_constants(),
    )
    assert service.PRIVATE_MATCH_QUEUE_TTL > 0
    assert service.PRIVATE_MATCH_STATE_TTL > 0
