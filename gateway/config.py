"""Load + SIGHUP-reload `Config` from a YAML file.

In production we run `Config.model_validate(...)` once at boot. SIGHUP rereads
the same file and atomically swaps the in-memory config. On validation
failure the previous config is kept and the error is logged + alerted on.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

import yaml

from gateway.models import Config


log = logging.getLogger(__name__)


def load_config(path: str | os.PathLike[str]) -> Config:
    raw = Path(path).read_text()
    data = yaml.safe_load(raw)
    return Config.model_validate(data)


class ConfigHolder:
    """Mutable holder so dependents can pin a reference to the holder
    and always read the latest `.value`."""

    def __init__(self, value: Config, source_path: str | None = None) -> None:
        self.value = value
        self.source_path = source_path

    def reload(self) -> None:
        if not self.source_path:
            log.warning("ConfigHolder.reload called but no source_path is set")
            return
        try:
            new = load_config(self.source_path)
            self.value = new
            log.info("config reloaded from %s", self.source_path)
        except Exception:
            log.exception("config reload from %s failed; keeping old config", self.source_path)


def install_sighup_reload(holder: ConfigHolder) -> None:
    def _handler(_sig: int, _frame: object) -> None:  # noqa: D401
        holder.reload()

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (AttributeError, ValueError):
        # Non-POSIX platform or non-main-thread; skip silently.
        pass
