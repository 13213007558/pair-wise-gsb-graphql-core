"""Instrumented units of work used by the abort audit.

Four ownership shapes are exercised, matching the four shapes a real resolver
can take:

* coroutine resolver      - the library wraps it in a Task it owns;
* caller-created Future   - owned by the caller/business layer, never by the
                            library, so cancellation must not be forced on it;
* synchronous resolver    - no suspension point, cannot be interrupted;
* subscription iterator   - an async generator / custom async iterator whose
                            ``aclose()`` is the only cleanup contract.
"""

from __future__ import annotations

import asyncio

from .ledger import EventLedger


# --------------------------------------------------------------------------- #
# 1. Plain coroutine resolvers
# --------------------------------------------------------------------------- #
def hanging_coroutine(work_id: str, ledger: EventLedger):
    """A coroutine that suspends forever until it is cancelled."""

    async def resolver(_info=None):
        ledger.record("started", work_id)
        gate: asyncio.Future[None] = asyncio.Future()
        try:
            await gate
        except asyncio.CancelledError:
            ledger.record("cancel_notice", work_id, mechanism="task.cancel")
            raise
        finally:
            ledger.record("finally", work_id)

    return resolver


def cooperative_coroutine(work_id: str, ledger: EventLedger):
    """A coroutine that cooperates with the abort signal instead of being killed.

    It awaits ``info.abort_signal.wait()`` itself, so its own body receives no
    CancelledError: it observes the signal and returns cleanly through finally.
    """

    async def resolver(info=None):
        ledger.record("started", work_id)
        signal = info.abort_signal
        try:
            await signal.wait()
            ledger.record("signal_observed", work_id)
            return None
        except asyncio.CancelledError:
            ledger.record("cancel_notice", work_id, mechanism="task.cancel")
            raise
        finally:
            ledger.record("finally", work_id)

    return resolver


# --------------------------------------------------------------------------- #
# 2. Caller-created, externally owned Futures
# --------------------------------------------------------------------------- #
class SharedFuture:
    """Wraps a Future that business code keeps its own reference to.

    The resolver hands the *same* Future object to GraphQL. Because
    ``asyncio.ensure_future`` on an already-created Future returns that very
    object, calling ``.cancel()`` on it would cancel the business-owned object
    too. The safe adapters below show how ownership is respected.
    """

    def __init__(self, work_id: str, ledger: EventLedger):
        self.work_id = work_id
        self.ledger = ledger
        self.future: asyncio.Future[str] = asyncio.Future()
        self.cancelled_by_owner = False
        self.settled = False

    def resolver_returns_future_directly(self):
        """Baseline: resolver returns the shared Future itself.

        The library treats this Future as the unit of work; an abort-driven
        ``task.cancel()`` therefore reaches the business-owned object. This is
        only safe when the owner hands over ownership of the Future.
        """

        def resolver(_info=None):
            self.ledger.record("started", self.work_id)
            return self.future

        return resolver

    def resolver_awaits_future(self):
        """Plain ``await`` of the shared Future.

        This does NOT isolate ownership: cancelling the library Task that runs
        this coroutine propagates CancelledError through the plain await and
        cancels the shared Future as well. It is included to demonstrate the
        trap; use ``resolver_shields_future`` for real ownership transfer.
        """

        async def resolver(_info=None):
            self.ledger.record("started", self.work_id)
            try:
                return await self.future
            except asyncio.CancelledError:
                self.ledger.record(
                    "cancel_notice", self.work_id, mechanism="plain await propagates cancel"
                )
                raise
            finally:
                self.ledger.record("finally", self.work_id)

        return resolver

    def resolver_shields_future(self):
        """Ownership-safe adapter using ``asyncio.shield``: abort cancels the
        shield outer task while the inner shared Future keeps running and
        remains the sole responsibility of its business owner."""

        async def resolver(_info=None):
            self.ledger.record("started", self.work_id)
            try:
                return await asyncio.shield(self.future)
            except asyncio.CancelledError:
                self.ledger.record(
                    "cancel_notice", self.work_id, mechanism="shield outer.cancel"
                )
                raise
            finally:
                self.ledger.record("finally", self.work_id)

        return resolver

    def owner_settles(self, value: str = "owner-value") -> None:
        """The owner, and only the owner, settles its Future."""
        if not self.future.done():
            self.future.set_result(value)
            self.settled = True

    def owner_cancels(self) -> None:
        """The owner, and only the owner, cancels its Future."""
        if not self.future.done():
            self.future.cancel()
            self.cancelled_by_owner = True


