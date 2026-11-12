"""Regression tests for null bubbling visibility across incremental payloads.

When a non-nullable field fails inside an incremental (``@defer`` or
``@stream``) execution, the error propagates to the nearest nullable boundary
and the whole parent subtree becomes invalid. Data that was already delivered
cannot be withdrawn, so the client must *discard* that subtree when the failure
is announced and must never let a later incremental patch resurrect it.

The tests drive the completion order with in-memory pausable resolvers
(:class:`asyncio.Future` gates) and assert every payload of the sequence
(``pending``, ``incremental``, ``completed``, error paths and ``hasNext``),
not only the final state.

``ResultMerger`` is a deliberately tiny standalone client-side state ledger.
It keeps the merged data together with the set of invalidated ("tombstoned")
paths and shows exactly when a subtree has to be dropped and how subsequent
patches targeting a dead subtree are filtered out.
"""

from __future__ import annotations

from asyncio import Future
from copy import deepcopy
from typing import Any

import pytest

from graphql.execution import (
    ExperimentalIncrementalExecutionResults,
    experimental_execute_incrementally,
)
from graphql.language import parse
from graphql.type import (
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
)

pytestmark = pytest.mark.anyio


def _is_under(path: list[str | int], prefix: list[str | int]) -> bool:
    """Whether ``path`` is equal to or nested below ``prefix``."""
    return path[: len(prefix)] == prefix


class ResultMerger:
    """Tiny in-memory ledger merging the incremental delivery protocol.

    It mirrors what a spec-compliant client has to do while applying
    incremental payloads:

    * the initial payload seeds ``data`` and the announced ``pending`` entries
      remember where each deferred fragment or stream lives;
    * a ``completed`` entry carrying errors nulls the owning pending location,
      recording it as a tombstone -- this is the moment a previously delivered
      subtree must be discarded, because sent bytes cannot be unsent;
    * every later ``incremental`` patch whose target is tombstoned is dropped,
      so a late patch can never resurrect data under a dead parent;
    * an unaffected branch is merged normally and still completes.
    """

    def __init__(self, filter_invalid_patches: bool = True) -> None:
        # When False, stale patches are applied anyway. This switch exists only
        # to prove that the filtering is what prevents a resurrection.
        self.filter_invalid_patches = filter_invalid_patches
        self.data: dict[str, Any] = {}
        self.pending_paths: dict[str, list[str | int]] = {}
        self.tombstones: list[list[str | int]] = []
        self.dropped_patches: list[dict[str, Any]] = []

    def _is_tombstoned(self, path: list[str | int]) -> bool:
        return any(
            _is_under(path, tombstone) or _is_under(tombstone, path)
            for tombstone in self.tombstones
        )

    def _null_at(self, path: list[str | int]) -> None:
        node: Any = self.data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = None

    def _merge(self, path: list[str | int], value: Any) -> None:
        if not path:
            if isinstance(value, dict):
                self.data.update(value)
            else:
                self.data = value
            return
        node: Any = self.data
        for key in path[:-1]:
            node = node.setdefault(key, {})
        if isinstance(value, dict):
            target = node.setdefault(path[-1], {})
            target.update(value)
        else:
            node[path[-1]] = value

    def _append_items(self, path: list[str | int], items: list[Any]) -> None:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or node.get(key) is None:
                # A naive client rebuilds every node it was told to discard;
                # this is exactly the resurrection the filter prevents.
                node[key] = {}
            node = node[key]
        if not isinstance(node, list):
            parent: Any = self.data
            for key in path[:-1]:
                parent = parent[key]
            node = []
            parent[path[-1]] = node
        node.extend(items)

    def apply_initial(self, payload: dict[str, Any]) -> None:
        self.data = deepcopy(payload["data"])
        for pending in payload.get("pending", []):
            self.pending_paths[pending["id"]] = list(pending["path"])

    def apply_subsequent(self, payload: dict[str, Any]) -> None:
        for pending in payload.get("pending", []):
            self.pending_paths[pending["id"]] = list(pending["path"])

        # Failures are processed first: a completed-with-errors entry declares
        # the owning pending subtree dead before any patch in the same payload
        # could be applied. Successful completions keep their path registered,
        # because incremental patches in the same payload still use that id.
        for completed in payload.get("completed", []):
            if completed.get("errors"):
                path = self.pending_paths.pop(completed["id"], [])
                if path and not self._is_tombstoned(path):
                    self.tombstones.append(path)
                    self._null_at(path)

        for patch in payload.get("incremental", []):
            base = self.pending_paths.get(patch["id"], [])
            target = [*base, *patch.get("subPath", [])]
            if self.filter_invalid_patches and self._is_tombstoned(target):
                self.dropped_patches.append(patch)
                continue
            if "data" in patch:
                self._merge(target, patch["data"])
            else:  # streamed list items
                self._append_items(target, patch["items"])


