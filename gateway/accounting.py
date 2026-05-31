"""Async accounting writer: per-attempt cost+token rows go to Postgres in
batched inserts, never blocking the caller's response.

Design:
* Bounded in-process deque (size ~10k). When full, drop oldest and bump
  `dropped_total` for the alerting hook.
* Background task drains the deque every `flush_interval_ms` or when
  `flush_size` records have queued, whichever comes first.
* `BatchedWriter` is a Protocol so tests inject a fake and the real Postgres
  impl lives in `db.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Protocol

from gateway.models import AttemptRecord


log = logging.getLogger(__name__)


class BatchedWriter(Protocol):
    async def write_batch(self, records: list[AttemptRecord]) -> None: ...


class AccountingQueue:
    def __init__(
        self,
        *,
        writer: BatchedWriter,
        capacity: int = 10_000,
        flush_size: int = 200,
        flush_interval_ms: int = 250,
    ) -> None:
        self._writer = writer
        self._capacity = capacity
        self._flush_size = flush_size
        self._flush_interval_s = flush_interval_ms / 1000.0
        self._buffer: deque[AttemptRecord] = deque(maxlen=None)
        self._dropped_total = 0
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---------------------------------------------------------------- enqueue

    def enqueue(self, rec: AttemptRecord) -> None:
        if len(self._buffer) >= self._capacity:
            try:
                self._buffer.popleft()
                self._dropped_total += 1
            except IndexError:
                pass
        self._buffer.append(rec)
        if len(self._buffer) >= self._flush_size:
            self._wakeup.set()

    @property
    def dropped_total(self) -> int:
        return self._dropped_total

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._drain_loop(), name="accounting-drain")

    async def stop(self) -> None:
        self._stop.set()
        self._wakeup.set()
        if self._task:
            await self._task
            self._task = None
        # Final drain
        await self._flush()

    # ---------------------------------------------------------------- drain

    async def _drain_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=self._flush_interval_s
                )
            except asyncio.TimeoutError:
                pass
            self._wakeup.clear()
            await self._flush()
        # Catch anything that landed between the last wakeup and stop
        await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        try:
            await self._writer.write_batch(batch)
        except Exception:
            log.exception("accounting write_batch failed; %d rows dropped", len(batch))
            self._dropped_total += len(batch)
