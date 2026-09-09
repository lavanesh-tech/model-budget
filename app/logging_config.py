"""
Structured (JSON) logging with a per-request correlation ID.

Design: a single ContextVar holds the current request's ID. A
logging.Filter reads it and stamps every LogRecord with `request_id`
automatically -- application code calls `logger.info(...)` normally and
never has to pass the ID by hand, so it can't be forgotten on one code
path and present on another. The ID is set once per request by
RequestIDMiddleware (see app.main) and is naturally scoped correctly
across concurrent requests: Python's contextvars are per-async-task (and
per-thread for the sync helpers this project already runs via
asyncio.to_thread -- ContextVar values ARE copied into the thread started
by to_thread, so log lines emitted from inside e.g. _run_txn1_sync still
carry the correct request_id).

Safety: this module never touches or knows about prompts, completions,
API keys, secret hashes, or authorization headers -- it only formats
whatever fields the CALLER explicitly passes via `extra={...}`. Keeping
prompts/secrets out of logs is enforced by discipline at each call site
(see app.api.chat_completions), not by this module, which is
intentionally dumb: it formats what it's given.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Fields that come from Python's own LogRecord machinery and should NOT
# be re-emitted from `extra` (avoids duplicate/confusing keys); anything
# else the caller passes via extra={...} is included verbatim.
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None
).__dict__.keys()) | {"message", "asctime", "request_id"}


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def get_request_id() -> str | None:
    return _request_id_var.get()


def new_request_id() -> str:
    return str(uuid.uuid4())


class _RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call more than once (e.g. once at import time
    and again explicitly in a test), replacing any prior configuration
    rather than stacking duplicate handlers.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JSONFormatter())
    handler.addFilter(_RequestIDFilter())
    root.addHandler(handler)

    # Keep third-party loggers (uvicorn, httpx, openai's own SDK logger)
    # at a less noisy default unless the app's own level is more verbose
    # than that -- avoids drowning application logs in library chatter,
    # without ever silencing them below what the operator asked for.
    for noisy_logger in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy_logger).setLevel(max(getattr(logging, level.upper()), logging.WARNING))
