import os
import sys
import re
import logging
from contextvars import ContextVar
from typing import Optional

# ContextVar for tracking request ID across async tasks
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

def set_request_id(req_id: str) -> None:
    request_id_ctx.set(req_id)

def get_request_id() -> str:
    return request_id_ctx.get()

def sanitize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """
    Masks credentials in proxy URLs (e.g. http://user:secret@host:port -> http://user:***@host:port)
    """
    if not proxy_url:
        return None
    url_str = str(proxy_url)
    return re.sub(r'(://[^:]+:)[^@]+(@)', r'\1***\2', url_str)

class RequestIdFilter(logging.Filter):
    """
    Injects request_id context into log records.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True

class CustomLogFormatter(logging.Formatter):
    """
    Custom log formatter with clean timestamp, level, request ID context, and logger name.
    """
    def format(self, record: logging.LogRecord) -> str:
        req_id = getattr(record, 'request_id', '-')
        if req_id and req_id != '-':
            record.req_prefix = f"[{req_id}] "
        else:
            record.req_prefix = ""
        return super().format(record)

def setup_logging(log_level_str: Optional[str] = None):
    """
    Configures application-wide logging with stdout stream, custom formatting, and request context filtering.
    """
    level_name = (log_level_str or os.getenv("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s | %(levelname)-8s | %(req_prefix)s%(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    formatter = CustomLogFormatter(fmt=fmt, datefmt=datefmt)

    # Force stdout handler for unbuffered Docker log capture
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(RequestIdFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = [stdout_handler]

    # Adjust noisy third-party loggers unless running at DEBUG level
    if level > logging.DEBUG:
        for lib in ["urllib3", "asyncio", "playwright", "curl_cffi", "websockets"]:
            logging.getLogger(lib).setLevel(logging.WARNING)

    # Align uvicorn logger levels
    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
