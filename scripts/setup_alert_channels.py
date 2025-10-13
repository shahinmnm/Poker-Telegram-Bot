#!/usr/bin/env python3
"""Guide admin through discovering their personal Telegram chat ID."""

from __future__ import annotations

import textwrap


def main() -> None:
    """Print the simplified setup steps for admin-only alerting."""

    message = textwrap.dedent(
        """
        ================= ADMIN ALERT SETUP =================

        This simplified alerting system sends ALL alerts directly to your
        personal Telegram account (no groups required).

        SETUP STEPS:

        1. Find your bot on Telegram (@YourPokerBot) and send it the
           message: /start

           This ensures your personal chat is visible to the bot.

        2. Run the discovery script to find your chat ID:

           python scripts/get_chat_ids.py

           This will output your personal chat ID (example: 123456789).

        3. Add the chat ID to your .env file:

           echo "ADMIN_CHAT_ID=123456789" >> .env

        4. Redeploy the alerting stack:

           docker-compose up -d alert-bridge alertmanager

        5. Send a test alert to verify delivery:

           curl -X POST http://localhost:9093/api/v1/alerts -d '[
             {
               "labels": {"alertname":"TestAlert","severity":"info"},
               "annotations": {"summary":"Setup successful"}
             }
           ]'

           You should receive the test message in your personal Telegram chat.

        DONE! All security alerts will now arrive in your personal DMs.

        Need help? See docs/alerting.md for troubleshooting.
        =====================================================
        """
    ).strip("\n")

    print(message)


if __name__ == "__main__":
    main()
