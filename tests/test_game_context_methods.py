import ast
import pathlib

import pytest

TARGET_METHODS = {
    "send_message",
    "send_photo",
    "send_desk_cards_img",
    "edit_message_text",
    "edit_message_caption",
    "edit_message_media",
    "edit_message_reply_markup",
    "delete_message",
    "answer_callback_query",
    "send_ready_message",
    "send_buttons_message",
    "update_player_action_buttons",
    "update_board_cards",
}


def _load_viewer_class() -> ast.ClassDef:
    path = pathlib.Path("pokerapp/pokerbotview.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "PokerBotViewer":
            return node
    raise AssertionError("PokerBotViewer class not found in pokerapp/pokerbotview.py")


def _extract_methods(cls: ast.ClassDef) -> dict[str, ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _call_uses_game_keyword(call: ast.Call) -> bool:
    return any(isinstance(keyword, ast.keyword) and keyword.arg == "game" for keyword in call.keywords)


@pytest.mark.parametrize("method_name", sorted(TARGET_METHODS))
def test_outbound_helpers_include_game_keyword(method_name: str) -> None:
    cls = _load_viewer_class()
    methods = _extract_methods(cls)
    func = methods.get(method_name)

    if func is None:
        pytest.skip(f"{method_name} helper is not defined in PokerBotViewer")

    kwonly_args = {arg.arg for arg in func.args.kwonlyargs}
    assert "game" in kwonly_args, f"{method_name} is missing keyword-only game parameter"

    timed_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_timed_api_call"
    ]

    assert timed_calls, f"{method_name} should invoke _timed_api_call"

    for call in timed_calls:
        assert _call_uses_game_keyword(call), (
            f"{method_name} _timed_api_call must forward game keyword"
        )
