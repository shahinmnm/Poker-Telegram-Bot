"""Prometheus metrics HTTP server."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_metrics_server_started = False


def start_metrics_server(port: int = 8000, host: Optional[str] = None) -> bool:
    """Start Prometheus metrics HTTP server on the given port.

    Args:
        port: Port to listen on (default: 8000)
        host: Host/interface to bind the metrics server to. ``None`` will use the
            Prometheus client's default of listening on all interfaces.

    Returns:
        True if server started successfully, False otherwise
    """
    global _metrics_server_started

    if _metrics_server_started:
        logger.warning("Metrics server already started")
        return True

    try:
        from prometheus_client import start_http_server
        listen_host = "" if host is None else host
        start_http_server(port, addr=listen_host)
        _metrics_server_started = True
        bind_label = listen_host or "0.0.0.0"
        logger.info(
            "✅ Prometheus metrics server started",
            extra={"metrics_host": bind_label, "metrics_port": port},
        )
        return True
    except ImportError:
        logger.warning("prometheus_client not installed, metrics server disabled")
        return False
    except OSError as e:
        logger.error(f"Failed to start metrics server on port {port}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error starting metrics server: {e}")
        return False


def is_metrics_server_running() -> bool:
    """Check if metrics server is running."""
    return _metrics_server_started

