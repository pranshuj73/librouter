# `gateway/accounting.py` — write-behind audit queue

## Purpose

`AccountingQueue` is the write-behind buffer between the request hot path and
Postgres. Every `AttemptRecord` produced by the `Router` (one per attempt, not
just the winning one) is enqueued non-blockingly; a background task drains
batches into `Database.write_batch`. The queue is the single mechanism that
prevents Postgres latency or downtime from blocking caller responses.

This module owns nothing about *what* gets recorded — that is the Router's job.
It owns *when* batches flush, *how* overflow is handled, and *how* writer
failures degrade. The actual SQL lives in [`db.md`](db.md).

See [`architecture.md`](../architecture.md) §7 for how accounting fits into the
resilience story (write-behind absorbs Postgres outages).

## Public surface

| Symbol | Signature | Notes |
|---|---|---|
| `BatchedWriter` | `Protocol` with `async write_batch(records: list[AttemptRecord]) -> None` | Satisfied by `Database` and by test fakes. |
| `AccountingQueue` | `class` | One instance per process. |
| `AccountingQueue.__init__` | `(*, writer: BatchedWriter, capacity=10_000, flush_size=200, flush_interval_ms=250)` | All four sizing knobs are constructor-only. |
| `AccountingQueue.enqueue` | `(rec: AttemptRecord) -> None` (sync) | Hot-path call. Never raises. |
| `AccountingQueue.dropped_total` | `property -> int` | Cumulative drop count since process start. |
| `AccountingQueue.start` | `async () -> None` | Idempotent; spawns the drain task. |
| `AccountingQueue.stop` | `async () -> None` | Signals stop, awaits the drain task, then performs a final flush. |

There is no public way to peek at the buffer or force a flush mid-flight other
than letting size or interval thresholds fire. Tests reach into `_buffer`
directly when they need to assert state ([`../code-review/t-1.md`](../../code-review/t-1.md) §2).

## Internals

### Design at a glance

```
hot path ─enqueue()─► deque (cap 10_000) ─wakeup.set() at flush_size─►  _drain_loop ─► writer.write_batch(batch)
                          │                                                │
                          └── on overflow: popleft() + dropped_total++     └── timer: every flush_interval_ms
                              (also bumps ACCOUNTING_DROPPED only in _flush; see Failure modes)
```

### Buffer

`_buffer: deque[AttemptRecord]` is constructed with `maxlen=None` rather than
`maxlen=capacity`. Capacity is enforced manually in `enqueue` (`accounting.py:52-61`):

```python
if len(self._buffer) >= self._capacity:
    try:
        self._buffer.popleft()
        self._dropped_total += 1
    except IndexError:
        pass
self._buffer.append(rec)
```

This is intentional. Using `deque(maxlen=capacity)` would silently drop the
*oldest* record on overflow, but we would not be able to count drops without
extra bookkeeping. The manual form makes the count + drop atomic.

### Flush triggers

Two triggers, whichever comes first:

| Trigger | Wired via | Default |
|---|---|---|
| Size-based | `enqueue` calls `self._wakeup.set()` once `len(self._buffer) >= flush_size` | `flush_size=200` |
| Time-based | `_drain_loop` uses `asyncio.wait_for(self._wakeup.wait(), timeout=flush_interval_s)` | `flush_interval_ms=250` |

### `_drain_loop`

`accounting.py:85-96`:

```python
while not self._stop.is_set():
    try:
        await asyncio.wait_for(self._wakeup.wait(), timeout=self._flush_interval_s)
    except asyncio.TimeoutError:
        pass
    self._wakeup.clear()
    await self._flush()
# Catch anything that landed between the last wakeup and stop
await self._flush()
```

Invariants:

- Exactly one drain task per `AccountingQueue` (`start` is idempotent —
  `self._task is None` guard).
- The loop reads `len(self._buffer)` only inside `_flush`. There is no separate
  "is the buffer big enough?" check in the loop body — the wakeup event already
  encoded that.
- `_wakeup.clear()` happens *before* `_flush`, so a record arriving during the
  flush will set the wakeup again and trigger another iteration. No racy lost
  wakeups.
- An empty `_flush` is cheap: it returns on the first `if not self._buffer`
  check without acquiring a DB connection.

### `_flush`

`accounting.py:98-111`:

```python
batch = list(self._buffer)
self._buffer.clear()
try:
    await self._writer.write_batch(batch)
except Exception:
    log.exception("accounting write_batch failed; %d rows dropped", len(batch))
    n = len(batch)
    self._dropped_total += n
    ACCOUNTING_DROPPED.inc(n)
```

Order matters: the buffer is *cleared before* the write is awaited. This means
a failed batch is permanently lost — there is no retry queue. The design
trade-off is that the queue cannot grow unboundedly during a Postgres outage
because failed batches drop on the floor immediately. The cost is data loss
under outage. Operators see this via:

1. `dropped_total` (in-process counter accessible via the property).
2. The `ACCOUNTING_DROPPED` Prometheus counter, bumped **live** on the
   writer-exception path (`accounting.py:111`) — operators see drops the
   moment a flush fails, not only at shutdown. (This is the cr-1 §6.3 fix
   from commit `8e046e5`.)
3. The `log.exception` traceback at WARNING level.

### Lifecycle

