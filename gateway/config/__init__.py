"""ConfigHolder + SIGHUP-reload wiring.

Config is now loaded from Postgres (via ConfigStore), not a YAML file.
SIGHUP triggers a force-refresh from DB + Redis-cache invalidation.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from gateway.models import Config


log = logging.getLogger(__name__)


class ConfigHolder:
    """Mutable holder so dependents can pin a reference to the holder
    and always read the latest `.value`."""

    def __init__(self, value: Config, config_store=None) -> None:
        self.value = value
        self._config_store = config_store

    def reload(self) -> None:
        """Schedule an async force-refresh from DB.

        SIGHUP handlers run in the main thread; we can't ``await`` here, so we
        schedule the coroutine onto the running event loop.  If no loop is
        running (e.g. during a test that never starts one) the reload is
        skipped and a warning is logged.
        """
        if self._config_store is None:
            log.warning(
                "ConfigHolder.reload called but no config_store is wired — "
                "assign holder._config_store after constructing the ConfigStore"
            )
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("ConfigHolder.reload called outside a running event loop; skipping")
            return

        async def _do_reload() -> None:
            try:
                new = await self._config_store.load_or_refresh(force=True)
                self.value = new
                log.info("config reloaded from database via SIGHUP")
            except Exception:
                log.exception("config reload from DB failed; keeping old config")

        loop.create_task(_do_reload(), name="sighup-config-reload")


def install_sighup_reload(holder: ConfigHolder) -> None:
    def _handler(_sig: int, _frame: object) -> None:  # noqa: D401
        holder.reload()

    try:
        signal.signal(signal.SIGHUP, _handler)
    except (AttributeError, ValueError):
        # Non-POSIX platform or non-main-thread; skip silently.
        pass
