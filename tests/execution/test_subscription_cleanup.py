"""Tests for the cleanup semantics of subscription response streams.

When the subscription client leaves early, the source iterator fails or a
resolver is cancelled, the source, the resolver task and any pending awaits
must be finalized together: each source is closed exactly once, no background
tasks are left behind and error paths do not swallow the original exception.
"""

from __future__ import annotations

from asyncio import (
    CancelledError,
    Event,
    all_tasks,
    ensure_future,
    sleep,
)
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast

import pytest

from graphql.error import GraphQLError
from graphql.execution import (
    AbortedGraphQLExecutionError,
    ExecutionResult,
    execute,
    subscribe,
)
from graphql.language import parse
from graphql.pyutils import AbortController, AbortError
from graphql.type import (
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning"),
]

document = parse("subscription { message }")

query_type = GraphQLObjectType("Query", {"dummy": GraphQLField(GraphQLString)})


class ObservableSource:
    """An observable async generator source for subscription cleanup tests.

    Records start, yield:<payload> and closed events (the latter from the
    generator's finally block) so tests can assert that the source is closed
    exactly once. When hang_at is given, the generator suspends inside its
    anext before yielding the payload at that index (simulating backpressure)
    until the release event barrier is set; entered_hang signals that the
    pending anext call has actually been entered. When error is given, it is
    raised after all payloads have been yielded.
    """

    def __init__(
        self,
        *payloads: Any,
        hang_at: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.payloads = list(payloads)
        self.hang_at = hang_at
        self.error = error
        self.events: list[str] = []
        self.entered_hang = Event()
        self.release = Event()

    @property
    def close_count(self) -> int:
        return self.events.count("closed")

    async def generate(self) -> AsyncGenerator[Any, None]:
        self.events.append("start")
        try:
            for index, payload in enumerate(self.payloads):
                if index == self.hang_at:
                    self.entered_hang.set()
                    await self.release.wait()
                self.events.append(f"yield:{payload}")
                yield payload
            if self.error is not None:
                raise self.error
        finally:
            self.events.append("closed")

    def subscribe(self, _obj: Any, _info: Any) -> AsyncGenerator[Any, None]:
        return self.generate()


def message_schema(subscribe_fn: Any, resolve_fn: Any = None) -> GraphQLSchema:
    return GraphQLSchema(
        query=query_type,
        subscription=GraphQLObjectType(
            "Subscription",
            {
                "message": GraphQLField(
                    GraphQLString,
                    subscribe=subscribe_fn,
                    resolve=resolve_fn or (lambda message, _info: message),
                )
            },
        ),
    )


def make_subscription(
    subscribe_fn: Any, use_abort_signal: bool = False, resolve_fn: Any = None
) -> tuple[AsyncGenerator[ExecutionResult, None], AbortController | None]:
    """Create a subscription, optionally wired to an abort controller.

    The returned response stream is always the async generator that maps
    source events to execution results, so it can be closed with aclose().
    """
    controller = AbortController() if use_abort_signal else None
    kwargs: dict[str, Any] = (
        {"abort_signal": controller.signal} if controller is not None else {}
    )
    subscription = subscribe(
        message_schema(subscribe_fn, resolve_fn), document, **kwargs
    )
    assert isinstance(subscription, AsyncIterator)
    return cast("AsyncGenerator[ExecutionResult, None]", subscription), controller


@pytest.fixture(autouse=True)
async def no_background_tasks_left():
    """Assert that the test leaves no pending background tasks behind."""
    baseline = all_tasks()
    yield
    for _ in range(3):  # let cancelled tasks settle first
        await sleep(0)
    leaked = []
    for task in all_tasks():
        coro = task.get_coro()
        qualname = getattr(coro, "__qualname__", "")
        # ignore the tasks of the anyio test runner itself
        if qualname.startswith("TestRunner."):
            continue
        if task not in baseline and not task.done():
            leaked.append(task)
    assert not leaked


def describe_closing_the_response_stream():
    async def closing_before_the_first_next_is_a_clean_no_op():
        source = ObservableSource("a", "b")
        subscription, _controller = make_subscription(source.subscribe)

        await subscription.aclose()

        # the source was never started, so there is nothing to close
        assert source.events == []
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await subscription.aclose()
        assert source.events == []

    async def closing_before_the_first_next_with_abort_signal_wired():
        source = ObservableSource("a", "b")
        subscription, _controller = make_subscription(
            source.subscribe, use_abort_signal=True
        )

        await subscription.aclose()

        assert source.events == []
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await subscription.aclose()
        assert source.events == []

    async def closing_after_next_closes_the_source_exactly_once():
        source = ObservableSource("a", "b")
        subscription, _controller = make_subscription(source.subscribe)

        assert (await anext(subscription)).data == {"message": "a"}

        await subscription.aclose()

        assert source.events == ["start", "yield:a", "closed"]
        assert source.close_count == 1
        # closing repeatedly does not close the source again
        await subscription.aclose()
        assert source.close_count == 1
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)

    async def closing_after_next_with_abort_signal_wired():
        source = ObservableSource("a", "b")
        subscription, _controller = make_subscription(
            source.subscribe, use_abort_signal=True
        )

        assert (await anext(subscription)).data == {"message": "a"}

        await subscription.aclose()

        assert source.events == ["start", "yield:a", "closed"]
        await subscription.aclose()
        assert source.close_count == 1
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)

    async def repeated_closes_close_a_custom_iterator_source_only_once():
        class CountingIterator:
            def __init__(self, payloads: list[str]) -> None:
                self.payloads = payloads
                self.aclose_count = 0
                self.closed = False

            def __aiter__(self) -> CountingIterator:
                return self

            async def __anext__(self) -> str:
                if self.closed or not self.payloads:
                    raise StopAsyncIteration
                await sleep(0)
                return self.payloads.pop(0)

            async def aclose(self) -> None:
                self.aclose_count += 1
                self.closed = True

        iterator = CountingIterator(["a", "b"])
        subscription, _controller = make_subscription(lambda _obj, _info: iterator)

        assert (await anext(subscription)).data == {"message": "a"}

        await subscription.aclose()
        await subscription.aclose()

        assert iterator.aclose_count == 1
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)

    async def a_source_ending_on_its_own_is_closed_exactly_once():
        source = ObservableSource("a", "b")
        subscription, _controller = make_subscription(source.subscribe)

        assert (await anext(subscription)).data == {"message": "a"}
        assert (await anext(subscription)).data == {"message": "b"}
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)

        assert source.events == ["start", "yield:a", "yield:b", "closed"]
        # closing after natural exhaustion does not close the source again
        await subscription.aclose()
        assert source.close_count == 1


