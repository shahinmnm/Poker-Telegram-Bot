import logging
import json

from pokerapp.utils.datetime_utils import utc_isoformat


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": utc_isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("chat_id", "message_id", "request_params", "error_type"):
            if key in record.__dict__:
                log_record[key] = record.__dict__[key]

        standard_attrs = {
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
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in log_record or key in standard_attrs:
                continue
            log_record[key] = value
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    if logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler])
