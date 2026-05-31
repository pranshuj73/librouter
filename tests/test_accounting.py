"""Tests for gateway/accounting.py.

TDD step 11. Verifies bounded queue + batched flush + drop-counter semantics
without touching real Postgres (the writer is a Protocol; we inject a fake).
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.accounting import AccountingQueue, BatchedWriter
from gateway.models import AttemptRecord


pytestmark = pytest.mark.asyncio


def _rec(idx: int = 0, status: str = "ok") -> AttemptRecord:
    return AttemptRecord(
        request_id=f"req-{idx}",
        caller="svc",
        tier="fast",
        provider="openai",
        model="gpt-4o-mini",
        attempt_idx=idx,
        input_tokens=10,
        output_tokens=20,
        cost_usd=0.0001,
        latency_ms=100,
        status=status,
    )


class _FakeWriter(BatchedWriter):
    """Fake writer that records each batch and fires `flushed` on first flush.

    The `flushed` event lets tests await the first successful flush
    deterministically instead of sleeping for an arbitrary interval.
    """

    def __init__(self) -> None:
        self.batches: list[list[AttemptRecord]] = []
        self.flushed: asyncio.Event = asyncio.Event()

    async def write_batch(self, records: list[AttemptRecord]) -> None:
        self.batches.append(records)
        self.flushed.set()


async def test_enqueue_under_capacity_no_drops():
    writer = _FakeWriter()
    q = AccountingQueue(writer=writer, capacity=100, flush_size=10, flush_interval_ms=50)
    await q.start()
    try:
        for i in range(5):
            q.enqueue(_rec(i))
        await asyncio.sleep(0.15)
    finally:
        await q.stop()
    assert q.dropped_total == 0
    flat = [r for batch in writer.batches for r in batch]
    assert len(flat) == 5
    # Assert the actual record_ids written, not just the count, so a writer
    # that emitted unrelated records would not pass.
    assert {r.request_id for r in flat} == {f"req-{i}" for i in range(5)}


async def test_drops_oldest_when_full():
    writer = _FakeWriter()
    # Tiny capacity, no flushes during the burst.
    q = AccountingQueue(
        writer=writer, capacity=3, flush_size=100, flush_interval_ms=10_000
    )
    # NOT started — so the background drain doesn't fire.
    q.enqueue(_rec(0))
    q.enqueue(_rec(1))
    q.enqueue(_rec(2))
    q.enqueue(_rec(3))  # should evict oldest (request_id=req-0)
    q.enqueue(_rec(4))  # evict req-1
    assert q.dropped_total == 2
    # Note: this test reads private state because the queue doesn't expose a peek API.
    assert len(q._buffer) == 3  # type: ignore[attr-defined]
    # Verify content: req-2, req-3, req-4 remain
    ids = [r.request_id for r in q._buffer]  # type: ignore[attr-defined]
    assert ids == ["req-2", "req-3", "req-4"]


async def test_flushes_at_size_threshold():
    writer = _FakeWriter()
    q = AccountingQueue(
        writer=writer, capacity=100, flush_size=5, flush_interval_ms=10_000
    )
    await q.start()
    try:
        for i in range(5):
            q.enqueue(_rec(i))
        # Wait deterministically for the first flush.
        await asyncio.wait_for(writer.flushed.wait(), timeout=1.0)
        # Snapshot BEFORE stop() so we can prove the size-threshold (not the
        # stop-drain) caused the flush.
        pre_stop = [list(b) for b in writer.batches]
    finally:
        await q.stop()
    assert any(len(b) == 5 for b in pre_stop)


async def test_flushes_at_time_threshold():
    writer = _FakeWriter()
    q = AccountingQueue(
        writer=writer, capacity=100, flush_size=1000, flush_interval_ms=30
    )
    await q.start()
    try:
        q.enqueue(_rec(0))
        # Wait deterministically for the first flush triggered by the timer.
        await asyncio.wait_for(writer.flushed.wait(), timeout=1.0)
        pre_stop = [list(b) for b in writer.batches]
    finally:
        await q.stop()
    # At least one batch with the single enqueued record flushed before stop.
    assert any(len(b) == 1 for b in pre_stop)


async def test_stop_drains_remaining():
    writer = _FakeWriter()
    q = AccountingQueue(writer=writer, capacity=100, flush_size=1000, flush_interval_ms=10_000)
    await q.start()
    for i in range(3):
        q.enqueue(_rec(i))
    await q.stop()
    assert sum(len(b) for b in writer.batches) == 3


# ---------------------------------------------------------------- new tests (t-1 §2)


class _RaisingWriter(BatchedWriter):
    """Writer whose write_batch raises; fires `attempted` so tests can wait."""

    def __init__(self) -> None:
        self.attempts = 0
        self.attempted: asyncio.Event = asyncio.Event()

    async def write_batch(self, records: list[AttemptRecord]) -> None:
        self.attempts += 1
        self.attempted.set()
        raise RuntimeError("db down")


async def test_writer_exception_increments_dropped_total():
    """When the writer raises, the in-flight batch counts toward dropped_total.

    Covers the `except Exception` branch in AccountingQueue._flush.
    Also verifies that ACCOUNTING_DROPPED (the live Prometheus counter) is
    incremented immediately — not only at shutdown — so operators see drops
    in real-time.  (#6.3)
    """
    from gateway.metrics import ACCOUNTING_DROPPED

    # Snapshot the Prometheus counter value before the test.
    # _value is a prometheus_client ValueClass; .get() returns the float.
    before = ACCOUNTING_DROPPED._value.get()

    writer = _RaisingWriter()
    q = AccountingQueue(
        writer=writer, capacity=100, flush_size=3, flush_interval_ms=10_000
    )
    await q.start()
    try:
        for i in range(3):
            q.enqueue(_rec(i))
        await asyncio.wait_for(writer.attempted.wait(), timeout=1.0)
    finally:
        await q.stop()

    assert q.dropped_total >= 3

    # The Prometheus counter must have advanced by at least the number of
    # records in the failed batch — live, without waiting for shutdown.
    after = ACCOUNTING_DROPPED._value.get()
    assert after - before >= 3, (
        f"ACCOUNTING_DROPPED did not advance live: before={before}, after={after}"
    )


async def test_stop_before_start_is_safe():
    """Calling stop() without ever calling start() should not raise.

    The `if self._task:` guard in stop() handles the unstarted case.
    """
    writer = _FakeWriter()
    q = AccountingQueue(writer=writer, capacity=10, flush_size=10, flush_interval_ms=100)
    # Should be a no-op, not raise.
    await q.stop()


async def test_start_is_idempotent():
    """Calling start() twice must not spawn a second background task."""
    writer = _FakeWriter()
    q = AccountingQueue(writer=writer, capacity=10, flush_size=10, flush_interval_ms=100)
    await q.start()
    try:
        first_task = q._task  # type: ignore[attr-defined]
        await q.start()
        second_task = q._task  # type: ignore[attr-defined]
        assert first_task is second_task
    finally:
        await q.stop()


async def test_capacity_eviction_under_continuous_burst():
    """A burst of 100 records into a cap-5 queue (no flushes) keeps newest 5."""
    writer = _FakeWriter()
    q = AccountingQueue(
        writer=writer, capacity=5, flush_size=1000, flush_interval_ms=10_000
    )
    # NOT started — so no background drain runs.
    for i in range(100):
        q.enqueue(_rec(i))
    assert q.dropped_total == 95
    assert len(q._buffer) == 5  # type: ignore[attr-defined]
