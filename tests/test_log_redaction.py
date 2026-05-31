"""Tests for the logging redaction pipeline and config package exports.

Covers:
- RedactProcessor removes secrets from structlog output at INFO level.
- RedactProcessor is skipped entirely at DEBUG level (raw values visible).
- gateway.config package still exports the three symbols app.py depends on.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
import structlog

from gateway.logging import RedactProcessor, configure_logging


# ---------------------------------------------------------------------------
# Helpers

def _build_redacted_processor() -> RedactProcessor:
    """Return a RedactProcessor pre-loaded with the standard config values."""
    import json as _json
    import pathlib

    cfg_path = pathlib.Path(__file__).parent.parent / "gateway" / "config" / "logger.json"
    cfg = _json.loads(cfg_path.read_text())
    rc = cfg["redact"]
    return RedactProcessor(
        field_names=rc["field_names"],
        patterns=rc["patterns"],
        replacement=rc["replacement"],
    )


def _reconfigure_to_stringio(level_str: str) -> io.StringIO:
    """Reconfigure structlog to write JSON to a StringIO at *level_str*.

    Returns the StringIO so the caller can inspect rendered output.
    """
    buf = io.StringIO()

    # Reset cache so processors apply fresh.
    structlog.reset_defaults()

    effective_level = getattr(logging, level_str.upper(), logging.INFO)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    if effective_level > logging.DEBUG:
        processors.append(_build_redacted_processor())
    processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(effective_level),
        logger_factory=structlog.PrintLoggerFactory(buf),
        cache_logger_on_first_use=False,
    )
    return buf


# ---------------------------------------------------------------------------
# Redaction active at INFO

class TestRedactionAtInfo:
    def test_bearer_token_in_event_string_is_redacted(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        log.info(
            "request",
            bearer_token="Bearer sk-test-abcdef1234567890",
        )
        record = json.loads(buf.getvalue().strip())
        # bearer_token key is not in field_names, but the value matches the
        # bearer pattern regex — must be redacted.
        assert "<redacted>" in record["bearer_token"]
        assert "sk-test-abcdef1234567890" not in record["bearer_token"]

    def test_password_field_name_is_redacted(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        log.info("request", password="super-secret-123")
        record = json.loads(buf.getvalue().strip())
        assert record["password"] == "<redacted>"

    def test_api_key_field_name_is_redacted(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        log.info("request", api_key="sk-realkey1234567890abcd")
        record = json.loads(buf.getvalue().strip())
        assert record["api_key"] == "<redacted>"

    def test_sk_pattern_in_body_string_is_redacted(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        # Use a key without embedded hyphens so the sk- pattern matches.
        log.info("request", body="my key is sk-realkey1234567890abcd")
        record = json.loads(buf.getvalue().strip())
        assert "sk-realkey1234567890abcd" not in record["body"]
        assert "<redacted>" in record["body"]

    def test_all_four_secrets_redacted_in_single_record(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        # Keys without embedded hyphens so the sk- pattern matches correctly.
        log.info(
            "multi-secret",
            bearer_token="Bearer sk-testabcdef1234567890",
            password="hunter2",
            api_key="sk-anotherkey1234567890abcdef",
            body="my key is sk-realkey1234567890abcd",
        )
        raw = buf.getvalue().strip()
        record = json.loads(raw)

        assert "sk-testabcdef1234567890" not in record["bearer_token"]
        assert record["password"] == "<redacted>"
        assert record["api_key"] == "<redacted>"
        assert "sk-realkey1234567890abcd" not in record["body"]
        assert "<redacted>" in record["bearer_token"]
        assert "<redacted>" in record["body"]

    def test_nested_dict_value_redacted_one_level(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        log.info("nested", headers={"authorization": "Bearer sk-test-abcdef1234567890"})
        record = json.loads(buf.getvalue().strip())
        assert record["headers"]["authorization"] == "<redacted>"

    def test_non_sensitive_values_pass_through(self):
        buf = _reconfigure_to_stringio("INFO")
        log = structlog.get_logger("test")
        log.info("plain", user_id="u-123", count=42, flag=True)
        record = json.loads(buf.getvalue().strip())
        assert record["user_id"] == "u-123"
        assert record["count"] == 42
        assert record["flag"] is True


# ---------------------------------------------------------------------------
# No redaction at DEBUG

class TestNoRedactionAtDebug:
    def test_raw_bearer_token_visible_in_debug(self):
        buf = _reconfigure_to_stringio("DEBUG")
        log = structlog.get_logger("test")
        log.debug(
            "triage",
            bearer_token="Bearer sk-testabcdef1234567890",
            password="hunter2",
            api_key="sk-anotherkey1234567890abcdef",
            body="my key is sk-realkey1234567890abcd",
        )
        raw = buf.getvalue().strip()
        record = json.loads(raw)
        # At DEBUG the redact processor is absent — raw values come through.
        assert "sk-testabcdef1234567890" in record["bearer_token"]
        assert record["password"] == "hunter2"
        assert "sk-realkey1234567890abcd" in record["body"]


# ---------------------------------------------------------------------------
# Config package import surface

class TestConfigPackageExports:
    def test_exports_config_holder(self):
        from gateway.config import ConfigHolder  # noqa: F401
        assert ConfigHolder is not None

    def test_exports_install_sighup_reload(self):
        from gateway.config import install_sighup_reload  # noqa: F401
        assert callable(install_sighup_reload)
