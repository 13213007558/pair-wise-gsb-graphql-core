from inspect import isawaitable

from pytest import mark, raises

from graphql import (
    DeferredValue,
    GraphQLArgument,
    GraphQLField,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    SyncBatchLoader,
    execute,
    execute_sync,
    graphql_sync,
    parse,
)

pytestmark = mark.filterwarnings("ignore:.* was never awaited:RuntimeWarning")


class RecordingLoader:
    """A synchronous loader that records the keys of every dispatched batch."""

    def __init__(self, batch_load_fn):
        self.batch_load_fn = batch_load_fn
        self.batches = []

    def load(self, key):
        return DeferredValue(self, key)

    def batch_load(self, keys):
        self.batches.append(list(keys))
        return self.batch_load_fn(keys)


USERS = {
    str(i): {
        "id": str(i),
        "name": f"User {i}",
        "friend_ids": [str((i + 1) % 10), str((i + 2) % 10)],
    }
    for i in range(10)
}

ITEMS = [{"id": str(i), "owner_id": str(i % 10)} for i in range(100)]
ITEMS_BY_ID = {item["id"]: item for item in ITEMS}


def load_users(keys):
    return [USERS[key] for key in keys]


def make_schema(user_loader, events=None):
    def resolve_owner(item, _info):
        return user_loader.load(item["owner_id"])

    def resolve_friends(user, _info):
        return [user_loader.load(key) for key in user["friend_ids"]]

    user_type = GraphQLObjectType(
        "User",
        lambda: {
            "name": GraphQLField(GraphQLString),
            "friends": GraphQLField(
                GraphQLList(user_type), resolve=resolve_friends
            ),
        },
    )

    item_type = GraphQLObjectType(
        "Item",
        {
            "id": GraphQLField(GraphQLString),
            "owner": GraphQLField(user_type, resolve=resolve_owner),
            "requiredOwner": GraphQLField(
                GraphQLNonNull(user_type), resolve=resolve_owner
            ),
        },
    )

    def resolve_item(_root, _info, id):
        return ITEMS_BY_ID[id]

    query_type = GraphQLObjectType(
        "Query",
        {
            "items": GraphQLField(
                GraphQLList(item_type), resolve=lambda *_: ITEMS
            ),
            "item": GraphQLField(
                item_type,
                args={"id": GraphQLArgument(GraphQLString)},
                resolve=resolve_item,
            ),
        },
    )

    def make_touch(name):
        def resolve_touch(_root, _info, id):
            if events is not None:
                events.append(("resolve", name))
            return ITEMS_BY_ID[id]

        return resolve_touch

    mutation_type = GraphQLObjectType(
        "Mutation",
        {
            "first": GraphQLField(
                item_type,
                args={"id": GraphQLArgument(GraphQLString)},
                resolve=make_touch("first"),
            ),
            "second": GraphQLField(
                item_type,
                args={"id": GraphQLArgument(GraphQLString)},
                resolve=make_touch("second"),
            ),
        },
    )

    return GraphQLSchema(query=query_type, mutation=mutation_type)


