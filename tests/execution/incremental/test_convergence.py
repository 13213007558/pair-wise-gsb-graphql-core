"""Convergence tests for incremental execution.

The ``@defer``/``@stream`` labels, paths, fragment expansions and cancellation
signals reach the incremental executor at different stages. These tests assert
that the pending/completed events always converge - even when a shared fragment
is expanded in several positions, labels are missing or duplicated, a source or
resolver fails, the consumer closes the result stream, or an abort signal
fires - so that a consumer never waits forever for a final payload with
``hasNext: False`` and no asynchronous work is left behind.
"""

from __future__ import annotations

from asyncio import CancelledError, Event, Future, all_tasks, ensure_future, sleep
from typing import TYPE_CHECKING, Any

import pytest

from graphql.execution import (
    ExecutionResult,
    ExperimentalIncrementalExecutionResults,
    experimental_execute_incrementally,
)
from graphql.language import parse
from graphql.pyutils import AbortController, AbortError, is_awaitable
from graphql.type import (
    GraphQLField,
    GraphQLID,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
)
from graphql.utilities import build_schema
from graphql.validation import DeferStreamDirectiveLabel, validate

if TYPE_CHECKING:
    from asyncio import Task

    from graphql.language import DocumentNode

pytestmark = pytest.mark.anyio


friend_type = GraphQLObjectType(
    "Friend",
    lambda: {
        "id": GraphQLField(GraphQLID),
        "name": GraphQLField(GraphQLString),
        "nick": GraphQLField(GraphQLString),
        "friends": GraphQLField(GraphQLList(friend_type)),
    },
)


def raise_bad(_source: Any, _info: Any) -> str:
    """A resolver that always raises an error."""
    raise RuntimeError("bad")


obj_type = GraphQLObjectType(
    "Obj",
    {
        "ok": GraphQLField(GraphQLString),
        "bad": GraphQLField(GraphQLString, resolve=raise_bad),
        "badNonNull": GraphQLField(GraphQLNonNull(GraphQLString), resolve=raise_bad),
    },
)

query_type = GraphQLObjectType(
    "Query",
    lambda: {
        "hero": GraphQLField(friend_type),
        "friends": GraphQLField(GraphQLList(friend_type)),
        "obj": GraphQLField(obj_type),
    },
)

schema = GraphQLSchema(query_type)


async def drain(
    document: DocumentNode,
    root_value: Any = None,
    variable_values: dict[str, Any] | None = None,
    enable_early_execution: bool = False,
) -> Any:
    """Execute a document and drain all incremental results."""
    result = experimental_execute_incrementally(
        schema,
        document,
        root_value,
        variable_values=variable_values,
        enable_early_execution=enable_early_execution,
    )
    if is_awaitable(result):
        result = await result
    if isinstance(result, ExperimentalIncrementalExecutionResults):
        return [result.initial_result.formatted] + [
            patch.formatted async for patch in result.subsequent_results
        ]
    assert isinstance(result, ExecutionResult)
    return result.formatted


def assert_converged(payloads: list[dict[str, Any]]) -> None:
    """Assert that a drained payload stream converged to a terminal state.

    Every payload but the last must signal more payloads, the last payload
    must end the stream, and every announced pending id must be completed
    exactly once.
    """
    assert len(payloads) > 1
    assert payloads[0]["hasNext"] is True
    *middle, last = payloads[1:]
    assert all(payload["hasNext"] is True for payload in middle)
    assert last["hasNext"] is False
    pending_ids = [
        pending["id"] for payload in payloads for pending in payload.get("pending", [])
    ]
    completed_ids = [
        completed["id"]
        for payload in payloads
        for completed in payload.get("completed", [])
    ]
    assert sorted(completed_ids) == sorted(pending_ids)


def snapshot_tasks() -> set[Task[Any]]:
    """Snapshot the currently running tasks."""
    return all_tasks()


async def assert_no_residual_tasks(before: set[Task[Any]]) -> None:
    """Assert that no tasks started after the snapshot are still pending."""
    for _ in range(10):
        await sleep(0)
    residual = [task for task in all_tasks() if task not in before]
    assert not residual


