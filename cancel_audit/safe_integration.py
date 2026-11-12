"""Safe abort-signal integration patterns for graphql-core 3.3.

Each pattern is self-contained and runnable:

    .venv/bin/python -m cancel_audit.safe_integration

The rules demonstrated below follow from what the library's cancellation
machinery can and cannot express (see cancel_audit/README.md for the analysis):

1. Coroutine/Task resolvers are library-owned and ARE cancelled on abort; put
   cleanup in ``finally`` (or use ``async with``) and it always runs.
2. A Future you create and keep a reference to is caller-owned. A plain
   ``await shared_future`` still propagates cancellation into that Future when
   the library Task is cancelled. Use ``asyncio.shield`` (or have a separate,
   business-owned task hold the Future) so only the outer await is cancelled.
3. Synchronous resolvers have no suspension point and cannot be interrupted;
   poll ``info.abort_signal.aborted`` between chunks for cooperative checks.
4. Subscriptions are only closed deterministically when the consumer calls
   ``aclose()`` (or aborts while a ``__anext__`` is in flight). Always close
   the response stream.
"""

from __future__ import annotations

import asyncio
import time

from graphql import (
    AbortedGraphQLExecutionError,
    build_schema,
    execute,
    parse,
    subscribe,
)
from graphql.pyutils import AbortController

SCHEMA = build_schema(
    """
    type Query {
      owned: String
      shared: String
      shielded: String
      cooperative: String
      blocking: String
    }
    type Subscription {
      feed: Event
    }
    type Event {
      event: Int
    }
    """
)


# --------------------------------------------------------------------------- #
# Pattern 1: library-owned coroutine - cancellation + finally just works
# --------------------------------------------------------------------------- #
async def owned_coroutine_resolver(_info):
    resource = acquire_resource()
    try:
        await asyncio.sleep(30)  # a long-lived, cancellable await
        return "done"
    finally:
        # Runs because the library Task wrapping this coroutine is cancelled.
        release_resource(resource)


def acquire_resource():
    print("  resource acquired")
    return object()


def release_resource(_resource):
    print("  resource released in finally")


# --------------------------------------------------------------------------- #
# Pattern 2: caller-owned Future - do not hand the shared object to the library
# --------------------------------------------------------------------------- #
class BusinessService:
    """Some other subsystem owns this Future and keeps its own reference."""

    def __init__(self):
        self._future: asyncio.Future[str] | None = None
        self.started = asyncio.Event()

    def start(self) -> asyncio.Future[str]:
        self._future = asyncio.Future()
        self.started.set()
        return self._future  # the business layer retains a reference too

    async def resolver_naive_await(self, _info):
        # UNSAFE for a shared Future: cancelling this Task propagates the
        # CancelledError through the plain await and cancels self._future too.
        future = self.start()
        return await future

    async def resolver_shielded(self, _info):
        # SAFE: shield absorbs the Task cancellation; only the outer await is
        # cancelled while the inner, business-owned Future keeps running.
        future = self.start()
        return await asyncio.shield(future)

    def settle_later(self, value: str) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(value)


# --------------------------------------------------------------------------- #
# Pattern 3: cooperative resolver listening on the signal (no CancelledError)
# --------------------------------------------------------------------------- #
async def cooperative_resolver(info):
    # info.abort_signal is the same object passed to execute(); resolvers that
    # want to wind down gracefully instead of being cancelled can await it.
    await info.abort_signal.wait()
    return None  # signal_observed; return through finally as usual


# --------------------------------------------------------------------------- #
# Pattern 4: synchronous, uninterruptible work - poll the flag between chunks
# --------------------------------------------------------------------------- #
def blocking_sync_resolver(info):
    for chunk in range(10):
        time.sleep(0.01)  # this chunk itself cannot be interrupted
        if info.abort_signal is not None and info.abort_signal.aborted:
            # Cooperative early exit; the abort is otherwise only seen by the
            # executor at the next field boundary or at finish().
            return None
    return "finished"


# --------------------------------------------------------------------------- #
# Pattern 5: subscriptions - always aclose() the response stream
# --------------------------------------------------------------------------- #
async def event_source():
    try:
        for index in range(100):
            await asyncio.sleep(30)
            yield {"feed": {"event": index}}
    finally:
        print("  subscription source closed")


async def run_subscription(controller: AbortController) -> None:
    maybe_stream = subscribe(
        SCHEMA,
        parse("subscription { feed { event } }"),
        abort_signal=controller.signal,
        root_value={"feed": lambda _info: event_source()},
    )
    stream = await maybe_stream if asyncio.iscoroutine(maybe_stream) else maybe_stream
    try:
        async for _event in stream:
            pass  # process events
    except Exception as error:
        # On abort, the pending __anext__ raises the abort reason directly
        # (not an AbortedGraphQLExecutionError) for subscription streams.
        if not controller.signal.aborted:
            raise
        print(f"  subscription stopped by abort: {error}")
    finally:
        # Deterministic cleanup: closes the cancellable wrapper and the source.
        await stream.aclose()


async def main() -> None:
    # --- Patterns 1-4 through execute() ---
    service = BusinessService()
    controller = AbortController()
    task = asyncio.ensure_future(
        execute(
            SCHEMA,
            parse("{ owned shared cooperative blocking }"),
            abort_signal=controller.signal,
            root_value={
                "owned": owned_coroutine_resolver,
                "shared": service.resolver_shielded,
                "cooperative": cooperative_resolver,
                "blocking": blocking_sync_resolver,
            },
        )
    )
    # Wait until the in-flight resolvers are actually suspended, then abort.
    await service.started.wait()
    for _ in range(3):
        await asyncio.sleep(0)
    controller.abort(RuntimeError("client disconnected"))
    try:
        await task
    except AbortedGraphQLExecutionError as error:
        # The partial result is safe to await (it settles in the background
        # even if dropped), but awaiting it gives you the partial data.
        partial = await error.aborted_result
        print("aborted; partial data:", partial.data)

    # The business-owned Future was never cancelled by the library.
    assert service._future is not None and not service._future.cancelled()
    service.settle_later("settled by owner")
    print("shared future result:", service._future.result())

    # --- Pattern 5: subscription cleanup ---
    sub_controller = AbortController()
    sub_task = asyncio.ensure_future(run_subscription(sub_controller))
    await asyncio.sleep(0)
    sub_controller.abort(RuntimeError("client disconnected"))
    await sub_task
    print("subscription cleaned up without orphans")


if __name__ == "__main__":
    asyncio.run(main())