def describe_cancelling_the_consumer():
    async def cancelling_a_pending_next_closes_the_source_and_leaves_no_tasks():
        source = ObservableSource("a", "b", hang_at=1)
        subscription, _controller = make_subscription(source.subscribe)

        assert (await anext(subscription)).data == {"message": "a"}

        task = ensure_future(anext(subscription))
        await source.entered_hang.wait()
        task.cancel()
        with pytest.raises(CancelledError):
            await task

        await subscription.aclose()

        assert source.events == ["start", "yield:a", "closed"]
        assert source.close_count == 1
        source.release.set()  # nothing must still be waiting on the barrier

    async def cancelling_a_pending_next_with_abort_signal_wired():
        source = ObservableSource("a", "b", hang_at=1)
        subscription, _controller = make_subscription(
            source.subscribe, use_abort_signal=True
        )

        assert (await anext(subscription)).data == {"message": "a"}

        task = ensure_future(anext(subscription))
        await source.entered_hang.wait()
        task.cancel()
        with pytest.raises(CancelledError):
            await task

        await subscription.aclose()

        # the pending source anext task was cancelled and settled together
        # with the consumer task instead of being orphaned in the background
        assert source.events == ["start", "yield:a", "closed"]
        assert source.close_count == 1
        source.release.set()


def describe_aborting_during_a_subscription():
    async def aborting_before_the_first_next_raises_the_abort_reason():
        source = ObservableSource("a", "b")
        subscription, controller = make_subscription(
            source.subscribe, use_abort_signal=True
        )
        assert controller is not None
        controller.abort()

        with pytest.raises(AbortError, match="This operation was aborted"):
            await anext(subscription)

        # the source was never started, so there is nothing to close
        assert source.events == []
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await subscription.aclose()

    async def aborting_during_a_pending_next_raises_the_original_reason():
        source = ObservableSource("a", "b", hang_at=1)
        subscription, controller = make_subscription(
            source.subscribe, use_abort_signal=True
        )
        assert controller is not None

        assert (await anext(subscription)).data == {"message": "a"}

        task = ensure_future(anext(subscription))
        await source.entered_hang.wait()
        reason = RuntimeError("client disconnected")
        controller.abort(reason)

        # the original abort reason is raised, not a wrapper or generic error
        with pytest.raises(RuntimeError) as exc_info:
            await task
        assert exc_info.value is reason

        # the pending source anext was cancelled and the source was closed
        assert source.events == ["start", "yield:a", "closed"]
        assert source.close_count == 1
        await subscription.aclose()
        assert source.close_count == 1
        source.release.set()

    async def aborting_during_a_hanging_resolver_cancels_the_resolver_task():
        source = ObservableSource("a", "b")
        resolver_events: list[str] = []
        resolver_entered = Event()
        release_resolver = Event()

        async def resolve_message(message: str, _info: Any) -> str:
            resolver_events.append("entered")
            resolver_entered.set()
            try:
                await release_resolver.wait()
                return message  # pragma: no cover
            finally:
                resolver_events.append("finalized")

        subscription, controller = make_subscription(
            source.subscribe, use_abort_signal=True, resolve_fn=resolve_message
        )
        assert controller is not None

        task = ensure_future(anext(subscription))
        await resolver_entered.wait()
        reason = RuntimeError("client went away")
        controller.abort(reason)

        with pytest.raises(AbortedGraphQLExecutionError) as exc_info:
            await task
        # the original abort reason is exposed, not swallowed
        assert exc_info.value.reason is reason
        assert exc_info.value.__cause__ is reason

        # the partial result of the aborted event execution still settles
        partial = await exc_info.value.aborted_result
        assert isinstance(partial, ExecutionResult)
        assert partial.data == {"message": None}
        assert partial.errors is not None
        assert partial.errors[0].original_error is reason

        # source, resolver task and pending awaits were finalized together
        assert resolver_events == ["entered", "finalized"]
        assert source.events == ["start", "yield:a", "closed"]
        await subscription.aclose()
        assert source.close_count == 1
        release_resolver.set()


