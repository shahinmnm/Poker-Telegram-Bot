import fakeredis
import fakeredis.aioredis
import pytest

from pokerapp.entities import Game, GameState, Player, PlayerState, Wallet
from pokerapp.state_validator import GameStateValidator, ValidationIssue
from pokerapp.table_manager import TableManager


class DummyWallet(Wallet):
    @staticmethod
    def _prefix(id: int, suffix: str = ""):
        return f"{id}{suffix}"

    async def add_daily(self, amount: int) -> int:
        return amount

    async def has_daily_bonus(self) -> bool:
        return False

    async def inc(self, amount: int = 0) -> int:
        return amount

    async def inc_authorized_money(self, game_id: str, amount: int) -> None:
        return None

    async def authorized_money(self, game_id: str) -> int:
        return 0

    async def authorize(self, game_id: str, amount: int) -> None:
        return None

    async def authorize_all(self, game_id: str) -> int:
        return 0

    async def value(self) -> int:
        return 0

    async def approve(self, game_id: str) -> None:
        return None

    async def cancel(self, game_id: str) -> None:
        return None


@pytest.fixture
def validator() -> GameStateValidator:
    return GameStateValidator()


def _make_player(user_id: str = "1") -> Player:
    return Player(
        user_id=user_id,
        mention_markdown=f"@player{user_id}",
        wallet=DummyWallet(),
        ready_message_id="ready",
    )


def test_validate_valid_waiting_game(validator: GameStateValidator) -> None:
    game = Game()

    result = validator.validate_game(game)

    assert result.is_valid is True
    assert result.issues == []


def test_validate_missing_dealer(validator: GameStateValidator) -> None:
    game = Game()
    player = _make_player()
    game.add_player(player, seat_index=0)
    game.state = GameState.ROUND_PRE_FLOP
    game.dealer_index = -1
    game.remain_cards = game.remain_cards[:50]

    result = validator.validate_game(game)

    assert ValidationIssue.MISSING_DEALER in result.issues
    assert result.recovery_action == "reset_to_waiting"


def test_recover_to_waiting(validator: GameStateValidator) -> None:
    game = Game()
    player = _make_player()
    game.add_player(player, seat_index=0)
    game.state = GameState.ROUND_PRE_FLOP
    game.dealer_index = -1
    game.remain_cards = game.remain_cards[:50]
    player.cards = ["AS", "KD"]
    player.total_bet = 100
    player.has_acted = True
    player.state = PlayerState.FOLD

    result = validator.validate_game(game)
    recovered = validator.recover_game(game, result)

    assert recovered.state is GameState.INITIAL
    assert recovered.pot == 0
    assert recovered.cards_table == []
    assert len(recovered.remain_cards) == 52
    assert recovered.dealer_index == -1
    assert player.cards == []
    assert player.total_bet == 0
    assert player.has_acted is False
    assert player.state is PlayerState.ACTIVE


@pytest.mark.asyncio
async def test_corrupted_json_handling() -> None:
    server = fakeredis.FakeServer()
    redis_async = fakeredis.aioredis.FakeRedis(server=server)
    validator = GameStateValidator()
    table_manager = TableManager(redis_async, state_validator=validator)

    await redis_async.set("chat:123:game", b"{invalid json")

    game, result = await table_manager.load_game(123, validate=True)

    assert game is None
    assert result is not None
    assert ValidationIssue.CORRUPTED_JSON in result.issues
    assert await redis_async.get("chat:123:game") is None
