"""Tests that incremental delivery converges and leaves no residual tasks."""

from __future__ import annotations

from asyncio import Event, Future, all_tasks, current_task, sleep
from typing import TYPE_CHECKING

import pytest

from graphql.execution import (
    ExperimentalIncrementalExecutionResults,
    experimental_execute_incrementally,
)
from graphql.language import parse
from graphql.pyutils import AbortController, AbortError, is_awaitable
from graphql.type import (
    GraphQLField,
    GraphQLID,
    GraphQLList,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.anyio

friend_type = GraphQLObjectType(
    "Friend",
    {
        "id": GraphQLField(GraphQLID),
        "name": GraphQLField(GraphQLString),
    },
)

todo_type = GraphQLObjectType(
    "Todo",
    lambda: {
        "id": GraphQLField(GraphQLID),
        "items": GraphQLField(GraphQLList(GraphQLString)),
        "bad": GraphQLField(GraphQLString),
        "friends": GraphQLField(GraphQLList(friend_type)),
    },
)

schema = GraphQLSchema(
    GraphQLObjectType(
        "Query",
        {
            "todo": GraphQLField(todo_type),
        },
    )
)


def residual_tasks(before: set) -> list:
    """Tasks created after the snapshot that have not finished yet."""
    return [
        task
        for task in all_tasks()
        if task not in before and task is not current_task() and not task.done()
    ]


async def settle() -> None:
    """Let cancelled or finishing tasks run to completion."""
    for _ in range(10):
        await sleep(0)


async def execute(document, root_value=None, **kwargs):
    result = experimental_execute_incrementally(schema, document, root_value, **kwargs)
    if is_awaitable(result):
        result = await result
    assert isinstance(result, ExperimentalIncrementalExecutionResults)
    return result


async def drain(iterator: AsyncIterator) -> list:
    return [patch.formatted async for patch in iterator]


def describe_no_residual_tasks():
    async def leaves_no_tasks_after_clean_completion_with_nested_stream():
        """A defer wrapping a nested stream converges and cleans up."""
        before = set(all_tasks())
        document = parse(
            """
            query {
              todo {
                id
                ... @defer(label: "todoDefer") {
                  friends @stream(label: "friendStream", initialCount: 0) {
                    id
                  }
                }
              }
            }
            """
        )

        async def todo(_info):
            await sleep(0)
            return {"id": "1", "friends": [{"id": "1"}, {"id": "2"}]}

        result = await execute(document, {"todo": todo})
        assert result.initial_result.formatted == {
            "data": {"todo": {"id": "1"}},
            "pending": [{"id": "0", "path": ["todo"], "label": "todoDefer"}],
            "hasNext": True,
        }

        subsequent = await drain(result.subsequent_results)

        # the stream terminates with a final hasNext: False payload
        assert subsequent[-1]["hasNext"] is False
        assert all(patch["hasNext"] is True for patch in subsequent[:-1])

        # every announced pending group completed exactly once, without errors
        payloads = [result.initial_result.formatted, *subsequent]
        pending = [p for payload in payloads for p in payload.get("pending", [])]
        completed = [c for payload in payloads for c in payload.get("completed", [])]
        assert sorted(p["id"] for p in pending) == ["0", "1"]
        assert sorted(c["id"] for c in completed) == ["0", "1"]
        assert all("errors" not in c for c in completed)

        # the nested stream delivered both friends across the defer boundary
        items = [
            item
            for payload in payloads
            for entry in payload.get("incremental", [])
            for item in entry.get("items", [])
        ]
        assert items == [{"id": "1"}, {"id": "2"}]

        await settle()
        assert residual_tasks(before) == []

    async def leaves_no_tasks_after_partial_errors_in_deferred_fragment():
        """Partial errors in a deferred fragment still converge and clean up."""
        before = set(all_tasks())
        document = parse(
            """
            query {
              todo {
                id
                ... @defer {
                  items
                  bad
                }
              }
            }
            """
        )

        def bad(_info):
            raise RuntimeError("bad")

        async def todo(_info):
            await sleep(0)
            return {"id": "1", "items": ["a"], "bad": bad}

        result = await execute(document, {"todo": todo})
        subsequent = await drain(result.subsequent_results)

        assert subsequent[-1]["hasNext"] is False
        incremental = [
            entry for patch in subsequent for entry in patch.get("incremental", [])
        ]
        assert incremental == [
            {
                "data": {"items": ["a"], "bad": None},
                "id": "0",
                "errors": [
                    {
                        "message": "bad",
                        "locations": [{"line": 7, "column": 19}],
                        "path": ["todo", "bad"],
                    }
                ],
            }
        ]
        completed = [
            entry for patch in subsequent for entry in patch.get("completed", [])
        ]
        assert completed == [{"id": "0"}]

        await settle()
        assert residual_tasks(before) == []

    async def leaves_no_tasks_for_shared_fragment_deferred_and_not_deferred():
        """A fragment spread both deferred and non-deferred converges."""
        before = set(all_tasks())
        document = parse(
            """
            query {
              todo {
                id
                ...SharedBits
                ... @defer {
                  ...SharedBits
                  friends {
                    id
                  }
                }
              }
            }
            fragment SharedBits on Todo {
              items
            }
            """
        )

        async def todo(_info):
            await sleep(0)
            return {"id": "1", "items": ["a", "b"], "friends": [{"id": "1"}]}

        result = await execute(document, {"todo": todo})
        # the shared fields are delivered in the initial payload
        assert result.initial_result.formatted["data"] == {
            "todo": {"id": "1", "items": ["a", "b"]}
        }

        subsequent = await drain(result.subsequent_results)
        assert subsequent[-1]["hasNext"] is False
        completed = [
            entry for patch in subsequent for entry in patch.get("completed", [])
        ]
        assert completed == [{"id": "0"}]

        await settle()
        assert residual_tasks(before) == []

    async def leaves_no_tasks_when_aborted_with_a_hanging_resolver():
        """Aborting with pending deferred work cancels all spawned tasks."""
        before = set(all_tasks())
        abort_controller = AbortController()
        never: Future[list[str]] = Future()
        document = parse(
            """
            query {
              todo {
                id
                ... @defer {
                  items
                }
              }
            }
            """
        )

        result = await execute(
            document,
            {"todo": {"id": "1", "items": lambda _info: never}},
            abort_signal=abort_controller.signal,
        )
        assert result.initial_result.formatted["hasNext"] is True

        abort_controller.abort()
        with pytest.raises(AbortError):
            await anext(result.subsequent_results)

        await settle()
        assert residual_tasks(before) == []

    async def leaves_no_tasks_when_consumer_closes_the_stream_early():
        """Closing the subsequent results stream early cancels pending work."""
        before = set(all_tasks())
        release = Event()
        document = parse(
            """
            query {
              todo {
                id
                ... @defer {
                  items
                }
              }
            }
            """
        )

        async def items(_info):
            await release.wait()
            return ["a"]  # pragma: no cover

        result = await execute(document, {"todo": {"id": "1", "items": items}})
        iterator = result.subsequent_results

        # the consumer closes the stream while the deferred resolver hangs
        await iterator.aclose()
        release.set()

        await settle()
        assert residual_tasks(before) == []