# --------------------------------------------------------------------------- #
# 3. Synchronous resolvers - uninterruptible once they start
# --------------------------------------------------------------------------- #
def chunked_sync_resolver(work_id: str, ledger: EventLedger, chunks: int = 4):
    """A synchronous resolver that does CPU work in chunks, polling the abort
    flag between chunks. It never awaits, so nothing outside it can interrupt a
    chunk that is already running."""

    def resolver(info=None):
        ledger.record("started", work_id)
        signal = getattr(info, "abort_signal", None)
        observed = False
        for index in range(chunks):
            for _ in range(1000):
                pass  # pure synchronous work inside the chunk
            if signal is not None and signal.aborted and not observed:
                ledger.record("signal_observed", work_id, chunk=index + 1)
                observed = True
        ledger.record("completed", work_id)
        return f"sync-{work_id}"

    return resolver


def blocking_sync_resolver(work_id: str, ledger: EventLedger, seconds: float):
    """A truly blocking synchronous resolver (time.sleep). It cannot observe
    anything until it returns; the abort is only seen at the next field
    boundary."""
    import time

    def resolver(_info=None):
        ledger.record("started", work_id)
        time.sleep(seconds)
        ledger.record("completed", work_id)
        return f"blocking-{work_id}"

    return resolver


# --------------------------------------------------------------------------- #
# 4. Subscription source iterators
# --------------------------------------------------------------------------- #
def pushable_generator_source(work_id: str, ledger: EventLedger, events: int = 3):
    """Async-generator source whose per-event Futures are externally pushable,
    so the audit controls exactly when an event is delivered.

    Returns ``(iterable, gates, gate_ready)`` where ``gate_ready`` is an Event
    set whenever a fresh per-event gate exists, removing any delivery race. The
    returned iterable wraps the generator and records every explicit
    ``aclose()`` call separately from the generator's own ``finally`` event.

    ``aclose()`` on an async generator throws GeneratorExit into its suspended
    frame, which runs the finally block; that is the cleanup contract.
    """

    gates: list[asyncio.Future[int]] = []
    gate_ready = asyncio.Event()

    async def source():
        ledger.record("started", work_id)
        try:
            for index in range(events):
                gate: asyncio.Future[int] = asyncio.Future()
                gates.append(gate)
                gate_ready.set()
                try:
                    payload = await gate
                except asyncio.CancelledError:
                    ledger.record(
                        "cancel_notice",
                        work_id,
                        mechanism="anext task.cancel",
                        index=index,
                    )
                    raise
                ledger.record("delivered", work_id, index=payload)
                yield {"feed": {"event": payload}}
        finally:
            ledger.record("finally", work_id)

    class InstrumentedGenerator:
        def __init__(self, gen):
            self._gen = gen
            self.aclose_count = 0

        def __aiter__(self):
            return self

        def __anext__(self):
            return self._gen.__anext__()

        async def aclose(self):
            self.aclose_count += 1
            ledger.record("aclose", work_id, count=self.aclose_count)
            await self._gen.aclose()

    return InstrumentedGenerator(source()), gates, gate_ready


class ClassAsyncIterator:
    """Custom async iterator (not a generator): cancellation only reaches it if
    it cooperates, and cleanup is an explicit ``aclose()``."""

    def __init__(self, work_id: str, ledger: EventLedger, events: int = 3):
        self.work_id = work_id
        self.ledger = ledger
        self._events = events
        self._index = 0
        self._pending: asyncio.Future[int] | None = None
        self.aclose_count = 0
        self.started = False

    def __aiter__(self):
        return self

    def __anext__(self):
        if not self.started:
            self.ledger.record("started", self.work_id)
            self.started = True
        if self._index >= self._events:
            raise StopAsyncIteration
        self._pending = asyncio.Future()
        pending = self._pending
        index = self._index

        async def wait_for_event():
            try:
                payload = await pending
            except asyncio.CancelledError:
                self.ledger.record(
                    "cancel_notice",
                    self.work_id,
                    mechanism="anext task.cancel",
                    index=index,
                )
                raise
            self.ledger.record("delivered", self.work_id, index=payload)
            self._index += 1
            return {"feed": {"event": payload}}

        return wait_for_event()

    async def aclose(self):
        self.aclose_count += 1
        self.ledger.record("aclose", self.work_id, count=self.aclose_count)
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()

    def push_next(self, value: int) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(value)