`start` is called once in `app.lifespan` (`app.py:197-198`). `stop` is called in
the lifespan teardown (`app.py:225`). The two-step `stop`:

1. `self._stop.set(); self._wakeup.set()` — unblocks the drain loop.
2. `await self._task` — waits for the loop to exit. The loop itself does a final
   `await self._flush()` after the `while` exits.
3. `await self._flush()` — belt-and-braces flush in case anything was enqueued
   between the loop's last flush and `_task` returning.

The two consecutive final flushes are idempotent because each clears the buffer.

## Concurrency model

- **Single drain task per queue.** Guarded by `if self._task is None` in
  `start`. Calling `start` twice is a no-op.
- **`enqueue` is sync and runs on the event loop.** It mutates a `deque` —
  CPython's GIL plus the fact that all callers are on one event loop means
  `popleft` / `append` are race-free. There is no lock.
- **Multiple concurrent `enqueue` callers are safe** under the same constraint:
  all run in the same loop thread.
- **`_flush` holds no lock while awaiting `write_batch`.** New `enqueue` calls
  during the flush land in a now-empty buffer and will trigger the next cycle.
- **Off-thread `enqueue` is *not* supported.** The Router runs in the same
  event loop as the queue, so this is fine in practice.

The queue is intentionally a process-local singleton — there is no replica-wide
coordination. Each replica buffers its own attempts and flushes to the shared
Postgres independently.

## Failure modes

| Scenario | Behavior |
|---|---|
| Buffer at capacity, new enqueue | `popleft()` drops the oldest, `_dropped_total++`. New record is appended. The Prometheus counter is *not* bumped on drop-by-overflow — only on writer failure. (See "Open questions".) |
| `writer.write_batch` raises | Caught in `_flush`, logged via `log.exception`, all rows in the batch are counted as dropped, `ACCOUNTING_DROPPED.inc(n)` runs immediately (`accounting.py:111`). The buffer is *already cleared* — no retry. |
| `writer.write_batch` hangs | `_drain_loop` is blocked on the await; `_buffer` continues to grow up to `capacity`, then drops oldest per enqueue. There is no timeout on `write_batch` from this side. |
| `stop` called before `start` | `self._stop.set(); self._wakeup.set(); if self._task: …` — `self._task` is `None`, so the await is skipped. The final `_flush` still runs (no-op on empty buffer). |
| `stop` called twice | Second call: `_stop` already set, `_task` already `None`, final `_flush` is a no-op. Safe. |
| Process killed (SIGKILL, OOM) | Whatever was in the buffer is lost. The queue makes no durability guarantee. |

## Configuration knobs

| Knob | Constructor arg | Default | Effect |
|---|---|---|---|
| Capacity | `capacity` | `10_000` | Hard cap on in-memory records before drop-oldest kicks in. |
| Flush size | `flush_size` | `200` | When buffer reaches this length, wake the drain task immediately. |
| Flush interval | `flush_interval_ms` | `250` | Wake the drain task at least every N ms even if `flush_size` is not reached. |
| Writer | `writer` | (required) | Any `BatchedWriter`. Wired to `Database` in `app.py:197`. |

These are constructor-only; there is no env-var override and the values are
hardcoded at the `AccountingQueue(writer=db)` call site (`app.py:197` uses all
defaults).

## Open questions / known gaps

- **§6.3 — drops not emitted live.** Resolved in commit `8e046e5`:
  `ACCOUNTING_DROPPED.inc(n)` now fires inline in `_flush` on the
  writer-exception path, so operators see drops the instant a batch fails,
  not only at shutdown. **Remaining sub-gap:** overflow drops in `enqueue`
  still bump only the in-process `_dropped_total` and never touch the
  Prometheus counter. Watching only Prometheus will surface Postgres
  outages but not "queue too small / Router enqueued too fast" overflows.
- **No retry on writer failure.** Failed batches are lost on the first
  exception. Adding a retry queue would require a second deque + a max-retries
  policy to keep the bound on memory.
- **No back-pressure.** The queue is fire-and-forget by design. There is no way
  for the Router to be told "stop generating attempts; I cannot keep up." A
  prolonged Postgres outage at sustained traffic will silently lose audit rows.
- **`flush_interval_ms` and `flush_size` are not exposed via config.** Tuning
  requires editing the `AccountingQueue(...)` call site in `app.py:197`.
- **Test coverage** ([`../code-review/t-1.md`](../../code-review/t-1.md) §2):
  covers happy path, capacity drops, size threshold, time threshold, and stop
  drain. Missing: writer-exception path is not exercised end-to-end against the
  Prometheus counter; multi-stop is not asserted.

## Cross-references

- Writer implementation: [`modules/db.md`](db.md) — `Database.write_batch`
  (now writes `client_trace_id` in addition to the prior 12 columns).
- Boot wiring: [`modules/app.md`](app.md) (start at `app.py:197-198`, stop at
  `app.py:225`).
- Producer: [`modules/router.md`](router.md) — `Router.route` returns an
  attempts list; the `/v1/chat/completions` handler enqueues each.
- Architecture context: [`architecture.md`](../architecture.md) §7 row
  "Postgres is unreachable mid-flight".
- Code-review references: [`../code-review/cr-1.md`](../../code-review/cr-1.md) §6.3 (resolved 8e046e5; overflow-path sub-gap open).
- Test review: [`../code-review/t-1.md`](../../code-review/t-1.md) §2
