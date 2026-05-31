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
    def __init__(self) -> None:
        self.batches: list[list[AttemptRecord]] = []

    async def write_batch(self, records: list[AttemptRecord]) -> None:
        self.batches.append(records)


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
        await asyncio.sleep(0.05)
    finally:
        await q.stop()
    assert any(len(b) == 5 for b in writer.batches)


async def test_flushes_at_time_threshold():
    writer = _FakeWriter()
    q = AccountingQueue(
        writer=writer, capacity=100, flush_size=1000, flush_interval_ms=30
    )
    await q.start()
    try:
        q.enqueue(_rec(0))
        await asyncio.sleep(0.1)
    finally:
        await q.stop()
    assert sum(len(b) for b in writer.batches) == 1


async def test_stop_drains_remaining():
    writer = _FakeWriter()
    q = AccountingQueue(writer=writer, capacity=100, flush_size=1000, flush_interval_ms=10_000)
    await q.start()
    for i in range(3):
        q.enqueue(_rec(i))
    await q.stop()
    assert sum(len(b) for b in writer.batches) == 3
