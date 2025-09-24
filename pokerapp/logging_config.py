import json
import logging
from typing import Any, Dict

from pokerapp.utils.time_utils import now_utc


class ContextJsonFormatter(logging.Formatter):
    """Serialise log records into structured JSON for downstream systems."""

    #: Attributes defined by :class:`logging.LogRecord` that should be ignored
    #: when collecting application specific extras.
    _STANDARD_ATTRS = {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "stacklevel",
    }

    #: Common structured keys emitted by the poker bot.
    _COMMON_KEYS = (
        "chat_id",
        "message_id",
        "user_id",
        "game_id",
        "stage",
        "request_params",
        "error_type",
        "method",
        "content_hash",
        "retry_after",
        "delay",
        "category",
        "action",
    )

    def _coerce_value(self, value: Any) -> Any:
        """Best-effort conversion of ``value`` to a JSON serialisable type."""

        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int, float)):
            return getattr(value, "value")
        if isinstance(value, dict):
            return {k: self._coerce_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._coerce_value(item) for item in value]
        return repr(value)

    def format(self, record: logging.LogRecord) -> str:
        log_record: Dict[str, Any] = {
            "timestamp": now_utc().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key in self._COMMON_KEYS:
            if key in record.__dict__:
                log_record[key] = self._coerce_value(record.__dict__[key])

        debug_flag = record.__dict__.get("debug_mode")
        if debug_flag is not None:
            log_record["debug_mode"] = bool(debug_flag)

        extra_payload: Dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in log_record or key in self._STANDARD_ATTRS:
                continue
            extra_payload[key] = self._coerce_value(value)
        if extra_payload:
            log_record["extra"] = extra_payload

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record, ensure_ascii=False)


def setup_logging(level: int = logging.INFO, debug_mode: bool = False) -> None:
    """Initialise root logging with the structured JSON formatter."""

    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(ContextJsonFormatter())
        root_logger.addHandler(handler)

    root_logger.setLevel(logging.DEBUG if debug_mode else level)

    debug_trace_logger = logging.getLogger("pokerbot.debug_trace")
    if not debug_trace_logger.handlers:
        debug_handler = logging.StreamHandler()
        debug_handler.setFormatter(ContextJsonFormatter())
        debug_trace_logger.addHandler(debug_handler)
    debug_trace_logger.setLevel(logging.DEBUG if debug_mode else level)
    debug_trace_logger.propagate = False
