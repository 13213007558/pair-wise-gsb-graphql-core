"""Audit scenarios: four work kinds x four abort timings.

Each case returns a structured result containing:
* the replayable event ledger;
* how the GraphQL operation itself settled;
* library-owned task leftovers (orphans) observed after the case settles;
* ownership-relevant object state (shared Future, aclose counters).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass, field
from typing import Any

from graphql import (
    AbortedGraphQLExecutionError,
    build_schema,
    execute,
    parse,
    subscribe,
)
from graphql.pyutils import AbortController, AbortError

from .ledger import EventLedger
from .work import (
    ClassAsyncIterator,
    SharedFuture,
    blocking_sync_resolver,
    chunked_sync_resolver,
    cooperative_coroutine,
    hanging_coroutine,
    pushable_generator_source,
)


SCHEMA = build_schema(
    """
    type Query {
      a: String
      b: String
      c: String
    }
    type Mutation {
      m1: String
      m2: String
    }
    type Subscription {
      feed: Event
    }
    type Event {
      event: Int
    }
    """
)
SUBSCRIPTION_SCHEMA = build_schema(
    """
    type Query {
      noop: String
    }
    type Subscription {
      feed: Event
    }
    type Event {
      event: Int
    }
    """,
)
# Per-event payloads use the root subscription field name as their key, as the
# default field resolver requires; Event.event then reads its own attribute.
SUBSCRIPTION_SCHEMA.get_type("Event").fields["event"].resolve = (
    lambda payload, _info: payload["event"]
)


@dataclass
class CaseResult:
    case_id: str
    title: str
    kind: str
    timing: str
    outcome: str
    outcome_detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    orphan_tasks: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


def _settlement(error: BaseException | None) -> tuple[str, str]:
    if error is None:
        return "result", "ExecutionResult returned"
    if isinstance(error, AbortedGraphQLExecutionError):
        cause = error.__cause__ or error
        return "aborted_error", f"{type(cause).__name__}: {cause}"
    if isinstance(error, AbortError):
        return "abort_reason", f"AbortError: {error}"
    message = str(error)
    if isinstance(error, RuntimeError) and "client gone" in message:
        return "abort_reason", f"RuntimeError: {message}"
    return "error", f"{type(error).__name__}: {message}"


async def _drain_aborted_partial(error: BaseException | None) -> None:
    """Await the partial result exposed on the aborted error so that any
    library-owned background work it references gets a chance to settle."""
    if not isinstance(error, AbortedGraphQLExecutionError):
        return
    partial = error.aborted_result
    if asyncio.iscoroutine(partial) or hasattr(partial, "__await__"):
        with contextlib.suppress(BaseException):
            await partial


def _snapshot_tasks(loop: asyncio.AbstractEventLoop) -> set[asyncio.Task]:
    return {task for task in asyncio.all_tasks(loop) if not task.done()}


def _orphan_names(before: set[asyncio.Task], after: set[asyncio.Task]) -> list[str]:
    current = asyncio.current_task()
    leftovers = {task for task in (after - before) if task is not current}
    return [repr(task) for task in sorted(leftovers, key=lambda t: t.get_name())]


async def _run_case(case_id, title, kind, timing, ledger, coro_factory, *,
                    drain_partial: bool = True) -> CaseResult:
    loop = asyncio.get_running_loop()
    before = _snapshot_tasks(loop)
    controller = AbortController()
    error: BaseException | None = None
    try:
        await coro_factory(controller, loop, ledger)
    except BaseException as exc:  # noqa: BLE001 - audit records every outcome
        error = exc
    if drain_partial:
        await _drain_aborted_partial(error)
    for _ in range(5):
        await asyncio.sleep(0)
    after = _snapshot_tasks(loop)
    outcome, detail = _settlement(error)
    return CaseResult(
        case_id=case_id,
        title=title,
        kind=kind,
        timing=timing,
        outcome=outcome,
        outcome_detail=detail,
        counts=dict(ledger.kinds()),
        events=ledger.to_dict()["events"],
        orphan_tasks=_orphan_names(before, after),
    )


# --------------------------------------------------------------------------- #
# Kind 1: plain coroutine resolvers
# --------------------------------------------------------------------------- #
async def cases_coroutine() -> list[CaseResult]:
    results: list[CaseResult] = []

    async def t1(controller, loop, ledger):
        controller.abort(RuntimeError("client gone (t1)"))
        awaitable = execute(
            SCHEMA,
            parse("{ a }"),
            abort_signal=controller.signal,
            root_value={"a": hanging_coroutine("a", ledger)},
        )
        if asyncio.iscoroutine(awaitable):  # pragma: no cover
            await awaitable

    results.append(
        await _run_case(
            "coro-t1-before-start",
            "Abort before execute() is called",
            "coroutine",
            "before_start",
            EventLedger("coro-t1-before-start"),
            t1,
            drain_partial=False,
        )
    )

    async def t2(controller, loop, ledger):
        started = asyncio.Event()
        resolver = hanging_coroutine("a", ledger)

        async def resolving(info=None):
            started.set()
            return await resolver(info)

        task = asyncio.ensure_future(
            execute(
                SCHEMA,
                parse("{ a }"),
                abort_signal=controller.signal,
                root_value={"a": resolving},
            )
        )
        await started.wait()
        await asyncio.sleep(0)
        controller.abort(RuntimeError("client gone (t2)"))
        await task

    results.append(
        await _run_case(
            "coro-t2-while-waiting",
            "Abort while the coroutine resolver is awaiting",
            "coroutine",
            "while_waiting",
            EventLedger("coro-t2-while-waiting"),
            t2,
        )
    )

    async def t3(controller, loop, ledger):
        first_done = asyncio.Event()

        async def quick(_info=None):
            first_done.set()
            return "quick"

        task = asyncio.ensure_future(
            execute(
                SCHEMA,
                parse("{ a b }"),
                abort_signal=controller.signal,
                root_value={"a": quick, "b": hanging_coroutine("b", ledger)},
            )
        )
        await first_done.wait()
        for _ in range(2):
            await asyncio.sleep(0)
        controller.abort(RuntimeError("client gone (t3)"))
        await task

    results.append(
        await _run_case(
            "coro-t3-after-field-done",
            "Abort just after one sibling field completed",
            "coroutine",
            "after_field_done",
            EventLedger("coro-t3-after-field-done"),
            t3,
        )
    )

    async def t2_coop(controller, loop, ledger):
        started = asyncio.Event()
        resolver = cooperative_coroutine("a", ledger)

        async def resolving(info=None):
            started.set()
            return await resolver(info)

        task = asyncio.ensure_future(
            execute(
                SCHEMA,
                parse("{ a }"),
                abort_signal=controller.signal,
                root_value={"a": resolving},
            )
        )
        await started.wait()
        await asyncio.sleep(0)
        controller.abort(RuntimeError("client gone (coop)"))
        await task

    results.append(
        await _run_case(
            "coro-t2-cooperative",
            "Cooperative resolver awaiting info.abort_signal.wait()",
            "coroutine",
            "while_waiting_cooperative",
            EventLedger("coro-t2-cooperative"),
            t2_coop,
        )
    )
    return results


# --------------------------------------------------------------------------- #
# Kind 2: caller-created shared Futures
# --------------------------------------------------------------------------- #
async def cases_shared_future() -> list[CaseResult]:
    results: list[CaseResult] = []

    async def case(case_id, title, timing, adapter, settle_owner_after):
        ledger = EventLedger(case_id)
        shared = SharedFuture("shared", ledger)
        resolver = getattr(shared, f"resolver_{adapter}")()
        started = asyncio.Event()

        async def guarded(info=None):
            # Wrap in a coroutine so an async started event can confirm the
            # resolver body is suspended on the Future before we abort.
            started.set()
            return await resolver(info)

        controller = AbortController()
        loop = asyncio.get_running_loop()
        before = _snapshot_tasks(loop)
        error: BaseException | None = None
        task = asyncio.ensure_future(
            execute(
                SCHEMA,
                parse("{ a }"),
                abort_signal=controller.signal,
                root_value={"a": guarded},
            )
        )
        await started.wait()
        # Let the resolver body reach its suspension on the shared Future.
        for _ in range(3):
            await asyncio.sleep(0)
        if timing == "while_waiting":
            controller.abort(RuntimeError("client gone (future)"))
            try:
                await task
            except BaseException as exc:  # noqa: BLE001
                error = exc
            await _drain_aborted_partial(error)
        if settle_owner_after:
            # The owner can only settle a Future that the library did not
            # cancel. A no-op when the Future is already done.
            if not shared.future.done():
                shared.owner_settles()
            for _ in range(3):
                await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        after = _snapshot_tasks(loop)
        outcome, detail = _settlement(error)
        return CaseResult(
            case_id=case_id,
            title=title,
            kind="shared_future",
            timing=timing,
            outcome=outcome,
            outcome_detail=detail,
            counts=dict(ledger.kinds()),
            events=ledger.to_dict()["events"],
            orphan_tasks=_orphan_names(before, after),
            state={
                "adapter": adapter,
                "shared_future_done": shared.future.done(),
                "shared_future_cancelled": shared.future.cancelled(),
                "shared_future_owner_settled": shared.settled,
            },
        )

    results.append(
        await case(
            "future-t2-returned-directly",
            "Resolver returns the shared Future directly",
            "while_waiting",
            "returns_future_directly",
            False,
        )
    )
    results.append(
        await case(
            "future-t2-plain-await-cancels-shared",
            "Plain await of shared Future: abort cancels the business-owned object too",
            "while_waiting",
            "awaits_future",
            True,
        )
    )
    results.append(
        await case(
            "future-t2-shielded-then-owner-settles",
            "asyncio.shield around the shared Future; owner settles later",
            "while_waiting",
            "shields_future",
            True,
        )
    )
    return results


# --------------------------------------------------------------------------- #
# Kind 3: synchronous resolvers
# --------------------------------------------------------------------------- #
async def cases_sync() -> list[CaseResult]:
    results: list[CaseResult] = []

    async def t3(controller, loop, ledger):
        resolver_a = chunked_sync_resolver("a", ledger, chunks=1)
        resolver_b = blocking_sync_resolver("b", ledger, 0.3)
        timer_holder: dict[str, threading.Timer | None] = {"timer": None}

        def abort_after_a(info=None):
            timer_holder["timer"] = threading.Timer(
                0.05,
                lambda: loop.call_soon_threadsafe(
                    lambda: controller.abort(
                        RuntimeError("client gone (sync t3)")
                    )
                ),
            )
            timer_holder["timer"].start()
            return resolver_a(info)

        try:
            maybe_awaitable = execute(
                SCHEMA,
                parse("{ a b }"),
                abort_signal=controller.signal,
                root_value={"a": abort_after_a, "b": resolver_b},
            )
            if asyncio.iscoroutine(maybe_awaitable):
                await maybe_awaitable
        finally:
            if timer_holder["timer"] is not None:
                timer_holder["timer"].join()

    results.append(
        await _run_case(
            "sync-t3-parallel-while-blocking",
            "Abort during a blocking sync resolver; parallel query has no check between fields",
            "sync",
            "while_running_blocking",
            EventLedger("sync-t3-parallel-while-blocking"),
            t3,
            drain_partial=False,
        )
    )

    async def parallel_inline(controller, loop, ledger):
        # Same abort timing (after 'a', before the reducer returns) but on a
        # parallel query: execute_fields() has no signal check between fields,
        # so 'b' still runs to completion. The abort only surfaces at finish().
        def a(info=None):
            value = chunked_sync_resolver("a", ledger, chunks=1)(info)
            controller.abort(RuntimeError("client gone (sync parallel)"))
            return value

        execute(
            SCHEMA,
            parse("{ a b }"),
            abort_signal=controller.signal,
            root_value={
                "a": a,
                "b": chunked_sync_resolver("b", ledger, chunks=2),
            },
        )

    results.append(
        await _run_case(
            "sync-t3-parallel-inline-abort",
            "Parallel query: abort after field a, field b still runs, abort seen at finish()",
            "sync",
            "after_field_done_parallel",
            EventLedger("sync-t3-parallel-inline-abort"),
            parallel_inline,
            drain_partial=False,
        )
    )

    async def serial(controller, loop, ledger):
        # The whole synchronous execute() call never yields the event loop, so
        # an outside caller cannot squeeze an abort in between two fields: m1
        # itself triggers the abort right before returning. The serial reducer
        # checks the signal before m2, so m2 must never start.
        def m1(info=None):
            value = chunked_sync_resolver("m1", ledger, chunks=2)(info)
            controller.abort(RuntimeError("client gone (mutation)"))
            return value

        execute(
            SCHEMA,
            parse("mutation { m1 m2 }"),
            abort_signal=controller.signal,
            root_value={
                "m1": m1,
                "m2": chunked_sync_resolver("m2", ledger, chunks=2),
            },
        )

    results.append(
        await _run_case(
            "sync-serial-mutation",
            "Serial mutation: abort after m1, before m2 starts",
            "sync",
            "after_field_done_serial",
            EventLedger("sync-serial-mutation"),
            serial,
        )
    )

    async def pre(controller, loop, ledger):
        controller.abort(RuntimeError("client gone (sync t1)"))
        maybe_awaitable = execute(
            SCHEMA,
            parse("{ a b }"),
            abort_signal=controller.signal,
            root_value={
                "a": chunked_sync_resolver("a", ledger),
                "b": chunked_sync_resolver("b", ledger),
            },
        )
        if asyncio.iscoroutine(maybe_awaitable):  # pragma: no cover
            await maybe_awaitable

    results.append(
        await _run_case(
            "sync-t1-before-start",
            "Abort before start: synchronous resolvers never invoked",
            "sync",
            "before_start",
            EventLedger("sync-t1-before-start"),
            pre,
            drain_partial=False,
        )
    )
    return results

# --------------------------------------------------------------------------- #
# Kind 4: subscription async iterators
# --------------------------------------------------------------------------- #
async def cases_subscription() -> list[CaseResult]:
    results: list[CaseResult] = []
    document = parse("subscription { feed { event } }")

    async def do_subscribe(controller, source):
        maybe_stream = subscribe(
            SUBSCRIPTION_SCHEMA,
            document,
            abort_signal=controller.signal,
            root_value={"feed": lambda _info: source},
        )
        if asyncio.iscoroutine(maybe_stream):
            return await maybe_stream
        return maybe_stream

    async def t1(controller, loop, ledger):
        gen, _gates, _ready = pushable_generator_source("src", ledger)
        controller.abort(RuntimeError("client gone (sub t1)"))
        stream = await do_subscribe(controller, gen)
        # subscribe() returns the response generator directly; the pre-existing
        # abort surfaces on the first __anext__ via the cancellable wrapper.
        if hasattr(stream, "errors") and stream.errors:
            ledger.record("subscription_error_result", "src")
            raise AbortError(str(stream.errors[0]))
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    results.append(
        await _run_case(
            "sub-t1-before-start",
            "Abort before subscribe(): source never started",
            "subscription",
            "before_start",
            EventLedger("sub-t1-before-start"),
            t1,
            drain_partial=False,
        )
    )

    async def t2_gen(controller, loop, ledger):
        gen, gates, gate_ready = pushable_generator_source("src", ledger)
        stream = await do_subscribe(controller, gen)
        first = asyncio.ensure_future(stream.__anext__())
        await gate_ready.wait()
        gate_ready.clear()
        gates[0].set_result(0)
        event0 = await first
        assert event0.data == {"feed": {"event": 0}}
        waiting = asyncio.ensure_future(stream.__anext__())
        await gate_ready.wait()
        controller.abort(RuntimeError("client gone (sub t2 gen)"))
        try:
            await waiting
        finally:
            await stream.aclose()

    results.append(
        await _run_case(
            "sub-t2-generator-waiting",
            "Abort while generator source awaits next event, then aclose()",
            "subscription",
            "while_waiting",
            EventLedger("sub-t2-generator-waiting"),
            t2_gen,
            drain_partial=False,
        )
    )

    async def t2_class(controller, loop, ledger):
        source = ClassAsyncIterator("src", ledger)
        stream = await do_subscribe(controller, source)
        first = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        source.push_next(0)
        await first
        waiting = asyncio.ensure_future(stream.__anext__())
        await asyncio.sleep(0)
        controller.abort(RuntimeError("client gone (sub t2 class)"))
        try:
            await waiting
        finally:
            await stream.aclose()
        ledger.record("source_aclose_observed", "src", count=source.aclose_count)

    results.append(
        await _run_case(
            "sub-t2-class-waiting",
            "Abort while custom iterator awaits next event, then aclose()",
            "subscription",
            "while_waiting_class",
            EventLedger("sub-t2-class-waiting"),
            t2_class,
            drain_partial=False,
        )
    )

    async def t4(controller, loop, ledger):
        gen, gates, gate_ready = pushable_generator_source("src", ledger, events=3)
        stream = await do_subscribe(controller, gen)
        for index in range(2):
            nxt = asyncio.ensure_future(stream.__anext__())
            await gate_ready.wait()
            gate_ready.clear()
            gates[index].set_result(index)
            await nxt
        # abort while idle between events (no pending __anext__)
        controller.abort(RuntimeError("client gone (sub t4)"))
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()

    results.append(
        await _run_case(
            "sub-t4-after-event",
            "Abort after events delivered; next __anext__ raises, then aclose()",
            "subscription",
            "after_event_delivered",
            EventLedger("sub-t4-after-event"),
            t4,
            drain_partial=False,
        )
    )

    async def t4_leak(controller, loop, ledger):
        import gc

        gen, gates, gate_ready = pushable_generator_source("src", ledger, events=3)
        inner_gen = gen._gen
        consumer_done = asyncio.Event()

        async def consume_then_abandon():
            # Runs as a fire-and-forget task: nothing in the outer frame awaits
            # it, so once it returns its frame (and the response stream it
            # holds) becomes unreachable.
            stream = await do_subscribe(controller, gen)
            nxt = asyncio.ensure_future(stream.__anext__())
            await gate_ready.wait()
            gates[0].set_result(0)
            await nxt
            # Abort while idle: no pending __anext__ exists, so the library has
            # no in-flight call whose task it could cancel and no close to run.
            controller.abort(RuntimeError("client gone (sub abandoned)"))
            # Deliberately drop the stream without calling aclose().

        consumer = asyncio.ensure_future(consume_then_abandon())
        consumer.add_done_callback(lambda _task: consumer_done.set())
        await consumer_done.wait()
        del gen
        # Before any GC pass the chain is still suspended: nothing has closed
        # it because the abort happened while the stream was idle.
        ledger.record(
            "pre_gc_source_suspended",
            "src",
            suspended=inner_gen.ag_frame is not None,
        )
        for _ in range(5):
            gc.collect()
            await asyncio.sleep(0)
        ledger.record(
            "post_gc_source_still_suspended",
            "src",
            suspended=inner_gen.ag_frame is not None,
        )

    results.append(
        await _run_case(
            "sub-t4-abandoned",
            "Idle abort, consumer drops stream without aclose(); source stays suspended",
            "subscription",
            "after_event_abandoned",
            EventLedger("sub-t4-abandoned"),
            t4_leak,
            drain_partial=False,
        )
    )
    return results


async def run_all() -> list[CaseResult]:
    all_results: list[CaseResult] = []
    for factory in (
        cases_coroutine,
        cases_shared_future,
        cases_sync,
        cases_subscription,
    ):
        all_results.extend(await factory())
    return all_results
