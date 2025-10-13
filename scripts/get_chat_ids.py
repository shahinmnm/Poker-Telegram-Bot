#!/usr/bin/env python3
"""Discover Telegram chat identifiers for admin alert delivery."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Iterable, Optional, Tuple

API_BASE = "https://api.telegram.org"


def _load_token(cli_token: Optional[str]) -> str:
    token = cli_token or os.environ.get("POKERBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Telegram bot token is required. Pass --token or set POKERBOT_TOKEN/TELEGRAM_BOT_TOKEN."
        )
    return token


def _fetch_updates(token: str, offset: Optional[int] = None) -> Dict[str, object]:
    query: Dict[str, object] = {"timeout": "0", "allowed_updates": json.dumps(["message", "my_chat_member"])}
    if offset is not None:
        query["offset"] = str(offset)
    url = f"{API_BASE}/bot{token}/getUpdates?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=15) as response:  # nosec B310
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def _iter_updates(result: Dict[str, object]) -> Iterable[Dict[str, object]]:
    for update in result.get("result", []):
        yield update


def _extract_chat(update: Dict[str, object]) -> Optional[Tuple[int, str, str]]:
    if "message" in update and update["message"] and "chat" in update["message"]:
        chat = update["message"]["chat"]
    elif "my_chat_member" in update and update["my_chat_member"]:
        chat = update["my_chat_member"].get("chat")
    else:
        return None

    if not chat:
        return None

    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if chat_type not in ["private", "group", "supergroup"]:
        return None

    if chat_type == "private":
        title = chat.get("username") or chat.get("first_name") or "(unknown title)"
    else:
        title = chat.get("title") or "(unknown title)"
    if chat_id is None:
        return None
    return int(chat_id), str(title), str(chat_type)


def _print_summary(chats: Dict[int, Tuple[str, str]]) -> None:
    if not chats:
        print("No chats detected. Send /start to the bot from your personal account.")
        return

    print("Discovered chat identifiers:\n")
    for chat_id, (title, chat_type) in sorted(chats.items()):
        human_type = "Private" if chat_type == "private" else "Group"
        print(f"  • [{human_type}] {title}: {chat_id}")

    private_chats = [cid for cid, (_, ctype) in chats.items() if ctype == "private"]
    if private_chats:
        suggested_admin = private_chats[0]
        print(f"\n✅ Suggested admin chat ID: {suggested_admin}")
        print("\nUpdate your .env file with:")
        print(f"  ADMIN_CHAT_ID={suggested_admin}")
    else:
        print("\n⚠️ No private chats found. Send /start to the bot from your personal account.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", help="Telegram bot token; overrides environment variables")
    parser.add_argument(
        "--mark-read",
        action="store_true",
        help="Advance the getUpdates offset so Telegram stops resending historical updates",
    )
    args = parser.parse_args()

    token = _load_token(args.token)
    try:
        raw_updates = _fetch_updates(token)
    except urllib.error.HTTPError as exc:  # pragma: no cover - network failure
        print(f"HTTP error from Telegram API: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:  # pragma: no cover - network failure
        print(f"Failed to reach Telegram API: {exc}", file=sys.stderr)
        raise SystemExit(1)

    chats: Dict[int, Tuple[str, str]] = {}
    max_update_id: Optional[int] = None
    for update in _iter_updates(raw_updates):
        chat_info = _extract_chat(update)
        if chat_info:
            chat_id, title, chat_type = chat_info
            chats.setdefault(chat_id, (title, chat_type))
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            if max_update_id is None or update_id > max_update_id:
                max_update_id = update_id

    _print_summary(chats)

    if args.mark_read and max_update_id is not None:
        _fetch_updates(token, offset=max_update_id + 1)
        print("Advanced Telegram update offset; historical updates acknowledged.")


if __name__ == "__main__":
    main()