def describe_failing_subscriptions():
    async def a_source_error_propagates_the_original_exception():
        boom = RuntimeError("source failed")
        source = ObservableSource("a", error=boom)
        subscription, _controller = make_subscription(source.subscribe)

        assert (await anext(subscription)).data == {"message": "a"}

        # the exact exception raised by the source iterator is propagated
        with pytest.raises(RuntimeError) as exc_info:
            await anext(subscription)
        assert exc_info.value is boom

        assert source.events == ["start", "yield:a", "closed"]
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
        await subscription.aclose()
        assert source.close_count == 1

    async def a_failing_event_resolver_reports_error_and_keeps_stream_alive():
        boom = RuntimeError("event execution failed")
        source = ObservableSource("good", "bad", "good-again")

        def resolve_message(message: str, _info: Any) -> str:
            if message == "bad":
                raise boom
            return message

        subscription, _controller = make_subscription(
            source.subscribe, resolve_fn=resolve_message
        )

        assert (await anext(subscription)).data == {"message": "good"}

        # a single failing event is reported as an error result that exposes
        # the original exception; it does not close the response stream
        result = await anext(subscription)
        assert result.data == {"message": None}
        assert result.errors is not None
        assert len(result.errors) == 1
        assert isinstance(result.errors[0], GraphQLError)
        assert result.errors[0].original_error is boom

        assert (await anext(subscription)).data == {"message": "good-again"}

        await subscription.aclose()
        assert source.events == [
            "start",
            "yield:good",
            "yield:bad",
            "yield:good-again",
            "closed",
        ]
        assert source.close_count == 1


def describe_shared_subscription_sources():
    async def closing_one_consumer_propagates_to_the_other_consumers():
        source = ObservableSource("a", "b", "c")
        shared = source.generate()
        subscription_a, _ = make_subscription(lambda _obj, _info: shared)
        subscription_b, _ = make_subscription(lambda _obj, _info: shared)

        assert (await anext(subscription_a)).data == {"message": "a"}
        assert (await anext(subscription_b)).data == {"message": "b"}

        await subscription_a.aclose()

        # closing one subscription closed the shared source exactly once,
        # which ends the stream for the other consumer as well
        assert source.events == ["start", "yield:a", "yield:b", "closed"]
        with pytest.raises(StopAsyncIteration):
            await anext(subscription_b)

        await subscription_b.aclose()
        assert source.close_count == 1

    async def a_source_error_propagates_to_all_consumers():
        boom = RuntimeError("shared source failed")
        source = ObservableSource("a", error=boom)
        shared = source.generate()
        subscription_a, _ = make_subscription(lambda _obj, _info: shared)
        subscription_b, _ = make_subscription(lambda _obj, _info: shared)

        assert (await anext(subscription_a)).data == {"message": "a"}

        # the consumer pulling the failing event sees the original exception
        with pytest.raises(RuntimeError) as exc_info:
            await anext(subscription_a)
        assert exc_info.value is boom

        # the failed source is closed, ending the stream for the other
        # consumer as well
        assert source.events == ["start", "yield:a", "closed"]
        with pytest.raises(StopAsyncIteration):
            await anext(subscription_b)

        await subscription_a.aclose()
        await subscription_b.aclose()
        assert source.close_count == 1


def describe_subscription_cleanup_compatibility():
    async def sync_iterators_are_still_rejected_as_subscription_sources():
        def subscribe_sync(_obj: Any, _info: Any) -> Any:
            return iter(["a", "b"])

        result = subscribe(message_schema(subscribe_sync), document)

        assert isinstance(result, ExecutionResult)
        assert result.data is None
        assert result.errors is not None
        assert result.errors[0].message == (
            "Subscription field must return AsyncIterable."
            " Received: <list_iterator instance>."
        )

    async def normal_execute_is_not_affected():
        result = execute(
            message_schema(ObservableSource("a").subscribe),
            parse("{ dummy }"),
            root_value={"dummy": "value"},
        )

        assert result == ({"dummy": "value"}, None)
