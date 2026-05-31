"""structlog config — JSON to stdout, one line per event.

Includes a `RedactProcessor` that strips tokens/secrets from log records
unless the effective log level is DEBUG (where operators need raw output
for triage).  Configuration is loaded from ``gateway/config/logger.json``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import structlog

# Path to the logger configuration file, relative to this file's package.
_LOGGER_JSON = Path(__file__).parent / "config" / "logger.json"


def _load_logger_config() -> dict[str, Any]:
    """Read logger.json; return an empty dict if the file is absent."""
    try:
        return json.loads(_LOGGER_JSON.read_text())
    except Exception:
        return {}


class RedactProcessor:
    """structlog processor that removes secrets from the event dict.

    Redaction strategy (applied in order):
    1. Any key whose *name* matches ``field_names`` (case-insensitive) has
       its value replaced wholesale with ``replacement``.
    2. Every remaining *string* value (plus string values one level deep
       inside nested dicts) has ``patterns`` applied as regex substitutions.

    Values that are not strings or dicts (ints, bools, None, lists) are
    left untouched.
    """

    def __init__(
        self,
        *,
        field_names: list[str],
        patterns: list[str],
        replacement: str,
    ) -> None:
        self._field_names: frozenset[str] = frozenset(n.lower() for n in field_names)
        self._patterns: list[re.Pattern[str]] = [re.compile(p) for p in patterns]
        self._replacement = replacement

    # ------------------------------------------------------------------
    # internal helpers

    def _scrub_string(self, value: str) -> str:
        for pat in self._patterns:
            value = pat.sub(self._replacement, value)
        return value

    def _scrub_value(self, key: str, value: object) -> object:
        """Scrub a single key/value pair."""
        if key.lower() in self._field_names:
            return self._replacement
        if isinstance(value, str):
            return self._scrub_string(value)
        if isinstance(value, dict):
            # One level of recursion — enough for our log shapes.
            return {k: self._scrub_value(k, v) for k, v in value.items()}
        return value

    # ------------------------------------------------------------------
    # structlog processor protocol

    def __call__(
        self,
        logger: object,
        method_name: str,
        event_dict: dict[str, Any],
    ) -> dict[str, Any]:
        return {k: self._scrub_value(k, v) for k, v in event_dict.items()}


def configure_logging(level: str | None = None) -> None:
    """Configure stdlib logging + structlog.

    Priority for log level (highest to lowest):
    1. ``level`` argument (passed from the env-var read in ``app.py``).
    2. ``level`` key in ``gateway/config/logger.json``.
    3. Hard-coded default of ``"INFO"``.
    """
    cfg = _load_logger_config()
    json_level: str = cfg.get("level", "INFO")
    effective_level_str: str = (level or json_level or "INFO").upper()
    effective_level: int = getattr(logging, effective_level_str, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=effective_level,
    )

    # Build the processor pipeline.
    redact_cfg = cfg.get("redact", {})
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]

    # Insert redaction unless the operator explicitly chose DEBUG.
    if effective_level > logging.DEBUG and redact_cfg:
        processors.append(
            RedactProcessor(
                field_names=redact_cfg.get("field_names", []),
                patterns=redact_cfg.get("patterns", []),
                replacement=redact_cfg.get("replacement", "<redacted>"),
            )
        )

    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(effective_level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(name)
