import pathlib


def test_game_engine_uses_trace_lock_guard_only() -> None:
    game_engine_path = pathlib.Path(__file__).resolve().parents[1] / "pokerapp" / "game_engine.py"
    content = game_engine_path.read_text(encoding="utf-8")
    assert "_lock_manager.acquire(" not in content, (
        "game_engine.py should use _trace_lock_guard instead of calling _lock_manager.acquire directly"
    )
