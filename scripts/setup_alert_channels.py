#!/usr/bin/env python3
"""Guide operators through provisioning Telegram alert channels."""

from __future__ import annotations

import textwrap


def main() -> None:
    """Print the manual steps required to bootstrap alert chat groups."""

    message = textwrap.dedent(
        """
        ================= TELEGRAM ALERT CHANNEL SETUP =================

        1. Create the three group chats from any Telegram client:
           • Critical Security Alerts (`poker-alerts-critical`)
           • Operational Alerts (`poker-alerts-ops`)
           • Security Digest (`poker-alerts-digest`)

        2. Add the poker bot user (@PokerHardeningBot) to each group and promote
           it to admin so it can post alerts without interruption.

        3. Disable the "Group Privacy" setting for the bot in each chat. This is
           required so Telegram exposes messages to the bot when you run the
           discovery script.

        4. Post a welcome message in every group that includes the string
           `/start`. This ensures Telegram forwards the update to the bot, which
           allows the discovery script to capture the chat IDs.

        5. Once all groups are configured, execute `python scripts/get_chat_ids.py`
           from the project root. The script will output the chat identifiers you
           must copy into the `.env` file (CRITICAL_CHAT_ID, OPERATIONAL_CHAT_ID,
           DIGEST_CHAT_ID).

        6. Commit the configuration by updating `.env` (or the deployment
           secrets store) with the discovered IDs and redeploy the stack with
           `docker-compose --env-file .env up -d`.

        Need help? See docs/alerting.md for a more detailed runbook.
        =================================================================
        """
    ).strip("\n")

    print(message)


if __name__ == "__main__":
    main()