friend_type = GraphQLObjectType(
    "Friend",
    {
        "name": GraphQLField(GraphQLString),
        "nonNullName": GraphQLField(GraphQLNonNull(GraphQLString)),
    },
)

hero_type = GraphQLObjectType(
    "Hero",
    {
        "name": GraphQLField(GraphQLString),
        "nonNullName": GraphQLField(GraphQLNonNull(GraphQLString)),
        "friends": GraphQLField(GraphQLList(GraphQLNonNull(friend_type))),
    },
)

other_type = GraphQLObjectType(
    "Other", {"value": GraphQLField(GraphQLString)}
)

query_type = GraphQLObjectType(
    "Query",
    {"hero": GraphQLField(hero_type), "other": GraphQLField(other_type)},
)

schema = GraphQLSchema(query_type)


def _merge_payloads(
    payloads: list[dict[str, Any]], filter_invalid_patches: bool = True
) -> ResultMerger:
    merger = ResultMerger(filter_invalid_patches=filter_invalid_patches)
    merger.apply_initial(payloads[0])
    for payload in payloads[1:]:
        merger.apply_subsequent(payload)
    return merger


def describe_null_bubbling_keeps_failed_subtrees_hidden():
    """Null bubbling must not be undone by later incremental work."""

    async def failure_settled_before_initial_result_is_consumed():
        """Non-null failure settled before the client reads the initial result

        The failing deferred fragment announces ``hero`` as null; the in-flight
        streamed ``friends`` work is removed server-side and never emits, while
        the independent ``other`` branch still completes.
        """
        document = parse(
            """
            query {
              ... @defer(label: "hero") {
                hero {
                  nonNullName
                  friends @stream(initialCount: 0) { name }
                }
              }
              ... @defer(label: "other") {
                other { value }
              }
            }
            """
        )
        bad_name: Future[None] = Future()
        root_value = {
            "hero": {
                "nonNullName": bad_name,
                "friends": [{"name": "Han"}, {"name": "Leia"}],
            },
            "other": {"value": "ok"},
        }

        result = experimental_execute_incrementally(
            schema, document, root_value
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)

        initial = result.initial_result.formatted
        assert initial == {
            "data": {},
            "pending": [
                {"id": "0", "path": [], "label": "hero"},
                {"id": "1", "path": [], "label": "other"},
            ],
            "hasNext": True,
        }

        # Fail the non-nullable field before consuming any subsequent payload.
        bad_name.set_result(None)

        payloads = [initial]
        async for patch in result.subsequent_results:
            payloads.append(patch.formatted)

        # Shortest sequence: the unaffected branch, then the failed one.
        assert payloads == [
            initial,
            {
                "hasNext": True,
                "incremental": [
                    {"data": {"other": {"value": "ok"}}, "id": "1"}
                ],
                "completed": [{"id": "1"}],
            },
            {
                "hasNext": False,
                "incremental": [
                    {
                        "data": {"hero": None},
                        "id": "0",
                        "errors": [
                            {
                                "message": "Cannot return null for non-nullable"
                                " field Hero.nonNullName.",
                                "locations": [{"line": 5, "column": 19}],
                                "path": ["hero", "nonNullName"],
                            }
                        ],
                    }
                ],
                "completed": [{"id": "0"}],
            },
        ]
        # The stream (id "2") never appears: no pending announcement, items or
        # completion payload survive the failed parent.
        assert all(
            "id" not in entry or entry["id"] != "2"
            for payload in payloads
            for entry in payload.get("incremental", [])
        )

        merger = _merge_payloads(payloads)
        assert merger.data == {"hero": None, "other": {"value": "ok"}}
        assert merger.tombstones == []
        assert merger.dropped_patches == []

    async def failure_after_initial_result_was_already_delivered():
        """Non-null failure arrives after the initial payload was sent

        ``hero`` and the first streamed friend are already visible. The later
        deferred failure cannot retract those bytes, so the client discards the
        whole ``hero`` subtree at the announced path and the late stream item
        must not bring it back.
        """
        document = parse(
            """
            query {
              hero {
                name
                ... @defer(label: "slow") {
                  nonNullName
                }
                friends @stream(initialCount: 1) { name }
              }
              ... @defer(label: "other") {
                other { value }
              }
            }
            """
        )
        slow_name: Future[None] = Future()
        second_friend: Future[dict[str, str]] = Future()
        root_value = {
            "hero": {
                "name": "Luke",
                "nonNullName": slow_name,
                "friends": [{"name": "Han"}, second_friend],
            },
            "other": {"value": "ok"},
        }

        result = experimental_execute_incrementally(
            schema, document, root_value
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)

        initial = result.initial_result.formatted
        assert initial == {
            "data": {
                "hero": {"name": "Luke", "friends": [{"name": "Han"}]}
            },
            "pending": [
                {"id": "0", "path": ["hero"], "label": "slow"},
                {"id": "1", "path": [], "label": "other"},
                {"id": "2", "path": ["hero", "friends"]},
            ],
            "hasNext": True,
        }

        iterator = result.subsequent_results

        first_patch = (await anext(iterator)).formatted
        assert first_patch == {
            "hasNext": True,
            "incremental": [
                {"data": {"other": {"value": "ok"}}, "id": "1"}
            ],
            "completed": [{"id": "1"}],
        }

        # Now the deferred non-nullable field fails after data was delivered.
        slow_name.set_result(None)
        failure_patch = (await anext(iterator)).formatted
        assert failure_patch == {
            "hasNext": True,
            "completed": [
                {
                    "id": "0",
                    "errors": [
                        {
                            "message": "Cannot return null for non-nullable"
                            " field Hero.nonNullName.",
                            "locations": [{"line": 6, "column": 19}],
                            "path": ["hero", "nonNullName"],
                        }
                    ],
                }
            ],
        }

        # The pending stream item settles afterwards; its delivery is filtered.
        second_friend.set_result({"name": "Leia"})
        late_patch = (await anext(iterator)).formatted
        assert late_patch == {
            "hasNext": False,
            "incremental": [
                {
                    "items": [{"name": "Leia"}],
                    "id": "2",
                }
            ],
            "completed": [{"id": "2"}],
        }
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

        payloads = [initial, first_patch, failure_patch, late_patch]
        merger = _merge_payloads(payloads)

        # The client discards the already-sent hero subtree and the late item
        # is recorded as dropped instead of resurrecting any field under it.
        assert merger.tombstones == [["hero"]]
        assert merger.data == {
            "hero": None,
            "other": {"value": "ok"},
        }
        assert [patch["id"] for patch in merger.dropped_patches] == ["2"]

    async def non_null_stream_item_failure_propagates_through_the_list():
        """A null in a non-null streamed item nulls the whole list

        The first list item was sent in the initial payload. When the later
        item resolves to null, the error path points at that index and bubbles
        past the non-null list, so the client drops the entire list; an
        independent stream still finishes.
        """
        list_type = GraphQLObjectType(
            "ListRoot",
            {
                "nonNullFriendList": GraphQLField(
                    GraphQLList(GraphQLNonNull(friend_type))
                ),
                "otherList": GraphQLField(GraphQLList(GraphQLString)),
            },
        )
        stream_schema = GraphQLSchema(
            GraphQLObjectType("Query", {"list": GraphQLField(list_type)})
        )
        document = parse(
            """
            query {
              list {
                nonNullFriendList @stream(initialCount: 1) { name }
                otherList @stream(initialCount: 0)
              }
            }
            """
        )
        second_item: Future[None] = Future()
        root_value = {
            "list": {
                "nonNullFriendList": lambda _info: [
                    {"name": "Han"},
                    second_item,
                ],
                "otherList": lambda _info: ["a", "b"],
            }
        }

        result = experimental_execute_incrementally(
            stream_schema, document, root_value
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)

        initial = result.initial_result.formatted
        assert initial == {
            "data": {
                "list": {
                    "nonNullFriendList": [{"name": "Han"}],
                    "otherList": [],
                }
            },
            "pending": [
                {"id": "0", "path": ["list", "nonNullFriendList"]},
                {"id": "1", "path": ["list", "otherList"]},
            ],
            "hasNext": True,
        }

        iterator = result.subsequent_results
        unaffected_patch = (await anext(iterator)).formatted
        assert unaffected_patch == {
            "hasNext": True,
            "incremental": [{"items": ["a", "b"], "id": "1"}],
            "completed": [{"id": "1"}],
        }

        second_item.set_result(None)
        failure_patch = (await anext(iterator)).formatted
        assert failure_patch == {
            "hasNext": False,
            "completed": [
                {
                    "id": "0",
                    "errors": [
                        {
                            "message": "Cannot return null for non-nullable"
                            " field ListRoot.nonNullFriendList.",
                            "locations": [{"line": 4, "column": 17}],
                            "path": [
                                "list",
                                "nonNullFriendList",
                                1,
                            ],
                        }
                    ],
                }
            ],
        }
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

        merger = _merge_payloads(
            [initial, unaffected_patch, failure_patch]
        )
        assert merger.tombstones == [["list", "nonNullFriendList"]]
        assert merger.data == {
            "list": {"nonNullFriendList": None, "otherList": ["a", "b"]}
        }
        assert merger.dropped_patches == []

    async def multiple_independent_deferred_branches_isolate_the_failure():
        """One failing deferred branch does not affect a sibling branch

        The failed branch is nulled while a separate deferred branch completes
        normally and remains visible in the merged data.
        """
        document = parse(
            """
            query {
              ... @defer(label: "left") {
                hero { name nonNullName }
              }
              ... @defer(label: "right") {
                other { value }
              }
            }
            """
        )
        bad_name: Future[None] = Future()
        right_value: Future[str] = Future()
        root_value = {
            "hero": {"name": "Luke", "nonNullName": bad_name},
            "other": {"value": right_value},
        }

        result = experimental_execute_incrementally(
            schema, document, root_value
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)

        initial = result.initial_result.formatted
        assert initial == {
            "data": {},
            "pending": [
                {"id": "0", "path": [], "label": "left"},
                {"id": "1", "path": [], "label": "right"},
            ],
            "hasNext": True,
        }

        iterator = result.subsequent_results
        bad_name.set_result(None)
        failure_patch = (await anext(iterator)).formatted
        assert failure_patch == {
            "hasNext": True,
            "incremental": [
                {
                    "data": {"hero": None},
                    "id": "0",
                    "errors": [
                        {
                            "message": "Cannot return null for non-nullable"
                            " field Hero.nonNullName.",
                            "locations": [{"line": 4, "column": 29}],
                            "path": ["hero", "nonNullName"],
                        }
                    ],
                }
            ],
            "completed": [{"id": "0"}],
        }

        right_value.set_result("R")
        sibling_patch = (await anext(iterator)).formatted
        assert sibling_patch == {
            "hasNext": False,
            "incremental": [
                {"data": {"other": {"value": "R"}}, "id": "1"}
            ],
            "completed": [{"id": "1"}],
        }
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

        merger = _merge_payloads(
            [initial, failure_patch, sibling_patch]
        )
        assert merger.data == {"hero": None, "other": {"value": "R"}}
        assert merger.tombstones == []
        assert merger.dropped_patches == []

    async def disabling_filter_resurrects_the_failed_subtree():
        """Sanity check: without filtering, a stale patch revives dead data

        This temporarily turns the tombstone filtering off and feeds a late
        patch into the dead ``hero`` subtree, proving the filter is the
        mechanism that keeps failed data hidden. With filtering enabled the
        same patch is dropped instead.
        """
        document = parse(
            """
            query {
              hero {
                name
                ... @defer(label: "slow") {
                  nonNullName
                }
                friends @stream(initialCount: 1) { name }
              }
            }
            """
        )
        slow_name: Future[None] = Future()
        second_friend: Future[dict[str, str]] = Future()
        root_value = {
            "hero": {
                "name": "Luke",
                "nonNullName": slow_name,
                "friends": [{"name": "Han"}, second_friend],
            }
        }

        result = experimental_execute_incrementally(
            schema, document, root_value
        )
        assert isinstance(result, ExperimentalIncrementalExecutionResults)
        initial = result.initial_result.formatted
        iterator = result.subsequent_results

        slow_name.set_result(None)
        failure_patch = (await anext(iterator)).formatted
        assert failure_patch["completed"][0]["errors"]

        second_friend.set_result({"name": "Leia"})
        late_patch = (await anext(iterator)).formatted
        assert late_patch["hasNext"] is False

        # Filtering on (default): the late stream item cannot resurrect hero.
        safe_merger = _merge_payloads(
            [initial, failure_patch, late_patch]
        )
        assert safe_merger.data == {"hero": None}
        assert len(safe_merger.dropped_patches) == 1

        # Filtering disabled: the stale patch writes back under the dead node.
        unsafe_merger = _merge_payloads(
            [initial, failure_patch, late_patch],
            filter_invalid_patches=False,
        )
        assert unsafe_merger.dropped_patches == []
        # The nulled parent was rebuilt and populated with the late item:
        # data that the failure was supposed to keep hidden is visible again.
        assert unsafe_merger.data["hero"] == {
            "friends": [{"name": "Leia"}],
        }
