"""Tests for the teardown semantics of subscription response streams.

These tests pin down how the source event stream, the per-event resolver
tasks and pending ``anext`` awaits are settled together when a subscription
ends: whether the client leaves early (``aclose``), the source iterator
fails or finishes by itself, an event resolver is cancelled, or an abort
signal fires while backpressure keeps ``anext`` pending.

All sources are observable async iterators gated on event barriers so that
every test can assert that each source is closed exactly once, that no
background tasks are leaked, and that error paths never swallow the
original exception.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import pytest

from graphql import (
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    parse,
)
from graphql.execution import (
    AbortedGraphQLExecutionError,
    ExecutionResult,
    execute,
    subscribe,
)
from graphql.pyutils import AbortController, AbortError

pytestmark = pytest.mark.anyio


schema = GraphQLSchema(
    query=GraphQLObjectType("Query", {"dummy": GraphQLField(GraphQLString)}),
    subscription=GraphQLObjectType(
        "Subscription", {"foo": GraphQLField(GraphQLString)}
    ),
)

document = parse("subscription { foo }")


class ObservableSource:
    """An observable async iterator gated on an event barrier.

    Records every ``anext`` and ``aclose`` call. While the gate is cleared,
    ``anext`` blocks (simulating backpressure) until the gate is set; an
    empty source then raises ``StopAsyncIteration``. An optional ``error``
    is raised from ``anext``, an optional ``close_error`` from ``aclose``.
    """

    def __init__(self) -> None:
        self.anext_calls = 0
        self.close_calls = 0
        self.closed = False
        self.error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.items: deque[Any] = deque()
        self.gate = asyncio.Event()

    def __aiter__(self) -> ObservableSource:
        return self

    async def __anext__(self) -> Any:
        self.anext_calls += 1
        if self.error is not None:
            raise self.error
        if not self.items:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        if self.items:
            return self.items.popleft()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    def feed(self, *items: Any) -> None:
        self.items.extend(items)


class SharedObservableSource:
    """A fan-out source shared by several subscription response streams.

    Every pending ``anext`` receives each emitted event. ``aclose`` wakes
    all pending consumers with ``StopAsyncIteration`` so that a failure of
    one subscription propagates to the other consumers; only the first
    ``aclose`` call has an effect.
    """

    def __init__(self) -> None:
        self.close_calls = 0
        self.close_effects = 0
        self.closed = False
        self.waiters: list[asyncio.Future[Any]] = []

    def __aiter__(self) -> SharedObservableSource:
        return self

    async def __anext__(self) -> Any:
        if self.closed:
            raise StopAsyncIteration
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        try:
            return await waiter
        finally:
            if waiter in self.waiters:
                self.waiters.remove(waiter)

    def emit(self, item: Any) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(item)

    async def aclose(self) -> None:
        self.close_calls += 1
        if not self.closed:
            self.close_effects += 1
            self.closed = True
            for waiter in self.waiters:
                if not waiter.done():
                    waiter.set_exception(StopAsyncIteration)


def pending_tasks() -> set[asyncio.Task[Any]]:
    """Snapshot the currently pending tasks of the running loop."""
    return {task for task in asyncio.all_tasks() if not task.done()}


async def assert_no_new_tasks(baseline: set[asyncio.Task[Any]]) -> None:
    """Assert that no tasks created after the baseline are still pending."""
    for _ in range(5):  # let cancelled or finished tasks settle first
        await asyncio.sleep(0)
    assert not (pending_tasks() - baseline)


def subscribe_to(source: Any, **kwargs: Any) -> AsyncIterator[ExecutionResult]:
    subscription = subscribe(schema, document, root_value={"foo": source}, **kwargs)
    assert isinstance(subscription, AsyncIterator)
    return subscription


def describe_aclose_lifecycle():
    async def closes_source_exactly_once_when_closed_after_next():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        subscription = subscribe_to(source)

        assert await anext(subscription) == ({"foo": "one"}, None)

        await subscription.aclose()  # type: ignore
        assert source.close_calls == 1

        # repeated close is a no-op and does not close the source again
        await subscription.aclose()  # type: ignore
        assert source.close_calls == 1

        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)

    async def does_not_close_source_when_closed_before_first_next():
        baseline = pending_tasks()
        source = ObservableSource()
        subscription = subscribe_to(source)

        # Closing a stream that was never started leaves the source
        # untouched, since the response generator body never ran.
        await subscription.aclose()  # type: ignore
        assert source.close_calls == 0
        assert source.anext_calls == 0

        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)

    async def closes_source_once_when_source_ends_by_itself():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        subscription = subscribe_to(source)

        assert await anext(subscription) == ({"foo": "one"}, None)

        source.gate.set()  # release the barrier with no further events
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        assert source.close_calls == 1

        # closing after the source ended by itself is a no-op
        await subscription.aclose()  # type: ignore
        assert source.close_calls == 1

        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)


def describe_error_paths():
    async def propagates_source_errors_to_the_consumer():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        subscription = subscribe_to(source)

        assert await anext(subscription) == ({"foo": "one"}, None)

        error = ValueError("source-boom")
        source.error = error
        with pytest.raises(ValueError, match="source-boom") as exc_info:
            await anext(subscription)
        assert exc_info.value is error
        assert source.close_calls == 1

        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)

    async def does_not_mask_the_original_error_when_closing_also_fails():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        source.close_error = RuntimeError("close-boom")
        subscription = subscribe_to(source)

        assert await anext(subscription) == ({"foo": "one"}, None)

        error = ValueError("source-boom")
        source.error = error
        with pytest.raises(ValueError, match="source-boom") as exc_info:
            await anext(subscription)
        # the original error is propagated, the failing close is suppressed
        assert exc_info.value is error
        assert source.close_calls == 1
        await assert_no_new_tasks(baseline)

    async def ignores_failing_source_close_on_consumer_initiated_close():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        source.close_error = RuntimeError("close-boom")
        subscription = subscribe_to(source)

        assert await anext(subscription) == ({"foo": "one"}, None)

        await subscription.aclose()  # type: ignore  # does not raise
        assert source.close_calls == 1
        await assert_no_new_tasks(baseline)

    async def captures_event_resolver_errors_in_the_result_and_continues():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "boom"}, {"foo": "ok"})

        def resolve_foo(root: Any, _info: Any) -> Any:
            value = root["foo"]
            if value == "boom":
                raise RuntimeError("event-boom")
            return value

        subscription = subscribe_to(source, field_resolver=resolve_foo)

        result = await anext(subscription)
        assert result.data == {"foo": None}
        assert [error.message for error in result.errors or []] == ["event-boom"]

        # a single failing event does not terminate the stream
        assert await anext(subscription) == ({"foo": "ok"}, None)

        source.gate.set()
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        assert source.close_calls == 1
        await assert_no_new_tasks(baseline)


def describe_cancellation_and_abort():
    async def closes_source_once_when_consumer_is_cancelled_during_next():
        baseline = pending_tasks()
        source = ObservableSource()
        subscription = subscribe_to(source)

        # backpressure: the pending anext blocks on the source barrier
        task = asyncio.ensure_future(anext(subscription))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert source.anext_calls == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert source.close_calls == 1
        await assert_no_new_tasks(baseline)

    async def raises_abort_reason_when_aborted_during_pending_next():
        baseline = pending_tasks()
        source = ObservableSource()
        abort_controller = AbortController()
        subscription = subscribe_to(source, abort_signal=abort_controller.signal)

        # backpressure: the pending anext blocks on the source barrier
        task = asyncio.ensure_future(anext(subscription))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert source.anext_calls == 1

        abort_controller.abort()
        with pytest.raises(AbortError, match="This operation was aborted"):
            await task

        assert source.close_calls == 1
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)

    async def cancels_event_resolver_when_aborted_during_event_execution():
        baseline = pending_tasks()
        source = ObservableSource()
        source.feed({"foo": "one"})
        abort_controller = AbortController()
        resolver_started = asyncio.Event()
        resolver_released = asyncio.Event()
        resolver_cancelled = False

        async def resolve_foo(root: Any, _info: Any) -> Any:
            nonlocal resolver_cancelled
            resolver_started.set()
            try:
                await resolver_released.wait()
            except asyncio.CancelledError:
                resolver_cancelled = True
                raise
            return root["foo"]  # pragma: no cover

        subscription = subscribe_to(
            source,
            field_resolver=resolve_foo,
            abort_signal=abort_controller.signal,
        )

        task = asyncio.ensure_future(anext(subscription))
        await asyncio.wait_for(resolver_started.wait(), 1)

        abort_controller.abort()
        for _ in range(5):  # let the abort cancel the in-flight resolver
            await asyncio.sleep(0)
        assert resolver_cancelled
        resolver_released.set()
        with pytest.raises(AbortedGraphQLExecutionError):
            await task

        assert source.close_calls == 1
        await assert_no_new_tasks(baseline)


def describe_shared_source():
    async def delivers_events_to_all_consumers():
        baseline = pending_tasks()
        source = SharedObservableSource()
        first = subscribe_to(source)
        second = subscribe_to(source)

        first_next = asyncio.ensure_future(anext(first))
        second_next = asyncio.ensure_future(anext(second))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(source.waiters) == 2

        source.emit({"foo": "event"})
        assert await first_next == ({"foo": "event"}, None)
        assert await second_next == ({"foo": "event"}, None)

        await first.aclose()  # type: ignore
        await second.aclose()  # type: ignore
        assert source.close_effects == 1
        await assert_no_new_tasks(baseline)

    async def propagates_single_subscription_failure_to_other_consumers():
        baseline = pending_tasks()
        source = SharedObservableSource()
        abort_controller = AbortController()
        first = subscribe_to(source, abort_signal=abort_controller.signal)
        second = subscribe_to(source)

        first_next = asyncio.ensure_future(anext(first))
        second_next = asyncio.ensure_future(anext(second))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(source.waiters) == 2

        # the first subscription fails while both consumers are pending
        abort_controller.abort()
        with pytest.raises(AbortError, match="This operation was aborted"):
            await first_next

        # its teardown closes the shared source, ending the other consumer
        with pytest.raises(StopAsyncIteration):
            await second_next

        # both response streams call aclose, but the source closes only once
        assert source.close_calls == 2
        assert source.close_effects == 1
        await assert_no_new_tasks(baseline)


def describe_compatibility():
    async def supports_async_iterables_without_aclose():
        baseline = pending_tasks()
        items = deque([{"foo": "one"}, {"foo": "two"}])

        class IterableWithoutClose:
            def __aiter__(self) -> IterableWithoutClose:
                return self

            async def __anext__(self) -> Any:
                try:
                    return items.popleft()
                except IndexError:
                    raise StopAsyncIteration from None

        subscription = subscribe_to(IterableWithoutClose())

        assert await anext(subscription) == ({"foo": "one"}, None)
        assert await anext(subscription) == ({"foo": "two"}, None)
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await assert_no_new_tasks(baseline)

    async def rejects_sync_iterables_as_source_event_stream():
        result = subscribe(schema, document, root_value={"foo": [{"foo": "x"}]})
        assert isinstance(result, ExecutionResult)
        assert result.data is None
        assert [error.message for error in result.errors or []] == [
            "Subscription field must return AsyncIterable. Received: [{'foo': 'x'}]."
        ]

    def keeps_plain_execute_synchronous():
        result = execute(schema, parse("{ dummy }"), root_value={"dummy": "ok"})
        assert isinstance(result, ExecutionResult)
        assert result == ({"dummy": "ok"}, None)
