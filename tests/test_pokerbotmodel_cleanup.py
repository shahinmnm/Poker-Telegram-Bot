"""Tests for stale cleanup job configuration in PokerBotModel."""

import logging
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest

from pokerapp.background_jobs import StaleUserCleanupJob
from pokerapp.pokerbotmodel import PokerBotModel
from pokerapp.private_match_service import PrivateMatchService
from pokerapp.pokerbotview import TurnMessageUpdate
from pokerapp.entities import PlayerAction
from pokerapp.utils.request_metrics import RequestMetrics


def _build_view_mock() -> MagicMock:
    """Create a viewer mock compatible with PokerBotModel initialisation."""

    view = MagicMock()
    view.edit_message_text = AsyncMock(return_value=None)
    view.send_message_return_id = AsyncMock(return_value=None)
    view.send_message = AsyncMock()
    view.announce_player_seats = AsyncMock(return_value=None)
    view.send_player_role_anchors = AsyncMock(return_value=None)
    view.delete_message = AsyncMock()
    view.start_prestart_countdown = AsyncMock(return_value=None)
    view._cancel_prestart_countdown = AsyncMock(return_value=None)
    view.clear_all_player_anchors = AsyncMock(return_value=None)
    view.update_player_anchors_and_keyboards = AsyncMock(return_value=None)
    view.sync_player_private_keyboards = AsyncMock(return_value=None)
    view.update_turn_message = AsyncMock(
        return_value=TurnMessageUpdate(
            message_id=None,
            call_label="CHECK",
            call_action=PlayerAction.CHECK,
            board_line="",
        )
    )
    view.request_metrics = RequestMetrics(
        logger_=logging.getLogger("tests.pokerbotmodel_cleanup.metrics")
    )
    return view


def _build_model(config) -> PokerBotModel:
    """Instantiate a PokerBotModel with lightweight dependencies for tests."""

    view = _build_view_mock()
    bot = MagicMock()
    kv = fakeredis.aioredis.FakeRedis()

    table_manager = MagicMock()
    table_manager.get_active_game_ids = AsyncMock(return_value=[])
    table_manager.load_game = AsyncMock(return_value=(MagicMock(), None))
    table_manager.save_game = AsyncMock(return_value=None)

    private_match_service = MagicMock(spec=PrivateMatchService)
    private_match_service.configure = MagicMock()

    model = PokerBotModel(
        view=view,
        bot=bot,
        cfg=config,
        kv=kv,
        table_manager=table_manager,
        private_match_service=private_match_service,
    )
    return model


@pytest.mark.asyncio
async def test_stale_cleanup_job_disabled(config_factory):
    """Ensure background job remains disabled when configuration opts out."""

    config = config_factory(ENABLE_STALE_CLEANUP_JOB=False)
    model = _build_model(config)

    assert model._stale_cleanup_job is None

    # The start/stop helpers should silently ignore requests when job disabled.
    await model.start_stale_user_cleanup()
    await model.stop_stale_user_cleanup()


def test_stale_cleanup_job_enabled(config_factory):
    """Ensure background job is created when the feature flag is enabled."""

    config = config_factory(ENABLE_STALE_CLEANUP_JOB=True)
    model = _build_model(config)

    assert isinstance(model._stale_cleanup_job, StaleUserCleanupJob)
