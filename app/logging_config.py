import os
import sys
import re
import logging
from contextvars import ContextVar
from typing import Optional

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

def set_request_id(req_id: str) -> None:
    request_id_ctx.set(req_id)

def get_request_id() -> str:
    return request_id_ctx.get()

# Userinfo stops at whitespace so free text after a URL (exception messages) isn't swallowed.
_CREDENTIALS_RE = re.compile(r'(://[^\s:/@]+:)[^\s@/]+(@)')


def sanitize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """http://user:secret@host:port -> http://user:***@host:port"""
    if not proxy_url:
        return None
    return _CREDENTIALS_RE.sub(r'\1***\2', str(proxy_url))

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True

class CustomLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        req_id = getattr(record, 'request_id', '-')
        if req_id and req_id != '-':
            record.req_prefix = f"[{req_id}] "
        else:
            record.req_prefix = ""
        # Exception text and tracebacks often embed the proxy URL verbatim; mask credentials in every line.
        return _CREDENTIALS_RE.sub(r'\1***\2', super().format(record))

def setup_logging(log_level_str: Optional[str] = None):
    level_name = (log_level_str or os.getenv("LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s | %(levelname)-8s | %(req_prefix)s%(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    formatter = CustomLogFormatter(fmt=fmt, datefmt=datefmt)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.addFilter(RequestIdFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = [stdout_handler]

    if level > logging.DEBUG:
        for lib in ["urllib3", "asyncio", "playwright", "curl_cffi", "websockets"]:
            logging.getLogger(lib).setLevel(logging.WARNING)

    logging.getLogger("uvicorn").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)
    logging.getLogger("uvicorn.error").setLevel(level)