def describe_shared_fragment_convergence():
    """Shared fragments expanded in several positions must converge."""

    @pytest.mark.timeout(5)
    async def delivers_shared_fragment_fields_once_across_sibling_defers():
        """Fields of a fragment shared by sibling defers are delivered once."""
        document = parse(
            """
            query {
              hero {
                id
                ... @defer(label: "d1") { ...F }
                ... @defer(label: "d2") { ...F nick }
              }
            }
            fragment F on Friend { name }
            """
        )
        before = snapshot_tasks()
        hero = {"id": "1", "name": "Luke", "nick": "L"}
        payloads = await drain(document, {"hero": hero})

        assert payloads == [
            {
                "data": {"hero": {"id": "1"}},
                "pending": [
                    {"id": "0", "path": ["hero"], "label": "d1"},
                    {"id": "1", "path": ["hero"], "label": "d2"},
                ],
                "hasNext": True,
            },
            {
                "incremental": [
                    {"data": {"name": "Luke"}, "id": "0"},
                    {"data": {"nick": "L"}, "id": "1"},
                ],
                "completed": [{"id": "0"}, {"id": "1"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def delivers_shared_fragment_fields_once_with_initial_selection():
        """Fields already in the initial selection are not delivered again."""
        document = parse(
            """
            query {
              hero {
                id
                ...F
                ... @defer(label: "d1") { ...F nick }
              }
            }
            fragment F on Friend { name }
            """
        )
        before = snapshot_tasks()
        hero = {"id": "1", "name": "Luke", "nick": "L"}
        payloads = await drain(document, {"hero": hero})

        assert payloads == [
            {
                "data": {"hero": {"id": "1", "name": "Luke"}},
                "pending": [{"id": "0", "path": ["hero"], "label": "d1"}],
                "hasNext": True,
            },
            {
                "incremental": [{"data": {"nick": "L"}, "id": "0"}],
                "completed": [{"id": "0"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


def describe_aliased_list_convergence():
    """Aliased selections of the same list field must converge."""

    @pytest.mark.timeout(5)
    async def converges_when_same_list_field_is_streamed_under_one_alias():
        """A streamed alias and a plain alias of one field both complete."""
        document = parse(
            """
            query {
              streamed: friends @stream(initialCount: 1) { id }
              plain: friends { id }
            }
            """
        )
        before = snapshot_tasks()
        root_value = {"friends": [{"id": "1"}, {"id": "2"}]}
        payloads = await drain(document, root_value)

        assert payloads == [
            {
                "data": {
                    "streamed": [{"id": "1"}],
                    "plain": [{"id": "1"}, {"id": "2"}],
                },
                "pending": [{"id": "0", "path": ["streamed"]}],
                "hasNext": True,
            },
            {
                "incremental": [{"items": [{"id": "2"}], "id": "0"}],
                "completed": [{"id": "0"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


def describe_nested_stream_convergence():
    """Streams nested inside streamed items must converge."""

    @pytest.mark.timeout(5)
    async def converges_with_streams_nested_inside_streamed_items():
        """Inner streams are announced, delivered and completed in order."""
        document = parse(
            """
            query {
              friends @stream(initialCount: 1) {
                id
                friends @stream(initialCount: 0) { name }
              }
            }
            """
        )
        before = snapshot_tasks()
        root_value = {
            "friends": [
                {"id": "1", "friends": [{"name": "a"}, {"name": "b"}]},
                {"id": "2", "friends": [{"name": "c"}]},
            ]
        }
        payloads = await drain(document, root_value)

        assert payloads == [
            {
                "data": {"friends": [{"id": "1", "friends": []}]},
                "pending": [
                    {"id": "0", "path": ["friends", 0, "friends"]},
                    {"id": "1", "path": ["friends"]},
                ],
                "hasNext": True,
            },
            {
                "pending": [{"id": "2", "path": ["friends", 1, "friends"]}],
                "incremental": [
                    {"items": [{"name": "a"}, {"name": "b"}], "id": "0"},
                    {"items": [{"id": "2", "friends": []}], "id": "1"},
                ],
                "completed": [{"id": "0"}, {"id": "1"}],
                "hasNext": True,
            },
            {
                "incremental": [{"items": [{"name": "c"}], "id": "2"}],
                "completed": [{"id": "2"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


validation_schema = build_schema(
    """
    type Query {
      hero: Hero
      friends: [String]
    }
    type Hero {
      id: ID
      name: String
    }
    """
)


def validate_labels(source: str) -> list[dict[str, Any]]:
    """Validate a document against the defer/stream label rule only."""
    return [
        {"message": error.message, "locations": error.locations}
        for error in validate(
            validation_schema, parse(source), rules=(DeferStreamDirectiveLabel,)
        )
    ]


def describe_label_convergence():
    """Missing or duplicated labels must not break convergence."""

    def allows_missing_and_null_labels():
        """Unlabeled and null-labeled directives are valid."""
        assert (
            validate_labels(
                """
            query {
              hero {
                ... @defer { name }
                ... @defer(label: null) { id }
              }
              friends @stream(initialCount: 0)
              friends: friends @stream(label: null, initialCount: 0)
            }
            """
            )
            == []
        )

    def reports_duplicate_labels_with_both_locations():
        """Duplicated labels are reported at both directive positions."""
        errors = validate_labels(
            """
            query {
              hero {
                ... @defer(label: "dup") { name }
                ... @defer(label: "dup") { id }
              }
            }
            """
        )
        assert errors == [
            {
                "message": "Defer/Stream directive label argument must be unique.",
                "locations": [(4, 21), (5, 21)],
            }
        ]

    def reports_duplicate_labels_across_defer_and_stream():
        """A label shared by @defer and @stream is reported as duplicated."""
        errors = validate_labels(
            """
            query {
              hero {
                ... @defer(label: "shared") { name }
              }
              friends @stream(label: "shared", initialCount: 0)
            }
            """
        )
        assert errors == [
            {
                "message": "Defer/Stream directive label argument must be unique.",
                "locations": [(4, 21), (6, 23)],
            }
        ]

    def reports_non_static_label_location():
        """A non-static label is reported at the label argument position."""
        errors = validate_labels(
            """
            query ($label: String) {
              hero {
                ... @defer(label: $label) { name }
              }
            }
            """
        )
        assert errors == [
            {
                "message": "Defer directive label argument must be a static string.",
                "locations": [(4, 21)],
            }
        ]

    @pytest.mark.timeout(5)
    async def converges_when_duplicate_labels_reach_execution():
        """Execution still converges when duplicate labels are not validated.

        The query is spec-invalid and would be rejected by validation, but
        ``experimental_execute_incrementally`` may be called
        without a prior validation pass; the label conflict must not make the
        consumer wait forever for ``hasNext: False``.
        """
        document = parse(
            """
            query {
              hero {
                ... @defer(label: "dup") { name }
                ... @defer(label: "dup") { id }
              }
            }
            """
        )
        before = snapshot_tasks()
        payloads = await drain(document, {"hero": {"id": "1", "name": "Luke"}})

        assert payloads == [
            {
                "data": {"hero": {}},
                "pending": [
                    {"id": "0", "path": ["hero"], "label": "dup"},
                    {"id": "1", "path": ["hero"], "label": "dup"},
                ],
                "hasNext": True,
            },
            {
                "incremental": [
                    {"data": {"name": "Luke"}, "id": "0"},
                    {"data": {"id": "1"}, "id": "1"},
                ],
                "completed": [{"id": "0"}, {"id": "1"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


def describe_variable_controlled_directives():
    """Directives controlled via variable ``if`` arguments converge."""

    @pytest.mark.timeout(5)
    async def converges_when_defer_is_enabled_or_disabled_by_variable():
        """The ``if`` variable decides whether a defer is pending."""
        document = parse(
            """
            query ($defer: Boolean!) {
              hero {
                id
                ... @defer(if: $defer, label: "d") { name }
              }
            }
            """
        )
        root_value = {"hero": {"id": "1", "name": "Luke"}}

        before = snapshot_tasks()
        result = await drain(document, root_value, {"defer": False})
        assert result == {"data": {"hero": {"id": "1", "name": "Luke"}}}
        await assert_no_residual_tasks(before)

        before = snapshot_tasks()
        payloads = await drain(document, root_value, {"defer": True})
        assert payloads == [
            {
                "data": {"hero": {"id": "1"}},
                "pending": [{"id": "0", "path": ["hero"], "label": "d"}],
                "hasNext": True,
            },
            {
                "incremental": [{"data": {"name": "Luke"}, "id": "0"}],
                "completed": [{"id": "0"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def converges_when_stream_is_enabled_or_disabled_by_variable():
        """The ``if`` variable decides whether a stream is pending."""
        document = parse(
            """
            query ($stream: Boolean!) {
              friends @stream(if: $stream, initialCount: 1, label: "s") { id }
            }
            """
        )
        root_value = {"friends": [{"id": "1"}, {"id": "2"}]}

        before = snapshot_tasks()
        result = await drain(document, root_value, {"stream": False})
        assert result == {"data": {"friends": [{"id": "1"}, {"id": "2"}]}}
        await assert_no_residual_tasks(before)

        before = snapshot_tasks()
        payloads = await drain(document, root_value, {"stream": True})
        assert payloads == [
            {
                "data": {"friends": [{"id": "1"}]},
                "pending": [{"id": "0", "path": ["friends"], "label": "s"}],
                "hasNext": True,
            },
            {
                "incremental": [{"items": [{"id": "2"}], "id": "0"}],
                "completed": [{"id": "0"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


def describe_partial_error_convergence():
    """Failing sources and resolvers must not break convergence."""

    @pytest.mark.timeout(5)
    async def converges_when_deferred_field_resolver_raises():
        """A nullable deferred field error is delivered with the payload."""
        document = parse('query { obj { ok ... @defer(label: "d") { bad } } }')
        before = snapshot_tasks()
        payloads = await drain(document, {"obj": {"ok": "OK"}})

        assert payloads == [
            {
                "data": {"obj": {"ok": "OK"}},
                "pending": [{"id": "0", "path": ["obj"], "label": "d"}],
                "hasNext": True,
            },
            {
                "incremental": [
                    {
                        "data": {"bad": None},
                        "id": "0",
                        "errors": [
                            {
                                "message": "bad",
                                "locations": [{"line": 1, "column": 43}],
                                "path": ["obj", "bad"],
                            }
                        ],
                    }
                ],
                "completed": [{"id": "0"}],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def converges_when_non_null_deferred_field_raises():
        """A non-null deferred field error completes the group with errors."""
        document = parse('query { obj { ok ... @defer(label: "d") { badNonNull } } }')
        before = snapshot_tasks()
        payloads = await drain(document, {"obj": {"ok": "OK"}})

        assert payloads == [
            {
                "data": {"obj": {"ok": "OK"}},
                "pending": [{"id": "0", "path": ["obj"], "label": "d"}],
                "hasNext": True,
            },
            {
                "completed": [
                    {
                        "id": "0",
                        "errors": [
                            {
                                "message": "bad",
                                "locations": [{"line": 1, "column": 43}],
                                "path": ["obj", "badNonNull"],
                            }
                        ],
                    }
                ],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def converges_when_stream_source_raises_after_initial_count():
        """A failing stream source completes the stream with errors."""

        async def failing_friends(_source: Any, _info: Any) -> Any:
            yield {"id": "1"}
            raise RuntimeError("source boom")

        local_schema = GraphQLSchema(
            GraphQLObjectType(
                "Query",
                {
                    "friends": GraphQLField(
                        GraphQLList(friend_type), resolve=failing_friends
                    )
                },
            )
        )
        document = parse("query { friends @stream(initialCount: 1) { id } }")
        before = snapshot_tasks()
        result = experimental_execute_incrementally(local_schema, document)
        assert is_awaitable(result)
        result = await result
        assert isinstance(result, ExperimentalIncrementalExecutionResults)
        payloads = [result.initial_result.formatted] + [
            patch.formatted async for patch in result.subsequent_results
        ]

        assert payloads == [
            {
                "data": {"friends": [{"id": "1"}]},
                "pending": [{"id": "0", "path": ["friends"]}],
                "hasNext": True,
            },
            {
                "completed": [
                    {
                        "id": "0",
                        "errors": [
                            {
                                "message": "source boom",
                                "locations": [{"line": 1, "column": 9}],
                                "path": ["friends"],
                            }
                        ],
                    }
                ],
                "hasNext": False,
            },
        ]
        assert_converged(payloads)
        await assert_no_residual_tasks(before)


def describe_cancellation_convergence():
    """Client closes and abort signals must converge without leftover work."""

    @pytest.mark.timeout(5)
    async def closes_stream_source_and_leaves_no_tasks_when_consumer_closes():
        """Closing the result stream closes the source and settles all work."""

        class Source:
            def __init__(self) -> None:
                self.count = 0
                self.blocked = Event()
                self.aclose_calls = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> str:
                self.count += 1
                if self.count == 1:
                    return "item1"
                self.blocked.set()
                await Future()  # never resolves
                raise StopAsyncIteration  # pragma: no cover

            async def aclose(self) -> None:
                self.aclose_calls += 1

        source = Source()
        local_schema = GraphQLSchema(
            GraphQLObjectType(
                "Query",
                {
                    "items": GraphQLField(
                        GraphQLList(GraphQLString), resolve=lambda *_: source
                    )
                },
            )
        )
        document = parse("query { items @stream(initialCount: 0) }")

        before = snapshot_tasks()
        result = experimental_execute_incrementally(local_schema, document)
        assert is_awaitable(result)
        result = await result
        assert isinstance(result, ExperimentalIncrementalExecutionResults)
        iterator = result.subsequent_results

        first = await anext(iterator)
        assert first.formatted == {
            "incremental": [{"items": ["item1"], "id": "0"}],
            "hasNext": True,
        }
        await source.blocked.wait()

        # the consumer closes the result stream while the source is blocked
        await iterator.aclose()

        # the blocked source was closed exactly once and nothing is left over
        assert source.aclose_calls == 1
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def cancels_pending_source_iteration_when_aborted():
        """An abort cancels the pending source iteration and all tasks."""
        abort_controller = AbortController()

        class Source:
            def __init__(self) -> None:
                self.started = Event()
                self.cancelled = False
                self.aclose_calls = 0

            def __aiter__(self) -> Any:
                return self

            async def __anext__(self) -> str:
                self.started.set()
                try:
                    await Future()  # never resolves
                except CancelledError:
                    self.cancelled = True
                    raise
                raise StopAsyncIteration  # pragma: no cover

            async def aclose(self) -> None:
                self.aclose_calls += 1

        source = Source()
        local_schema = GraphQLSchema(
            GraphQLObjectType(
                "Query",
                {
                    "items": GraphQLField(
                        GraphQLList(GraphQLString), resolve=lambda *_: source
                    )
                },
            )
        )
        document = parse("query { items @stream(initialCount: 0) }")

        before = snapshot_tasks()
        result = experimental_execute_incrementally(
            local_schema, document, abort_signal=abort_controller.signal
        )
        assert is_awaitable(result)
        result = await result
        assert isinstance(result, ExperimentalIncrementalExecutionResults)
        iterator = result.subsequent_results

        next_task = ensure_future(anext(iterator))
        await source.started.wait()

        abort_controller.abort()

        with pytest.raises(AbortError, match="This operation was aborted"):
            await next_task

        # the pending source iteration was cancelled, the source was closed
        # exactly once and no asynchronous work is left behind
        assert source.cancelled
        assert source.aclose_calls == 1
        await assert_no_residual_tasks(before)

    @pytest.mark.timeout(5)
    async def cancels_pending_deferred_resolver_when_aborted():
        """An abort cancels pending deferred resolvers and all tasks."""
        abort_controller = AbortController()
        started = Event()
        cancelled = False

        async def hanging(_source: Any, _info: Any) -> str:
            nonlocal cancelled
            started.set()
            try:
                await Future()  # never resolves
            except CancelledError:
                cancelled = True
                raise
            raise AssertionError("unreachable")  # pragma: no cover

        local_obj_type = GraphQLObjectType(
            "Obj",
            {
                "ok": GraphQLField(GraphQLString),
                "slow": GraphQLField(GraphQLString, resolve=hanging),
            },
        )
        local_schema = GraphQLSchema(
            GraphQLObjectType("Query", {"obj": GraphQLField(local_obj_type)})
        )
        document = parse('query { obj { ok ... @defer(label: "d") { slow } } }')

        before = snapshot_tasks()
        result = experimental_execute_incrementally(
            local_schema,
            document,
            {"obj": {"ok": "OK"}},
            abort_signal=abort_controller.signal,
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)
        iterator = result.subsequent_results

        next_task = ensure_future(anext(iterator))
        await started.wait()

        abort_controller.abort()

        with pytest.raises(AbortError, match="This operation was aborted"):
            await next_task

        # the pending deferred resolver is cancelled as the abort settles
        # and no asynchronous work is left behind
        await assert_no_residual_tasks(before)
        assert cancelled
