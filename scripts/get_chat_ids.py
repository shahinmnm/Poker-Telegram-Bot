#!/usr/bin/env python3
"""Discover Telegram chat identifiers for the alerting groups."""

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


def _extract_chat(update: Dict[str, object]) -> Optional[Tuple[int, str]]:
    if "message" in update and update["message"] and "chat" in update["message"]:
        chat = update["message"]["chat"]
    elif "my_chat_member" in update and update["my_chat_member"]:
        chat = update["my_chat_member"].get("chat")
    else:
        return None

    if not chat:
        return None

    chat_id = chat.get("id")
    title = chat.get("title") or chat.get("username") or "(unknown title)"
    if chat_id is None:
        return None
    return int(chat_id), str(title)


def _print_summary(chats: Dict[int, str]) -> None:
    if not chats:
        print("No group chats detected. Ensure the bot is added, privacy mode is disabled, and /start was sent.")
        return

    print("Discovered chat identifiers:\n")
    for chat_id, title in sorted(chats.items()):
        print(f"  • {title}: {chat_id}")

    print(
        "\nUpdate your .env file with:\n"
        "  CRITICAL_CHAT_ID=<critical chat id>\n"
        "  OPERATIONAL_CHAT_ID=<operational chat id>\n"
        "  DIGEST_CHAT_ID=<digest chat id>\n"
    )


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

    chats: Dict[int, str] = {}
    max_update_id: Optional[int] = None
    for update in _iter_updates(raw_updates):
        chat_info = _extract_chat(update)
        if chat_info:
            chat_id, title = chat_info
            chats.setdefault(chat_id, title)
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
