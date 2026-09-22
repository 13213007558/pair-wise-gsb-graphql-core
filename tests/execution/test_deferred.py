"""Tests for synchronous deferred batch loading with SyncDataLoader."""

import gc
import warnings
from inspect import isawaitable

from pytest import mark, raises

from graphql import (
    DeferredValue,
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    SyncDataLoader,
    execute,
    execute_sync,
    graphql_sync,
)
from graphql.language import parse


class RecordingLoader(SyncDataLoader):
    """A sync loader that records the keys of every dispatched batch."""

    def __init__(self, load_fn):
        self.batches = []

        def batch_load_fn(keys):
            keys = list(keys)
            self.batches.append(keys)
            return [load_fn(key) for key in keys]

        super().__init__(batch_load_fn)


def owner_schema(owner_ids, owner_loader, name_loader, owner_type=None):
    """Build a schema with 100-ish items that each have a deferred owner."""
    user = GraphQLObjectType(
        "User",
        lambda: {
            "name": GraphQLField(
                GraphQLString,
                resolve=lambda user_, _info: name_loader.load(user_["id"]),
            )
        },
    )
    item = GraphQLObjectType(
        "Item",
        lambda: {
            "owner": GraphQLField(
                owner_type or user,
                resolve=lambda item_, _info: owner_loader.load(item_["owner_id"]),
            )
        },
    )
    query = GraphQLObjectType(
        "Query",
        lambda: {
            "items": GraphQLField(
                GraphQLList(item),
                resolve=lambda _root, _info: [
                    {"owner_id": owner_id} for owner_id in owner_ids
                ],
            )
        },
    )
    return GraphQLSchema(query)


