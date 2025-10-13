#!/usr/bin/env python3
"""Minimal Alertmanager to Telegram bridge."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

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

        self._chat_ids = {
            "critical": os.environ.get("CRITICAL_CHAT_ID"),
            "operational": os.environ.get("OPERATIONAL_CHAT_ID"),
            "digest": os.environ.get("DIGEST_CHAT_ID"),
        }
        self._api_base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        self._session: Optional[ClientSession] = None
        self._alerts_processed = 0
        self._alerts_failed = 0

    async def startup(self, app: web.Application) -> None:
        timeout = ClientTimeout(total=10)
        self._session = ClientSession(timeout=timeout)
        LOGGER.info(
            "Alert bridge initialised",
            extra={
                "telegram_api_base": self._api_base,
                "has_critical_chat": bool(self._chat_ids.get("critical")),
                "has_operational_chat": bool(self._chat_ids.get("operational")),
                "has_digest_chat": bool(self._chat_ids.get("digest")),
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
        return web.Response(text=body + "\n", content_type="text/plain; version=0.0.4; charset=utf-8")

    async def handle_alertmanager(self, request: web.Request) -> web.Response:
        channel_hint = request.rel_url.query.get("channel")
        payload = await request.json()
        alerts = payload.get("alerts", [])
        LOGGER.info(
            "Alertmanager webhook received",
            extra={
                "alerts_count": len(alerts),
                "status": payload.get("status"),
                "channel_hint": channel_hint,
            },
        )

        formatted_alerts = self._format_alerts(alerts, channel_hint)
        if formatted_alerts:
            await self._dispatch(formatted_alerts)
        else:
            LOGGER.info("No alerts to dispatch after formatting", extra={"alerts_count": len(alerts)})
        return web.json_response({"status": "ok"})

    def _format_alerts(self, alerts: Sequence[Dict[str, Any]], channel_hint: Optional[str]) -> List[FormattedAlert]:
        formatted: List[FormattedAlert] = []
        digest_alerts: List[Dict[str, Any]] = []
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

            target_channel = self._resolve_channel(severity, labels, channel_hint)
            if target_channel == "digest":
                digest_alerts.append(
                    {
                        "alert_name": alert_name,
                        "severity": severity,
                        "status": status,
                        "component": component,
                        "summary": summary,
                        "description": description,
                        "runbook": runbook,
                        "timestamp": timestamp,
                    }
                )
                continue

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
            chat_id = self._chat_ids.get(target_channel)
            if not chat_id:
                LOGGER.warning(
                    "Skipping alert delivery because chat id is not configured",
                    extra={"target_channel": target_channel, "alert_name": alert_name},
                )
                continue
            formatted.append(FormattedAlert(chat_id=str(chat_id), body=body, severity=severity, alert_name=alert_name))

        if digest_alerts:
            chat_id = self._chat_ids.get("digest")
            if not chat_id:
                LOGGER.warning(
                    "Digest alerts dropped because DIGEST_CHAT_ID is missing",
                    extra={"alert_count": len(digest_alerts)},
                )
            else:
                for chunk in _chunk_alerts(digest_alerts, size=20):
                    body = self._build_digest(chunk)
                    formatted.append(FormattedAlert(chat_id=str(chat_id), body=body, severity="info", alert_name="digest"))

        return formatted

    def _resolve_channel(
        self, severity: str, labels: Dict[str, str], channel_hint: Optional[str]
    ) -> str:
        explicit = labels.get("notification_channel") or channel_hint
        if explicit in {"critical", "operational", "digest"}:
            return explicit
        if severity == "critical":
            return "critical"
        if severity in {"high", "warning"}:
            return "operational"
        return "digest"

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

    def _build_digest(self, alerts: Sequence[Dict[str, str]]) -> str:
        header = "🔵 *Security Digest*"
        lines = [header]
        for idx, alert in enumerate(alerts, start=1):
            lines.append(f"*{idx}. {_escape_markdown(alert['alert_name'])}*")
            severity_icon = _SEVERITY_EMOJI.get(alert["severity"], "🔵")
            status_icon = _RESOLVED_EMOJI if alert["status"] == "resolved" else severity_icon
            lines.append(f"  • Severity: `{_escape_markdown(alert['severity'].upper())}`")
            lines.append(f"  • Status: {status_icon} `{_escape_markdown(alert['status'].upper())}`")
            lines.append(f"  • Component: `{_escape_markdown(alert['component'])}`")
            if alert.get("summary"):
                lines.append(f"  • Summary: {_escape_markdown(alert['summary'])}")
            if alert.get("description"):
                lines.append(f"  • Description: {_escape_markdown(alert['description'])}")
            if alert.get("runbook"):
                lines.append(
                    f"  • Runbook: [{_escape_markdown('View runbook')}]({_escape_link_target(alert['runbook'])})"
                )
            lines.append(f"  • Timestamp: `{_escape_markdown(alert['timestamp'])}`")
        return "\n".join(lines)

    async def _dispatch(self, alerts: Sequence[FormattedAlert]) -> None:
        if not self._session:
            raise RuntimeError("Client session not initialised")
        url = f"{self._api_base}/bot{self._bot_token}/sendMessage"
        for alert in alerts:
            payload = {"chat_id": alert.chat_id, "text": alert.body, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}
            LOGGER.info(
                "Forwarding alert to Telegram",
                extra={"chat_id": alert.chat_id, "severity": alert.severity, "alert_name": alert.alert_name},
            )
            try:
                async with self._session.post(url, json=payload) as response:
                    text = await response.text()
                    if response.status >= 400:
                        self._alerts_failed += 1
                        LOGGER.error(
                            "Telegram delivery failed",
                            extra={
                                "chat_id": alert.chat_id,
                                "status": response.status,
                                "body": text,
                                "alert_name": alert.alert_name,
                            },
                        )
                    else:
                        self._alerts_processed += 1
                        LOGGER.info(
                            "Telegram delivery succeeded",
                            extra={"chat_id": alert.chat_id, "alert_name": alert.alert_name},
                        )
            except Exception as exc:  # pragma: no cover - network failure
                self._alerts_failed += 1
                LOGGER.exception(
                    "Exception while sending alert to Telegram",
                    extra={"chat_id": alert.chat_id, "alert_name": alert.alert_name, "error": repr(exc)},
                )


def _chunk_alerts(items: Sequence[Dict[str, Any]], size: int) -> Iterable[Sequence[Dict[str, Any]]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


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
