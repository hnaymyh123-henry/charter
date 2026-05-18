"""Structured logging for Charter.

Every key path (fetch + verify, CLI command, MCP tool entry) emits one
log line carrying `charter_id` / `principal_id` / `agent_id` / `outcome`
fields when known. Two formats are supported:

  - `human` (default): one line, key=value style, suitable for `tail -f`
    during development.
  - `json`: one JSON object per line, suitable for production log
    aggregators (Datadog, Loki, Grafana Cloud, etc.).

The format is selected by `CHARTER_LOG_FORMAT` and applied at import
time. Subsequent calls to `configure_logging()` overwrite the existing
handlers, which is fine for tests that need to swap formats.

The logger hierarchy is:

  charter.fetch          - fetch + verify outcomes
  charter.cli.<command>  - CLI command entries
  charter.mcp.<tool>     - MCP tool invocations

Use `get_logger("charter.fetch")` etc. — never call `logging.getLogger`
directly; we want all charter loggers to share the same handler and
filter chain.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

_ROOT_LOGGER_NAME = "charter"

# Standard `LogRecord` attributes — anything else passed via `extra=` is
# considered structured context to include in the output.
_STANDARD_ATTRS = frozenset(
    [
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
        "asctime",
        "taskName",
    ]
)


class _JsonFormatter(logging.Formatter):
    """One JSON object per line. Includes any non-standard LogRecord
    attributes (the `extra=` keyword payload) as top-level fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Surface any extra= fields.
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _HumanFormatter(logging.Formatter):
    """Single-line `ts LEVEL logger msg key=value ...` format."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="seconds")
        extras = []
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            extras.append(f"{key}={value!r}")
        extra_str = (" " + " ".join(extras)) if extras else ""
        base = f"{ts} {record.levelname:5s} {record.name} {record.getMessage()}{extra_str}"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(*, fmt: str | None = None, level: int | str | None = None) -> None:
    """Install Charter's log handler on the root `charter` logger.

    Replaces any previously installed handlers. Safe to call multiple
    times (e.g. from tests that want to switch formats).

    Args:
        fmt:    "human" or "json". Defaults to `CHARTER_LOG_FORMAT` env
                or "human".
        level:  Standard logging level. Defaults to `CHARTER_LOG_LEVEL`
                env or "INFO".
    """
    fmt = fmt or os.environ.get("CHARTER_LOG_FORMAT", "human").lower()
    if fmt not in ("human", "json"):
        fmt = "human"

    resolved_level: int
    if isinstance(level, int):
        resolved_level = level
    elif isinstance(level, str):
        resolved_level = logging.getLevelName(level.upper())
        if not isinstance(resolved_level, int):
            resolved_level = logging.INFO
    else:
        env_level = os.environ.get("CHARTER_LOG_LEVEL", "INFO").upper()
        maybe_level = logging.getLevelName(env_level)
        resolved_level = maybe_level if isinstance(maybe_level, int) else logging.INFO

    root = logging.getLogger(_ROOT_LOGGER_NAME)
    # Drop any existing handlers so repeated configure_logging calls don't
    # accumulate duplicates.
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _HumanFormatter())
    root.addHandler(handler)
    root.setLevel(resolved_level)
    # Propagation stays on so pytest's caplog (and any host application's
    # root-logger handlers) can observe charter events. If you want only
    # our handler to see records, set `charter.propagate = False` from
    # the host code, or filter at the root handler.
    root.propagate = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the `charter` namespace.

    Pass the fully-qualified name (e.g. ``"charter.fetch"``,
    ``"charter.cli.revoke"``, ``"charter.mcp.fetch_charter"``).
    """
    if not name.startswith(_ROOT_LOGGER_NAME):
        raise ValueError(f"Logger name {name!r} must start with {_ROOT_LOGGER_NAME!r}")
    return logging.getLogger(name)


# Auto-configure on import. Tests that need to inspect log output should
# re-call configure_logging() with a `caplog`-friendly setup.
configure_logging()