def describe_sync_deferred_batch_loading():
    def batches_100_same_layer_fields_into_one_call():
        owner_loader = RecordingLoader(lambda key: {"id": key})
        name_loader = RecordingLoader(lambda key: "user-%s" % key)
        schema = owner_schema(range(100), owner_loader, name_loader)

        result = graphql_sync(schema, "{ items { owner { name } } }")

        assert not result.errors
        items = result.data["items"]
        assert len(items) == 100
        assert items[0] == {"owner": {"name": "user-0"}}
        assert items[99] == {"owner": {"name": "user-99"}}
        # all 100 same-layer owner fields form a single batch
        assert owner_loader.batches == [list(range(100))]
        # the next-layer name fields form the next batch
        assert name_loader.batches == [list(range(100))]

    def deduplicates_keys_and_maps_results_back_to_positions():
        owner_loader = RecordingLoader(lambda key: {"id": key})
        name_loader = RecordingLoader(lambda key: "user-%s" % key)
        schema = owner_schema([2, 1, 2, 3, 1], owner_loader, name_loader)

        result = graphql_sync(schema, "{ items { owner { name } } }")

        assert not result.errors
        # each key is only loaded once, in order of first appearance
        assert owner_loader.batches == [[2, 1, 3]]
        # results are mapped back to the original list positions
        names = [item["owner"]["name"] for item in result.data["items"]]
        assert names == ["user-2", "user-1", "user-2", "user-3", "user-1"]

    def shares_loaded_values_between_aliased_fields():
        owner_loader = RecordingLoader(lambda key: {"id": key})
        user = GraphQLObjectType(
            "User",
            lambda: {
                "id": GraphQLField(GraphQLString, resolve=lambda u, _info: u["id"])
            },
        )
        query = GraphQLObjectType(
            "Query",
            lambda: {
                "viewer": GraphQLField(
                    user, resolve=lambda _root, _info: owner_loader.load("me")
                )
            },
        )
        schema = GraphQLSchema(query)

        result = graphql_sync(schema, "{ first: viewer { id } second: viewer { id } }")

        assert not result.errors
        assert result.data == {"first": {"id": "me"}, "second": {"id": "me"}}
        assert owner_loader.batches == [["me"]]

    def supports_load_many_for_list_fields():
        loader = RecordingLoader(lambda key: "value-%s" % key)
        query = GraphQLObjectType(
            "Query",
            lambda: {
                "things": GraphQLField(
                    GraphQLList(GraphQLString),
                    resolve=lambda _root, _info: loader.load_many(["a", "b", "a"]),
                )
            },
        )
        schema = GraphQLSchema(query)

        result = graphql_sync(schema, "{ things }")

        assert not result.errors
        assert result.data == {"things": ["value-a", "value-b", "value-a"]}
        assert loader.batches == [["a", "b"]]

    def does_not_share_cache_between_executions():
        owner_loader = RecordingLoader(lambda key: {"id": key})
        name_loader = RecordingLoader(lambda key: "user-%s" % key)
        schema = owner_schema([1], owner_loader, name_loader)

        for _ in range(2):
            result = graphql_sync(schema, "{ items { owner { name } } }")
            assert not result.errors

        assert owner_loader.batches == [[1], [1]]
        assert name_loader.batches == [[1], [1]]

    def sub_fields_produce_further_batch_rounds():
        batches = []

        def recording(load_fn):
            def batch_load_fn(keys):
                keys = list(keys)
                batches.append(keys)
                return [load_fn(key) for key in keys]

            return SyncDataLoader(batch_load_fn)

        id_loader = recording(lambda key: "id-%s" % key)
        name_loader = recording(lambda key: "name-%s" % key)
        inner = GraphQLObjectType(
            "Inner",
            lambda: {
                "name": GraphQLField(
                    GraphQLString, resolve=lambda source, _info: name_loader.load(source)
                )
            },
        )
        mid = GraphQLObjectType(
            "Mid",
            lambda: {
                "inner": GraphQLField(
                    inner, resolve=lambda source, _info: id_loader.load(source)
                )
            },
        )
        query = GraphQLObjectType(
            "Query",
            lambda: {
                "mid": GraphQLField(
                    mid, resolve=lambda _root, _info: id_loader.load("root")
                )
            },
        )
        schema = GraphQLSchema(query)

        result = graphql_sync(schema, "{ mid { inner { name } } }")

        assert not result.errors
        assert result.data == {"mid": {"inner": {"name": "name-id-id-root"}}}
        # each level of deferred fields forms its own batch round
        assert batches == [["root"], ["id-root"], ["id-id-root"]]

    def describe_error_handling():
        def single_failure_bubbles_only_for_that_field():
            def load_owner(key):
                if key == 2:
                    return ValueError("cannot load owner 2")
                return {"id": key}

            owner_loader = RecordingLoader(load_owner)
            name_loader = RecordingLoader(lambda key: "user-%s" % key)
            schema = owner_schema([1, 2, 3], owner_loader, name_loader)

            result = graphql_sync(schema, "{ items { owner { name } } }")

            # the other fields of the batch are not affected
            assert result.data == {
                "items": [
                    {"owner": {"name": "user-1"}},
                    {"owner": None},
                    {"owner": {"name": "user-3"}},
                ]
            }
            assert len(result.errors) == 1
            error = result.errors[0]
            assert error.message == "cannot load owner 2"
            assert error.path == ["items", 1, "owner"]

        def failure_of_non_null_field_nulls_the_parent():
            def load_owner(key):
                if key == 2:
                    return ValueError("cannot load owner 2")
                return {"id": key}

            owner_loader = RecordingLoader(load_owner)
            name_loader = RecordingLoader(lambda key: "user-%s" % key)
            user = GraphQLObjectType(
                "User",
                lambda: {
                    "name": GraphQLField(
                        GraphQLString,
                        resolve=lambda u, _info: name_loader.load(u["id"]),
                    )
                },
            )
            schema = owner_schema(
                [1, 2], owner_loader, name_loader, GraphQLNonNull(user)
            )

            result = graphql_sync(schema, "{ items { owner { name } } }")

            assert result.data == {"items": [{"owner": {"name": "user-1"}}, None]}
            assert len(result.errors) == 1
            assert result.errors[0].message == "cannot load owner 2"
            assert result.errors[0].path == ["items", 1, "owner"]

        def failure_of_non_null_root_field_nulls_the_whole_response():
            owner_loader = RecordingLoader(lambda key: ValueError("boom"))
            user = GraphQLObjectType(
                "User",
                lambda: {
                    "id": GraphQLField(GraphQLString, resolve=lambda u, _i: u["id"])
                },
            )
            query = GraphQLObjectType(
                "Query",
                lambda: {
                    "viewer": GraphQLField(
                        GraphQLNonNull(user),
                        resolve=lambda _root, _info: owner_loader.load("me"),
                    )
                },
            )
            schema = GraphQLSchema(query)

            result = graphql_sync(schema, "{ viewer { id } }")

            assert result.data is None
            assert len(result.errors) == 1
            assert result.errors[0].message == "boom"
            assert result.errors[0].path == ["viewer"]

        def failing_batch_function_fails_each_key_of_the_batch():
            def batch_load_fn(_keys):
                raise ValueError("database is down")

            loader = SyncDataLoader(batch_load_fn)
            query = GraphQLObjectType(
                "Query",
                lambda: {
                    "one": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(1)
                    ),
                    "two": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(2)
                    ),
                },
            )
            schema = GraphQLSchema(query)

            result = graphql_sync(schema, "{ one two }")

            assert result.data == {"one": None, "two": None}
            assert [error.message for error in result.errors] == [
                "database is down",
                "database is down",
            ]

        def batch_function_with_wrong_result_count_fails_clearly():
            loader = SyncDataLoader(lambda _keys: ["only-one-result"])
            query = GraphQLObjectType(
                "Query",
                lambda: {
                    "one": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(1)
                    ),
                    "two": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(2)
                    ),
                },
            )
            schema = GraphQLSchema(query)

            result = graphql_sync(schema, "{ one two }")

            assert result.data is None
            assert len(result.errors) == 1
            assert result.errors[0].message == (
                "Batch loading function did not return the expected number"
                " of values, expected 2, but got 1."
            )

    def describe_mutation_root_fields_stay_serial():
        def derived_loads_complete_before_the_next_root_field():
            events = []

            def batch_load_fn(keys):
                keys = list(keys)
                events.append(("batch", keys))
                return keys

            loader = SyncDataLoader(batch_load_fn)
            result_type = GraphQLObjectType(
                "Result",
                lambda: {
                    "value": GraphQLField(
                        GraphQLString,
                        resolve=lambda source, _info: loader.load(source),
                    )
                },
            )

            def resolve_first(_root, _info):
                events.append(("resolve", "first"))
                return "first"

            def resolve_second(_root, _info):
                events.append(("resolve", "second"))
                return "second"

            mutation = GraphQLObjectType(
                "Mutation",
                lambda: {
                    "first": GraphQLField(result_type, resolve=resolve_first),
                    "second": GraphQLField(result_type, resolve=resolve_second),
                },
            )
            query = GraphQLObjectType(
                "Query", lambda: {"dummy": GraphQLField(GraphQLString)}
            )
            schema = GraphQLSchema(query, mutation=mutation)

            result = graphql_sync(
                schema, "mutation { first { value } second { value } }"
            )

            assert not result.errors
            assert result.data == {
                "first": {"value": "first"},
                "second": {"value": "second"},
            }
            # the derived load of the first root field is dispatched before
            # the resolver of the second root field is even called
            assert events == [
                ("resolve", "first"),
                ("batch", ["first"]),
                ("resolve", "second"),
                ("batch", ["second"]),
            ]

    def describe_sync_execution_entry_points():
        def works_with_execute_sync_and_check_sync():
            loader = RecordingLoader(lambda key: "value-%s" % key)
            query = GraphQLObjectType(
                "Query",
                lambda: {
                    "field": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(1)
                    )
                },
            )
            schema = GraphQLSchema(query)

            result = execute_sync(schema, parse("{ field }"), check_sync=True)

            assert not result.errors
            assert result.data == {"field": "value-1"}
            assert loader.batches == [[1]]

        def fails_clearly_on_awaitable_without_starting_an_event_loop():
            async def resolve_async(_root, _info):
                return "async"  # pragma: no cover

            query = GraphQLObjectType(
                "Query",
                lambda: {"field": GraphQLField(GraphQLString, resolve=resolve_async)},
            )
            schema = GraphQLSchema(query)

            with warnings.catch_warnings(record=True) as records:
                warnings.simplefilter("always")
                with raises(RuntimeError) as exc_info:
                    execute_sync(schema, parse("{ field }"), check_sync=True)

            assert str(exc_info.value) == (
                "GraphQL execution failed to complete synchronously."
            )
            # the coroutine was closed instead of being scheduled on a loop
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                gc.collect()
            assert not [
                record
                for record in records
                if "was never awaited" in str(record.message)
            ]

        @mark.filterwarnings("ignore:.* was never awaited:RuntimeWarning")
        def rejects_deferred_values_during_async_execution():
            async def resolve_async(_root, _info):
                return "async"

            loader = RecordingLoader(lambda key: "value-%s" % key)
            query = GraphQLObjectType(
                "Query",
                lambda: {
                    "sync": GraphQLField(
                        GraphQLString, resolve=lambda _root, _info: loader.load(1)
                    ),
                    "async": GraphQLField(GraphQLString, resolve=resolve_async),
                },
            )
            schema = GraphQLSchema(query)

            result = execute(schema, parse("{ sync async }"))

            assert not isawaitable(result)
            assert result.data is None
            assert len(result.errors) == 1
            assert result.errors[0].message == (
                "Cannot resolve deferred values during asynchronous execution."
                " Use synchronous execution when resolvers return deferred values."
            )
            # collect the abandoned coroutines without emitting warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                gc.collect()

    def describe_loader_api():
        def load_returns_a_deferred_value():
            loader = SyncDataLoader(lambda keys: keys)
            deferred = loader.load("key")

            assert isinstance(deferred, DeferredValue)
            assert deferred.loader is loader
            assert deferred.key == "key"
            assert repr(deferred) == "DeferredValue(loader=%r, key='key')" % loader

        def load_requires_a_hashable_key():
            loader = SyncDataLoader(lambda keys: keys)

            with raises(TypeError) as exc_info:
                loader.load(["unhashable"])

            assert "hashable key" in str(exc_info.value)

        def loader_requires_a_callable():
            with raises(TypeError) as exc_info:
                SyncDataLoader("not-a-callable")

            assert "batch loading function" in str(exc_info.value)
