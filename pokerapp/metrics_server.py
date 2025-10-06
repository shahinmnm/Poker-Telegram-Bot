"""Prometheus metrics HTTP server."""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_metrics_server_started = False


def start_metrics_server(port: int = 8000) -> bool:
    """Start Prometheus metrics HTTP server on the given port.
    
    Args:
        port: Port to listen on (default: 8000)
        
    Returns:
        True if server started successfully, False otherwise
    """
    global _metrics_server_started
    
    if _metrics_server_started:
        logger.warning("Metrics server already started")
        return True
    
    try:
        from prometheus_client import start_http_server
        start_http_server(port)
        _metrics_server_started = True
        logger.info(f"✅ Prometheus metrics server started on port {port}")
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

