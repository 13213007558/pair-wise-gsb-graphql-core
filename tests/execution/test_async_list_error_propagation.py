"""Tests for concurrent async list resolvers that raise non-null errors.

When several asynchronous resolvers of a list field settle concurrently and
some of them raise errors that hit non-null positions, execution must keep
the visible data the specification allows, propagate null across the correct
list and object boundaries, and produce result semantics that do not depend
on the completion order of the resolvers. An event barrier gates the
resolvers and records the order in which they start, complete and are
cancelled, so that different completion orders can be exercised
deterministically and leftover asynchronous work can be asserted to be
cleaned up.
"""

from __future__ import annotations

import asyncio
from asyncio import Event, ensure_future, sleep
from typing import TYPE_CHECKING, Any

import pytest

from graphql.error import GraphQLError
from graphql.execution import AbortedGraphQLExecutionError, execute
from graphql.language import parse
from graphql.pyutils import AbortController, is_awaitable
from graphql.type import (
    GraphQLField,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from graphql.execution import ExecutionResult
    from graphql.type import GraphQLFieldResolver, GraphQLOutputType

pytestmark = pytest.mark.anyio


class EventBarrier:
    """A barrier that gates async resolvers and records completion order.

    Each labelled resolver waits until its gate is released, so a test can
    control the exact completion order of concurrent resolvers, while the
    barrier records the order in which the resolvers start, complete and are
    cancelled.
    """

    def __init__(self) -> None:
        self.started: list[str] = []
        self.completed: list[str] = []
        self.cancelled: list[str] = []
        self._gates: dict[str, Event] = {}
        self._started_event = Event()

    def gate(self, label: str) -> Event:
        return self._gates.setdefault(label, Event())

    async def wait(self, label: str) -> None:
        """Wait inside a resolver until its gate has been released."""
        self.started.append(label)
        self._started_event.set()
        try:
            await self.gate(label).wait()
        except asyncio.CancelledError:
            self.cancelled.append(label)
            raise
        self.completed.append(label)

    async def wait_started(self, *labels: str) -> None:
        """Wait until the resolvers with the given labels have all started."""
        while not set(labels).issubset(self.started):
            self._started_event.clear()
            await self._started_event.wait()

    def release(self, *labels: str) -> None:
        """Allow the resolvers with the given labels to complete."""
        for label in labels:
            self.gate(label).set()


def current_tasks() -> set[asyncio.Task[Any]]:
    """Get the current set of asyncio tasks as a baseline snapshot."""
    return asyncio.all_tasks()


def leftover_tasks(baseline: set[asyncio.Task[Any]]) -> list[asyncio.Task[Any]]:
    """Get all pending asyncio tasks that were created after the baseline."""
    return [
        task for task in asyncio.all_tasks() if task not in baseline and not task.done()
    ]


def make_item(
    barrier: EventBarrier,
    label: str,
    value: Any = None,
    error: Exception | None = None,
) -> Awaitable[Any]:
    """Create an awaitable list item that is gated by the barrier."""

    async def item() -> Any:
        await barrier.wait(label)
        if error is not None:
            raise error
        return value

    return item()


def make_list_resolver(
    barrier: EventBarrier, entries: list[tuple[str, Any]]
) -> GraphQLFieldResolver:
    """Create a list field resolver returning gated awaitable items.

    Each entry is a ``(label, value)`` pair; if the value is an exception, the
    corresponding item raises it instead of returning it.
    """

    def resolve(_root: Any, _info: Any) -> list[Awaitable[Any]]:
        return [
            make_item(barrier, label, error=value)
            if isinstance(value, Exception)
            else make_item(barrier, label, value=value)
            for label, value in entries
        ]

    return resolve


def make_schema(
    items_type: GraphQLOutputType,
    items_resolver: GraphQLFieldResolver,
    extra_fields: dict[str, GraphQLField] | None = None,
) -> GraphQLSchema:
    """Build a schema with a single ``items`` list field on the query type."""
    fields = {"items": GraphQLField(items_type, resolve=items_resolver)}
    if extra_fields:
        fields.update(extra_fields)
    return GraphQLSchema(GraphQLObjectType("Query", fields))


def error_summaries(
    result: ExecutionResult,
) -> list[tuple[str, tuple[Any, ...], tuple[tuple[int, int], ...]]]:
    """Get the errors of a result as summaries sorted by message and path."""
    return sorted(
        (
            error.message,
            tuple(error.path or ()),
            tuple(
                (location.line, location.column) for location in error.locations or ()
            ),
        )
        for error in result.errors or ()
    )


def execute_as_task(
    schema: GraphQLSchema, query: str, **kwargs: Any
) -> asyncio.Task[ExecutionResult]:
    """Start executing a query in a new asyncio task."""
    result = execute(schema, parse(query), **kwargs)
    assert is_awaitable(result)
    return ensure_future(result)


boom_b = RuntimeError("boom-b")
boom_d = RuntimeError("boom-d")

string_non_null = GraphQLNonNull(GraphQLString)
list_of_non_null_string = GraphQLNonNull(GraphQLList(string_non_null))
non_null_list_of_string = GraphQLNonNull(GraphQLList(GraphQLString))
list_of_string_non_null = GraphQLList(string_non_null)


def describe_async_list_items_with_non_null_errors():
    entries = [("a", "A"), ("b", boom_b), ("c", "C"), ("d", boom_d)]

    async def nulls_out_non_null_list_on_simultaneous_item_failures():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            list_of_non_null_string, make_list_resolver(barrier, entries)
        )
        task = execute_as_task(schema, "{ items }")
        await barrier.wait_started("a", "b", "c", "d")
        barrier.release("a", "b", "c", "d")
        result = await task

        # all resolvers completed in the order their gates were released
        assert barrier.completed == ["a", "b", "c", "d"]
        assert barrier.cancelled == []
        # the first failure propagates through the non-null item type and
        # the non-null list type up to the nullable root, nulling all data
        assert result == (
            None,
            [{"message": "boom-b", "path": ["items", 1], "locations": [(1, 3)]}],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []

    async def propagates_only_the_first_completed_failure_for_non_null_lists():
        baseline = current_tasks()
        results = []
        barriers = []
        for order in (("a", "b", "c", "d"), ("d", "c", "b", "a")):
            barrier = EventBarrier()
            barriers.append(barrier)
            schema = make_schema(
                list_of_non_null_string, make_list_resolver(barrier, entries)
            )
            task = execute_as_task(schema, "{ items }")
            for label in order:
                barrier.release(label)
                await sleep(0)
                await sleep(0)
            results.append(await task)
            await sleep(0)
            # no resolver task is left behind, whatever the completion order
            assert leftover_tasks(baseline) == []

        first, second = results
        # the data shape and the error count do not depend on the order in
        # which the failing resolvers complete
        assert first.data is None
        assert second.data is None
        assert first.errors is not None
        assert second.errors is not None
        assert len(first.errors) == 1
        assert len(second.errors) == 1
        # the recorded error is the failure that completed first
        assert error_summaries(first) == [("boom-b", ("items", 1), ((1, 3),))]
        assert error_summaries(second) == [("boom-d", ("items", 3), ((1, 3),))]
        # resolvers that were still pending got cancelled, none completed twice
        for barrier in barriers:
            assert not set(barrier.completed) & set(barrier.cancelled)

    async def nulls_only_failed_items_of_nullable_item_list_in_any_order():
        baseline = current_tasks()
        results = []
        for order in (("a", "b", "c", "d"), ("d", "c", "b", "a")):
            barrier = EventBarrier()
            schema = make_schema(
                non_null_list_of_string, make_list_resolver(barrier, entries)
            )
            task = execute_as_task(schema, "{ items }")
            await barrier.wait_started("a", "b", "c", "d")
            for label in order:
                barrier.release(label)
                await sleep(0)
            result = await task

            # the resolvers completed in exactly the release order and no
            # resolver had to be cancelled, since nullable items do not
            # propagate errors and therefore do not cancel their siblings
            assert barrier.completed == list(order)
            assert barrier.cancelled == []
            results.append(result)
            await sleep(0)
            assert leftover_tasks(baseline) == []

        first, second = results
        # the completion order does not change the result semantics: the
        # visible items are kept and each failure is reported individually
        assert first.data == second.data == {"items": ["A", None, "C", None]}
        assert len(first.errors or ()) == 2
        assert len(second.errors or ()) == 2
        assert (
            error_summaries(first)
            == error_summaries(second)
            == [
                ("boom-b", ("items", 1), ((1, 3),)),
                ("boom-d", ("items", 3), ((1, 3),)),
            ]
        )

    async def propagates_null_across_nested_list_boundaries():
        baseline = current_tasks()

        def resolve_nested(barrier: EventBarrier) -> GraphQLFieldResolver:
            def resolve(_root: Any, _info: Any) -> list[list[Awaitable[Any]]]:
                return [
                    [
                        make_item(barrier, "a0", value="a0"),
                        make_item(barrier, "a1", value="a1"),
                    ],
                    [
                        make_item(barrier, "b0", value="b0"),
                        make_item(barrier, "b1", error=boom_b),
                    ],
                    [make_item(barrier, "c0", value="c0")],
                ]

            return resolve

        async def run(items_type: GraphQLOutputType) -> ExecutionResult:
            barrier = EventBarrier()
            schema = make_schema(items_type, resolve_nested(barrier))
            task = execute_as_task(schema, "{ items }")
            await barrier.wait_started("a0", "a1", "b0", "b1", "c0")
            barrier.release("a0", "a1", "b0", "b1", "c0")
            result = await task
            await sleep(0)
            assert leftover_tasks(baseline) == []
            return result

        # [[String!]]: a failing inner item nulls only its own inner list
        result = await run(GraphQLList(GraphQLList(string_non_null)))
        assert result == (
            {"items": [["a0", "a1"], None, ["c0"]]},
            [{"message": "boom-b", "path": ["items", 1, 1], "locations": [(1, 3)]}],
        )

        # [[String!]!]: the non-null inner list propagates the null to the
        # outer list, which is nullable and therefore absorbs it
        result = await run(GraphQLList(GraphQLNonNull(GraphQLList(string_non_null))))
        assert result == (
            {"items": None},
            [{"message": "boom-b", "path": ["items", 1, 1], "locations": [(1, 3)]}],
        )

        # [[String!]!]!: the null propagates all the way up to the root
        result = await run(
            GraphQLNonNull(GraphQLList(GraphQLNonNull(GraphQLList(string_non_null))))
        )
        assert result == (
            None,
            [{"message": "boom-b", "path": ["items", 1, 1], "locations": [(1, 3)]}],
        )

    async def tracks_errors_per_alias_for_aliased_list_fields():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            non_null_list_of_string,
            make_list_resolver(barrier, [("a", "A"), ("b", boom_b)]),
        )
        task = execute_as_task(schema, "{ first: items second: items }")
        await barrier.wait_started("a", "b")
        barrier.release("a", "b")
        result = await task

        # each aliased field is completed independently and reports its own
        # error with the aliased path and the location of its own field node
        assert result.data == {"first": ["A", None], "second": ["A", None]}
        assert error_summaries(result) == [
            ("boom-b", ("first", 1), ((1, 3),)),
            ("boom-b", ("second", 1), ((1, 16),)),
        ]
        await sleep(0)
        assert leftover_tasks(baseline) == []

    async def keeps_visible_sibling_data_when_a_list_field_nulls_out():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            list_of_string_non_null,
            make_list_resolver(barrier, [("a", "A"), ("b", boom_b)]),
            extra_fields={
                "other": GraphQLField(GraphQLString, resolve=lambda *_args: "visible")
            },
        )
        task = execute_as_task(schema, "{ items other }")
        await barrier.wait_started("a", "b")
        barrier.release("a", "b")
        result = await task

        # the failing item nulls the whole nullable list field, but the
        # sibling field remains visible
        assert result == (
            {"items": None, "other": "visible"},
            [{"message": "boom-b", "path": ["items", 1], "locations": [(1, 3)]}],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []


def make_item_type(barrier: EventBarrier) -> GraphQLObjectType:
    """Create an object type with a gated async non-null value field."""

    async def resolve_value(obj: dict[str, Any], _info: Any) -> str:
        await barrier.wait(obj["label"])
        if "error" in obj:
            raise obj["error"]
        return obj["value"]

    return GraphQLObjectType(
        "Item", {"value": GraphQLField(string_non_null, resolve=resolve_value)}
    )


def describe_async_object_items_with_non_null_errors():
    objects = [
        {"label": "i0", "value": "zero"},
        {"label": "i1", "error": boom_b},
        {"label": "i2", "value": "two"},
    ]

    async def nulls_the_object_item_when_its_non_null_field_fails():
        baseline = current_tasks()
        barrier = EventBarrier()
        item_type = make_item_type(barrier)
        schema = make_schema(
            GraphQLNonNull(GraphQLList(item_type)), lambda _root, _info: objects
        )
        task = execute_as_task(schema, "{ items { value } }")
        await barrier.wait_started("i0", "i1", "i2")
        barrier.release("i0", "i1", "i2")
        result = await task

        # the error propagates across the object boundary of the failing
        # item and is absorbed by the nullable list item position
        assert result == (
            {"items": [{"value": "zero"}, None, {"value": "two"}]},
            [
                {
                    "message": "boom-b",
                    "path": ["items", 1, "value"],
                    "locations": [(1, 11)],
                }
            ],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []

    async def nulls_the_list_when_non_null_object_items_fail():
        baseline = current_tasks()
        barrier = EventBarrier()
        item_type = make_item_type(barrier)
        schema = make_schema(
            GraphQLList(GraphQLNonNull(item_type)), lambda _root, _info: objects
        )
        task = execute_as_task(schema, "{ items { value } }")
        await barrier.wait_started("i0", "i1", "i2")
        barrier.release("i0", "i1", "i2")
        result = await task

        # the error propagates across the object boundary and the non-null
        # item position up to the nullable list field
        assert result == (
            {"items": None},
            [
                {
                    "message": "boom-b",
                    "path": ["items", 1, "value"],
                    "locations": [(1, 11)],
                }
            ],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []


def describe_async_abstract_type_completion():
    async def completes_abstract_items_and_propagates_item_errors():
        baseline = current_tasks()
        barrier = EventBarrier()

        async def resolve_pet_type(
            obj: dict[str, Any], _info: Any, _abstract_type: Any
        ) -> str:
            await sleep(0)
            return obj["kind"]

        async def resolve_name(obj: dict[str, Any], _info: Any) -> str:
            await barrier.wait(f"name-{obj['name']}")
            if obj["name"] == "bad":
                raise boom_b
            return obj["name"]

        pet_type = GraphQLInterfaceType(
            "Pet",
            {"name": GraphQLField(string_non_null, resolve=resolve_name)},
            resolve_type=resolve_pet_type,
        )
        dog_type = GraphQLObjectType(
            "Dog",
            {"name": GraphQLField(string_non_null, resolve=resolve_name)},
            interfaces=[pet_type],
        )
        pets = [{"kind": "Dog", "name": "rex"}, {"kind": "Dog", "name": "bad"}]
        query_type = GraphQLObjectType(
            "Query",
            {
                "pets": GraphQLField(
                    GraphQLNonNull(GraphQLList(pet_type)),
                    resolve=lambda _root, _info: pets,
                )
            },
        )
        schema = GraphQLSchema(query_type, types=[dog_type])
        task = execute_as_task(schema, "{ pets { name } }")
        await barrier.wait_started("name-rex", "name-bad")
        barrier.release("name-rex", "name-bad")
        result = await task

        # the abstract type of each item is resolved asynchronously and the
        # failing item is nulled at its nullable list item position
        assert result == (
            {"pets": [{"name": "rex"}, None]},
            [
                {
                    "message": "boom-b",
                    "path": ["pets", 1, "name"],
                    "locations": [(1, 10)],
                }
            ],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []


def describe_middleware_with_async_list_resolvers():
    async def middleware_wrapping_preserves_list_error_propagation():
        baseline = current_tasks()
        calls = []

        async def logging_middleware(next_, root, info, **args):
            calls.append(("enter", info.field_name))
            result = next_(root, info, **args)
            if is_awaitable(result):
                result = await result
            calls.append(("exit", info.field_name))
            return result

        async def run(middleware: Any = None) -> ExecutionResult:
            barrier = EventBarrier()

            async def resolve_items(_root: Any, _info: Any) -> list[Awaitable[Any]]:
                return [
                    make_item(barrier, "a", value="A"),
                    make_item(barrier, "b", error=boom_b),
                ]

            schema = make_schema(
                non_null_list_of_string,
                resolve_items,
                extra_fields={
                    "other": GraphQLField(GraphQLString, resolve=lambda *_args: "v")
                },
            )
            task = execute_as_task(schema, "{ items other }", middleware=middleware)
            await barrier.wait_started("a", "b")
            barrier.release("a", "b")
            result = await task
            await sleep(0)
            assert leftover_tasks(baseline) == []
            return result

        plain_result = await run()
        wrapped_result = await run(middleware=[logging_middleware])

        # the middleware wrapped each field resolver exactly once, awaiting
        # the async resolver and passing through the synchronous one
        assert calls == [
            ("enter", "items"),
            ("exit", "items"),
            ("enter", "other"),
            ("exit", "other"),
        ]
        # and the result is the same as without middleware
        assert wrapped_result.formatted == plain_result.formatted
        assert plain_result == (
            {"items": ["A", None], "other": "v"},
            [{"message": "boom-b", "path": ["items", 1], "locations": [(1, 3)]}],
        )


def describe_mutation_with_async_resolvers():
    async def executes_top_level_mutation_fields_serially():
        baseline = current_tasks()
        barrier = EventBarrier()

        def make_mutation_resolver(
            label: str, value: Any = None, error: Exception | None = None
        ) -> GraphQLFieldResolver:
            async def resolve(_root: Any, _info: Any) -> Any:
                await barrier.wait(label)
                if error is not None:
                    raise error
                return value

            return resolve

        query_type = GraphQLObjectType("Query", {"ok": GraphQLField(GraphQLString)})
        mutation_type = GraphQLObjectType(
            "Mutation",
            {
                "first": GraphQLField(
                    GraphQLString,
                    resolve=make_mutation_resolver("first", value="one"),
                ),
                "second": GraphQLField(
                    GraphQLString,
                    resolve=make_mutation_resolver("second", error=boom_b),
                ),
                "third": GraphQLField(
                    GraphQLString,
                    resolve=make_mutation_resolver("third", value="three"),
                ),
            },
        )
        schema = GraphQLSchema(query_type, mutation_type)
        task = execute_as_task(schema, "mutation { first second third }")

        # each resolver starts only after the previous one has completed
        await barrier.wait_started("first")
        assert barrier.started == ["first"]
        assert barrier.completed == []
        barrier.release("first")

        await barrier.wait_started("second")
        assert barrier.started == ["first", "second"]
        assert barrier.completed == ["first"]
        barrier.release("second")

        await barrier.wait_started("third")
        assert barrier.started == ["first", "second", "third"]
        assert barrier.completed == ["first", "second"]
        barrier.release("third")

        result = await task
        assert barrier.completed == ["first", "second", "third"]
        assert barrier.cancelled == []
        # the failing field is nulled, but the serial execution continues
        assert result == (
            {"first": "one", "second": None, "third": "three"},
            [{"message": "boom-b", "path": ["second"], "locations": [(1, 18)]}],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []


def describe_cancellation_of_async_list_resolvers():
    async def abort_cancels_pending_resolvers_and_cleans_up_tasks():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            non_null_list_of_string,
            make_list_resolver(barrier, [("x", "X"), ("y", "Y")]),
        )
        abort_controller = AbortController()
        task = execute_as_task(
            schema, "{ items }", abort_signal=abort_controller.signal
        )
        await barrier.wait_started("x", "y")
        abort_controller.abort()

        with pytest.raises(AbortedGraphQLExecutionError) as exc_info:
            await task
        assert str(exc_info.value) == "This operation was aborted"

        # the aborted execution still exposes its partial result
        aborted_result = exc_info.value.aborted_result
        assert is_awaitable(aborted_result)
        aborted_result = await aborted_result

        # both gated resolvers were cancelled while waiting on the barrier
        assert sorted(barrier.cancelled) == ["x", "y"]
        assert barrier.completed == []
        assert aborted_result.data == {"items": [None, None]}
        assert error_summaries(aborted_result) == [
            ("This operation was aborted", ("items", 0), ((1, 3),)),
            ("This operation was aborted", ("items", 1), ((1, 3),)),
        ]

        # no resolver or background task is left running
        await sleep(0)
        assert leftover_tasks(baseline) == []


def serialize_odd(value: int) -> int:
    """Serialize only even values, raising a GraphQLError for odd ones."""
    if value % 2:
        raise GraphQLError(f"Cannot serialize odd value: {value}")
    return value


odd_int = GraphQLScalarType("OddInt", serialize=serialize_odd)


def describe_custom_scalar_serialize_errors_in_async_lists():
    async def nulls_the_item_when_serialization_fails():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            GraphQLNonNull(GraphQLList(odd_int)),
            make_list_resolver(barrier, [("n0", 2), ("n1", 3), ("n2", 4)]),
        )
        task = execute_as_task(schema, "{ items }")
        await barrier.wait_started("n0", "n1", "n2")
        barrier.release("n0", "n1", "n2")
        result = await task

        # the serialization error is reported like a resolver error and the
        # neighbouring items remain visible
        assert result == (
            {"items": [2, None, 4]},
            [
                {
                    "message": "Cannot serialize odd value: 3",
                    "path": ["items", 1],
                    "locations": [(1, 3)],
                }
            ],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []

    async def propagates_serialization_errors_of_non_null_items():
        baseline = current_tasks()
        barrier = EventBarrier()
        schema = make_schema(
            GraphQLList(GraphQLNonNull(odd_int)),
            make_list_resolver(barrier, [("n0", 2), ("n1", 3)]),
        )
        task = execute_as_task(schema, "{ items }")
        await barrier.wait_started("n0", "n1")
        barrier.release("n0", "n1")
        result = await task

        # the serialization error propagates through the non-null item type
        # and nulls the whole nullable list field
        assert result == (
            {"items": None},
            [
                {
                    "message": "Cannot serialize odd value: 3",
                    "path": ["items", 1],
                    "locations": [(1, 3)],
                }
            ],
        )
        await sleep(0)
        assert leftover_tasks(baseline) == []