def describe_deferred_values():
    def collects_same_level_loads_into_one_batch():
        user_loader = RecordingLoader(load_users)
        schema = make_schema(user_loader)
        result = execute_sync(schema, parse("{ items { owner { name } } }"))
        assert result.errors is None
        # all 100 owners are loaded in one batch, each key only once
        assert user_loader.batches == [[str(i) for i in range(10)]]
        # and the results are mapped back to the right positions in the list
        assert result.data == {
            "items": [
                {"owner": {"name": f"User {i % 10}"}} for i in range(100)
            ]
        }

    def loads_next_level_in_the_next_batch():
        user_loader = RecordingLoader(load_users)
        schema = make_schema(user_loader)
        query = "{ items { owner { name friends { name } } } }"
        result = execute_sync(schema, parse(query))
        assert result.errors is None
        # the owners form the first batch, their friends the second one
        assert len(user_loader.batches) == 2
        assert user_loader.batches[0] == [str(i) for i in range(10)]
        assert sorted(user_loader.batches[1]) == [str(i) for i in range(10)]
        assert result.data["items"][0] == {
            "owner": {
                "name": "User 0",
                "friends": [{"name": "User 1"}, {"name": "User 2"}],
            }
        }

    def maps_results_back_by_key_with_aliases():
        user_loader = RecordingLoader(load_users)
        schema = make_schema(user_loader)
        query = """
            {
              first: item(id: "7") { owner { name } }
              second: item(id: "3") { owner { name } }
              third: item(id: "7") { owner { name } }
            }
        """
        result = execute_sync(schema, parse(query))
        assert result.errors is None
        # the duplicate key "7" is only loaded once
        assert user_loader.batches == [["7", "3"]]
        assert result.data == {
            "first": {"owner": {"name": "User 7"}},
            "second": {"owner": {"name": "User 3"}},
            "third": {"owner": {"name": "User 7"}},
        }

    def supports_lists_of_deferred_values():
        user_loader = RecordingLoader(load_users)
        schema = make_schema(user_loader)
        query = '{ item(id: "0") { owner { friends { name } } } }'
        result = execute_sync(schema, parse(query))
        assert result.errors is None
        assert user_loader.batches == [["0"], ["1", "2"]]
        assert result.data == {
            "item": {
                "owner": {"friends": [{"name": "User 1"}, {"name": "User 2"}]}
            }
        }

    def does_not_share_caches_between_executions():
        user_loader = RecordingLoader(load_users)
        schema = make_schema(user_loader)
        document = parse("{ items { owner { name } } }")
        assert execute_sync(schema, document).errors is None
        assert execute_sync(schema, document).errors is None
        assert user_loader.batches == [
            [str(i) for i in range(10)],
            [str(i) for i in range(10)],
        ]

    def describe_error_handling():
        def _load_users_with_failure(keys):
            return [
                Exception(f"cannot load {key}") if key == "4" else USERS[key]
                for key in keys
            ]

        def fails_single_items_without_failing_the_whole_batch():
            user_loader = RecordingLoader(_load_users_with_failure)
            schema = make_schema(user_loader)
            result = execute_sync(schema, parse("{ items { owner { name } } }"))
            # everything was still loaded in a single batch
            assert user_loader.batches == [[str(i) for i in range(10)]]
            # only the fields with the failing key are nulled
            items = result.data["items"]
            assert len(items) == 100
            for index, item in enumerate(items):
                if index % 10 == 4:
                    assert item == {"owner": None}
                else:
                    assert item == {"owner": {"name": f"User {index % 10}"}}
            assert result.errors is not None
            assert len(result.errors) == 10
            error = result.errors[0]
            assert error.message == "cannot load 4"
            assert error.path == ["items", 4, "owner"]

        def bubbles_errors_according_to_nullability():
            user_loader = RecordingLoader(_load_users_with_failure)
            schema = make_schema(user_loader)
            query = "{ items { requiredOwner { name } } }"
            result = execute_sync(schema, parse(query))
            items = result.data["items"]
            assert len(items) == 100
            for index, item in enumerate(items):
                if index % 10 == 4:
                    # the non-null owner failed, so the whole item is null
                    assert item is None
                else:
                    assert item == {"requiredOwner": {"name": f"User {index % 10}"}}
            assert result.errors is not None
            assert len(result.errors) == 10
            assert result.errors[0].path == ["items", 4, "requiredOwner"]

        def bubbles_errors_of_non_null_root_fields_to_the_root():
            user_loader = RecordingLoader(_load_users_with_failure)
            schema = make_schema(user_loader)
            query = "{ items { requiredOwner { name } } }"
            # make the root field itself non-nullable
            item_type = schema.query_type.fields["items"].type.of_type
            schema.query_type.fields["items"].type = GraphQLNonNull(
                GraphQLList(GraphQLNonNull(item_type))
            )
            result = execute_sync(schema, parse(query))
            assert result.data is None
            assert result.errors is not None
            # each failing item propagates its own located error to the root
            assert len(result.errors) == 10
            assert result.errors[0].path == ["items", 4, "requiredOwner"]

        def handles_a_batch_function_that_raises():
            def failing_batch_load(keys):
                raise RuntimeError("database is down")

            user_loader = RecordingLoader(failing_batch_load)
            schema = make_schema(user_loader)
            query = '{ item(id: "1") { owner { name } } }'
            result = execute_sync(schema, parse(query))
            assert result.data == {"item": {"owner": None}}
            assert result.errors is not None
            assert len(result.errors) == 1
            assert result.errors[0].message == "database is down"
            assert result.errors[0].path == ["item", "owner"]

        def handles_a_batch_function_with_wrong_result_count():
            user_loader = RecordingLoader(lambda keys: [])
            schema = make_schema(user_loader)
            query = '{ item(id: "1") { owner { name } } }'
            result = execute_sync(schema, parse(query))
            assert result.data == {"item": {"owner": None}}
            assert result.errors is not None
            assert len(result.errors) == 1
            assert result.errors[0].message == (
                "Loader returned an incorrect number of results:"
                " expected 1, got 0."
            )

    def describe_mutation_root_fields_stay_serial():
        def loads_derived_values_before_the_next_root_field():
            events = []

            def batch_load(keys):
                events.append(("batch", list(keys)))
                return [USERS[key] for key in keys]

            user_loader = RecordingLoader(batch_load)
            schema = make_schema(user_loader, events)
            query = """
                mutation {
                  first(id: "1") { owner { name friends { name } } }
                  second(id: "2") { owner { name } }
                }
            """
            result = execute_sync(schema, parse(query))
            assert result.errors is None
            # the second root field is only resolved after the first root
            # field and all its derived loads have been completed
            assert events == [
                ("resolve", "first"),
                ("batch", ["1"]),
                ("batch", ["2", "3"]),
                ("resolve", "second"),
                ("batch", ["2"]),
            ]
            assert result.data == {
                "first": {
                    "owner": {
                        "name": "User 1",
                        "friends": [{"name": "User 2"}, {"name": "User 3"}],
                    }
                },
                "second": {"owner": {"name": "User 2"}},
            }

    def describe_sync_execution_behavior_is_preserved():
        def execute_returns_synchronously_for_deferred_values():
            user_loader = RecordingLoader(load_users)
            schema = make_schema(user_loader)
            result = execute(schema, parse("{ items { owner { name } } }"))
            assert not isawaitable(result)
            assert result.errors is None
            assert result.data["items"][0] == {"owner": {"name": "User 0"}}

        def execute_sync_works_with_check_sync():
            user_loader = RecordingLoader(load_users)
            schema = make_schema(user_loader)
            result = execute_sync(
                schema, parse("{ items { owner { name } } }"), check_sync=True
            )
            assert result.errors is None
            assert user_loader.batches == [[str(i) for i in range(10)]]

        def graphql_sync_works_with_deferred_values():
            user_loader = RecordingLoader(load_users)
            schema = make_schema(user_loader)
            result = graphql_sync(schema, "{ items { owner { name } } }")
            assert result.errors is None
            assert result.data["items"][0] == {"owner": {"name": "User 0"}}

        @mark.asyncio
        async def works_alongside_async_resolvers_in_async_execution():
            async def resolve_async(*_args):
                return "Async"

            user_loader = RecordingLoader(load_users)
            schema = make_schema(user_loader)
            schema.query_type.fields["async"] = GraphQLField(
                GraphQLString, resolve=resolve_async
            )
            query = '{ item(id: "1") { owner { name } } async }'
            result = execute(schema, parse(query))
            assert isawaitable(result)
            result = await result
            assert result.errors is None
            assert result.data == {
                "item": {"owner": {"name": "User 1"}},
                "async": "Async",
            }
            assert user_loader.batches == [["1"]]

        @mark.filterwarnings("ignore:.* was never awaited:RuntimeWarning")
        def fails_clearly_when_an_awaitable_is_returned():
            async def resolve_async(*_args):
                return "async"

            user_loader = RecordingLoader(load_users)
            schema = make_schema(user_loader)
            schema.query_type.fields["async"] = GraphQLField(
                GraphQLString, resolve=resolve_async
            )
            query = '{ item(id: "1") { owner { name } } async }'
            with raises(RuntimeError) as exc_info:
                execute_sync(schema, parse(query), check_sync=True)
            assert str(exc_info.value) == (
                "GraphQL execution failed to complete synchronously."
            )

    def describe_loaders():
        def supports_a_sync_batch_loader():
            calls = []

            def batch_load_fn(keys):
                calls.append(list(keys))
                return [USERS[key] for key in keys]

            loader = SyncBatchLoader(batch_load_fn)
            schema = make_schema(loader)
            result = execute_sync(schema, parse("{ items { owner { name } } }"))
            assert result.errors is None
            assert calls == [[str(i) for i in range(10)]]

        def supports_a_plain_callable_as_loader():
            calls = []

            def batch_load_fn(keys):
                calls.append(list(keys))
                return [USERS[key] for key in keys]

            class CallableLoader:
                def load(self, key):
                    return DeferredValue(batch_load_fn, key)

            schema = make_schema(CallableLoader())
            result = execute_sync(schema, parse("{ items { owner { name } } }"))
            assert result.errors is None
            assert calls == [[str(i) for i in range(10)]]
