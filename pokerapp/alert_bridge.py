#!/usr/bin/env python3
"""Minimal Alertmanager to Telegram bridge."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from aiohttp import ClientSession, ClientTimeout, web

from pokerapp.logging_config import setup_logging

LOGGER = logging.getLogger("pokerapp.alert_bridge")

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "warning": "🟡",
    "info": "🔵",
}

_RESOLVED_EMOJI = "✅"

_MARKDOWN_ESCAPE = str.maketrans({
    "_": r"\_",
    "*": r"\*",
    "[": r"\[",
    "]": r"\]",
    "(": r"\(",
    ")": r"\)",
    "~": r"\~",
    "`": r"\`",
    ">": r"\>",
    "#": r"\#",
    "+": r"\+",
    "-": r"\-",
    "=": r"\=",
    "|": r"\|",
    "{": r"\{",
    "}": r"\}",
    ".": r"\.",
    "!": r"\!",
})


def _escape_markdown(value: str) -> str:
    return value.translate(_MARKDOWN_ESCAPE)


def _escape_link_target(value: str) -> str:
    return value.replace("(", "%28").replace(")", "%29").replace(" ", "%20")


def _parse_timestamp(timestamp: Optional[str]) -> str:
    if not timestamp:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    try:
        if timestamp.endswith("Z"):
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(timestamp)
        dt = dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return dt.strftime("%Y-%m-%d %H:%M:%SZ")


@dataclass
class FormattedAlert:
    chat_id: str
    body: str
    severity: str
    alert_name: str


class AlertBridge:
    def __init__(self) -> None:
        self._bot_token = os.environ.get("POKERBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not self._bot_token:
            raise RuntimeError("POKERBOT_TOKEN environment variable must be set for alert bridge")

        self._admin_chat_id = os.environ.get("ADMIN_CHAT_ID")
        if not self._admin_chat_id:
            raise RuntimeError("ADMIN_CHAT_ID environment variable must be set for alert bridge")
        self._api_base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self._session: Optional[ClientSession] = None
        self._alerts_processed = 0
        self._alerts_failed = 0
        self._rate_limiter = TelegramRateLimiter()

    async def startup(self, app: web.Application) -> None:
        timeout = ClientTimeout(total=10)
        self._session = ClientSession(timeout=timeout)
        LOGGER.info(
            "Alert bridge initialised",
            extra={
                "telegram_api_base": self._api_base,
                "admin_chat_id": self._admin_chat_id,
                "mode": "single_admin",
            },
        )

    async def cleanup(self, app: web.Application) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def health(self, _: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def metrics(self, _: web.Request) -> web.Response:
        body = "\n".join(
            [
                "# HELP alert_bridge_alerts_processed_total Alerts processed by the bridge",
                "# TYPE alert_bridge_alerts_processed_total counter",
                f"alert_bridge_alerts_processed_total {self._alerts_processed}",
                "# HELP alert_bridge_alerts_failed_total Telegram delivery failures",
                "# TYPE alert_bridge_alerts_failed_total counter",
                f"alert_bridge_alerts_failed_total {self._alerts_failed}",
            ]
        )
        return web.Response(
            text=body + "\n",
            content_type="text/plain; version=0.0.4",
            charset="utf-8",
        )

    async def handle_alertmanager(self, request: web.Request) -> web.Response:
        payload = await request.json()
        alerts = payload.get("alerts", [])
        LOGGER.info(
            "Alertmanager webhook received",
            extra={
                "alerts_count": len(alerts),
                "status": payload.get("status"),
            },
        )

        formatted_alerts = self._format_alerts(alerts)
        if formatted_alerts:
            await self._dispatch(formatted_alerts)
        return web.json_response({"status": "ok"})

    def _format_alerts(self, alerts: Sequence[Dict[str, Any]]) -> List[FormattedAlert]:
        formatted: List[FormattedAlert] = []
        for alert in alerts:
            labels = {k: str(v) for k, v in alert.get("labels", {}).items()}
            severity = labels.get("severity", "info").lower()
            status = str(alert.get("status", "firing")).lower()
            alert_name = labels.get("alertname", "Unnamed Alert")
            component = labels.get("component", "unknown")
            annotations = {k: str(v) for k, v in alert.get("annotations", {}).items()}
            summary = annotations.get("summary") or ""
            description = annotations.get("description") or ""
            runbook = annotations.get("runbook")
            timestamp = _parse_timestamp(alert.get("startsAt") if status != "resolved" else alert.get("endsAt"))

            body = self._build_message(
                severity=severity,
                status=status,
                alert_name=alert_name,
                component=component,
                summary=summary,
                description=description,
                runbook=runbook,
                timestamp=timestamp,
            )

            formatted.append(
                FormattedAlert(
                    chat_id=str(self._admin_chat_id),
                    body=body,
                    severity=severity,
                    alert_name=alert_name,
                )
            )

        return formatted

    def _build_message(
        self,
        *,
        severity: str,
        status: str,
        alert_name: str,
        component: str,
        summary: str,
        description: str,
        runbook: Optional[str],
        timestamp: str,
    ) -> str:
        emoji = _SEVERITY_EMOJI.get(severity, "🔵")
        if status == "resolved":
            emoji = _RESOLVED_EMOJI
        lines = [f"{emoji} *{_escape_markdown(alert_name)}*"]
        lines.append(f"*Severity:* `{_escape_markdown(severity.upper())}`")
        lines.append(f"*Status:* `{_escape_markdown(status.upper())}`")
        lines.append(f"*Component:* `{_escape_markdown(component)}`")
        if summary:
            lines.append(f"*Summary:* {_escape_markdown(summary)}")
        if description:
            lines.append(f"*Description:* {_escape_markdown(description)}")
        if runbook:
            lines.append(
                f"*Runbook:* [{_escape_markdown('View runbook')}]({_escape_link_target(runbook)})"
            )
        else:
            lines.append("*Runbook:* _(not provided)_")
        lines.append(f"*Timestamp:* `{_escape_markdown(timestamp)}`")
        return "\n".join(lines)


    async def _dispatch(self, alerts: Sequence[FormattedAlert]) -> None:
        if not self._session:
            raise RuntimeError("Client session not initialised")

        tasks = []
        for alert in alerts:
            if not self._rate_limiter.check_limit(alert.chat_id):
                LOGGER.warning(
                    "Rate limit exceeded for chat",
                    extra={"chat_id": alert.chat_id, "alert_name": alert.alert_name},
                )
                self._alerts_failed += 1
                continue

            LOGGER.info(
                "Forwarding alert to Telegram",
                extra={"chat_id": alert.chat_id, "severity": alert.severity, "alert_name": alert.alert_name},
            )
            tasks.append(self._dispatch_with_retry(alert))

        if not tasks:
            return

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                self._alerts_failed += 1
                LOGGER.error(
                    "Exception while sending alert to Telegram",
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def _dispatch_with_retry(self, alert: FormattedAlert, max_retries: int = 3) -> bool:
        if not self._session:
            raise RuntimeError("Client session not initialised")

        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": alert.chat_id,
            "text": alert.body,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }

        for attempt in range(max_retries):
            try:
                async with self._session.post(url, json=payload) as response:
                    text = await response.text()

                    if response.status == 429:
                        retry_after = int(response.headers.get("Retry-After", "60"))
                        LOGGER.warning(
                            "Telegram rate limit hit, retrying",
                            extra={"retry_after": retry_after, "attempt": attempt + 1, "chat_id": alert.chat_id},
                        )
                        await asyncio.sleep(retry_after)
                        continue

                    if 500 <= response.status < 600 and attempt < max_retries - 1:
                        backoff = 2**attempt
                        LOGGER.warning(
                            "Telegram server error, retrying",
                            extra={"status": response.status, "backoff": backoff, "attempt": attempt + 1},
                        )
                        await asyncio.sleep(backoff)
                        continue

                    if response.status >= 400:
                        self._alerts_failed += 1
                        LOGGER.error(
                            "Telegram delivery failed permanently",
                            extra={
                                "chat_id": alert.chat_id,
                                "status": response.status,
                                "body": text[:200],
                                "alert_name": alert.alert_name,
                            },
                        )
                        return False

                    self._alerts_processed += 1
                    LOGGER.info(
                        "Telegram delivery succeeded",
                        extra={"chat_id": alert.chat_id, "alert_name": alert.alert_name},
                    )
                    return True

            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    backoff = 2**attempt
                    LOGGER.warning(
                        "Telegram timeout, retrying",
                        extra={"attempt": attempt + 1, "backoff": backoff, "chat_id": alert.chat_id},
                    )
                    await asyncio.sleep(backoff)
                    continue
                self._alerts_failed += 1
                LOGGER.error("Telegram timeout after max retries", extra={"chat_id": alert.chat_id})
                return False

            except Exception as exc:  # pragma: no cover - network failure
                self._alerts_failed += 1
                LOGGER.exception(
                    "Exception while sending alert to Telegram",
                    extra={"chat_id": alert.chat_id, "alert_name": alert.alert_name, "error": repr(exc)},
                )
                return False

        return False


class TelegramRateLimiter:
    """Enforce per-chat rate limits to prevent Telegram API bans."""

    def __init__(self, max_per_chat: int = 20, window: int = 60) -> None:
        self.max_per_chat = max_per_chat
        self.window = window
        self._chat_timestamps: Dict[str, List[float]] = defaultdict(list)

    def check_limit(self, chat_id: str) -> bool:
        """Return True if request is allowed, False if rate limited."""

        now = time.time()
        timestamps = self._chat_timestamps[chat_id]
        self._chat_timestamps[chat_id] = [ts for ts in timestamps if now - ts < self.window]

        if len(self._chat_timestamps[chat_id]) >= self.max_per_chat:
            return False

        self._chat_timestamps[chat_id].append(now)
        return True
def create_app() -> web.Application:
    setup_logging()
    bridge = AlertBridge()
    app = web.Application()
    app.router.add_get("/health", bridge.health)
    app.router.add_get("/metrics", bridge.metrics)
    app.router.add_post("/webhook/alertmanager", bridge.handle_alertmanager)
    app.on_startup.append(bridge.startup)
    app.on_cleanup.append(bridge.cleanup)
    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("ALERT_BRIDGE_PORT", "9099"))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
