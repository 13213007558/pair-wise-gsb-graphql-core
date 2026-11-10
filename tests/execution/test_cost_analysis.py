"""Tests for the optional pre-execution cost analysis and limit enforcement."""

import pytest

from graphql import (
    COST_EXTENSION_KEY,
    CostLimit,
    Executor,
    FieldCost,
    FieldCostInfo,
    GraphQLField,
    GraphQLList,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    build_schema,
    execute,
    execute_sync,
    parse,
)
from graphql.execution.cost_analysis import (
    DEFAULT_UNKNOWN_LIST_MULTIPLIER,
    check_cost_limit,
    get_operation_cost,
)

from ..fixtures import cleanup

pytestmark = pytest.mark.anyio


def build_test_schema() -> GraphQLSchema:
    """Small schema with explicit per-field cost configuration.

    Costs (weight / list multiplier):
    - Query.users: weight 1, list multiplier 10
    - User.friends: weight 2, list multiplier 5
    - User.tags: list multiplier 3 (default weight)
    - everything else: default weight 1, unknown list multiplier
    """
    schema = build_schema(
        """
        type Query {
          users(limit: Int = 10): [User!]!
          node(id: ID!): Node
          search(term: String!): [SearchResult!]!
          cheap: String
        }

        interface Node {
          id: ID!
        }

        type User implements Node {
          id: ID!
          name: String
          friends(limit: Int): [User!]
          tags: [String!]
        }

        type Organization implements Node {
          id: ID!
          name: String
          members: [User!]!
        }

        union SearchResult = User | Organization
        """
    )
    query_fields = schema.query_type.fields
    query_fields["users"].extensions[COST_EXTENSION_KEY] = {
        "weight": 1,
        "list_multiplier": 10,
    }
    query_fields["search"].extensions[COST_EXTENSION_KEY] = {
        "list_multiplier": 4,
    }
    user_fields = schema.get_type("User").fields
    user_fields["friends"].extensions[COST_EXTENSION_KEY] = {
        "weight": 2,
        "list_multiplier": 5,
    }
    user_fields["tags"].extensions[COST_EXTENSION_KEY] = {"list_multiplier": 3}
    # Organization.members intentionally has no cost configuration, so its list
    # multiplier is unknown and must trigger the conservative default.
    return schema


def analyze(schema, document, cost_limit, variable_values=None, operation_name=None):
    executor = Executor.build(
        schema,
        document,
        raw_variable_values=variable_values or {},
        operation_name=operation_name,
    )
    assert not isinstance(executor, list)
    return get_operation_cost(
        schema,
        executor.fragments,
        executor.operation,
        executor.variable_values,
        cost_limit,
    )


def describe_cost_computation():
    def bills_each_selected_field_with_default_weight():
        schema = build_test_schema()
        cost = analyze(
            schema, parse("{ cheap }"), CostLimit(max_cost=100)
        )
        assert cost.total == 1
        assert [entry.path for entry in cost.breakdown] == ["cheap"]

    def applies_list_multiplier_to_sub_selections_not_to_field_itself():
        schema = build_test_schema()
        # users has weight 1 with multiplier 10; its own entry costs 1.
        cost = analyze(
            schema, parse("{ users { id name } }"), CostLimit(max_cost=10_000)
        )
        paths = {entry.path: entry for entry in cost.breakdown}
        assert paths["users"].cost == 1
        # The two leaf fields each run once per list item: 1 * 10.
        assert paths["users.id"].cost == 10
        assert paths["users.name"].cost == 10
        # 1 + 10 + 10
        assert cost.total == 21

    def multiplies_nested_list_multipliers_along_the_path():
        schema = build_test_schema()
        # users(10) -> friends(5): friends weight 2 * 10, its children * 50.
        cost = analyze(
            schema,
            parse("{ users { friends { name } } }"),
            CostLimit(max_cost=10_000),
        )
        paths = {entry.path: entry for entry in cost.breakdown}
        assert paths["users"].cost == 1
        assert paths["users.friends"].cost == 2 * 10
        assert paths["users.friends.name"].multiplier == 50
        assert paths["users.friends.name"].cost == 50
        assert cost.total == 1 + 20 + 50

    def unknown_list_multiplier_uses_explicit_conservative_default():
        schema = build_test_schema()
        # Organization.members has no configured multiplier.
        cost = analyze(
            schema,
            parse(
                """
                { search(term: "x") {
                    ... on Organization { members { id } }
                } }
                """
            ),
            CostLimit(max_cost=10_000),
        )
        entry = next(
            entry
            for entry in cost.breakdown
            if entry.path == "search.members.id"
        )
        # search multiplier 4, then the conservative default (not 1).
        assert entry.multiplier == 4 * DEFAULT_UNKNOWN_LIST_MULTIPLIER
        assert entry.fallback_used is True
        assert cost.fallback_paths == ["search.members"]

    def custom_default_multiplier_is_used_for_unknown_lists():
        schema = build_test_schema()
        cost = analyze(
            schema,
            parse(
                """
                { search(term: "x") {
                    ... on Organization { members { id } }
                } }
                """
            ),
            CostLimit(max_cost=10_000, default_list_multiplier=25),
        )
        entry = next(
            entry
            for entry in cost.breakdown
            if entry.path == "search.members.id"
        )
        assert entry.multiplier == 4 * 25
        assert entry.fallback_used is True

    def explicit_multiplier_of_one_is_not_treated_as_unknown():
        schema = build_test_schema()
        schema.query_type.fields["cheap"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 1,
        }
        # cheap is not a list; instead use a list field configured with 1.
        schema.get_type("User").fields["tags"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 1,
        }
        cost = analyze(
            schema,
            parse("{ users { tags } }"),
            CostLimit(max_cost=10_000),
        )
        assert cost.fallback_paths == []
        tags_entry = next(
            entry for entry in cost.breakdown if entry.path == "users.tags"
        )
        # users multiplier 10, tags explicitly never fans out.
        assert tags_entry.multiplier == 10

    def bills_aliased_copies_separately_like_field_merging():
        schema = build_test_schema()
        cost = analyze(
            schema,
            parse("{ first: cheap second: cheap }"),
            CostLimit(max_cost=100),
        )
        assert cost.total == 2
        assert sorted(entry.path for entry in cost.breakdown) == [
            "first",
            "second",
        ]

    def merges_same_response_key_and_bills_once():
        schema = build_test_schema()
        document = parse(
            """
            {
              users { id name }
              users { name }
            }
            """
        )
        cost = analyze(schema, document, CostLimit(max_cost=10_000))
        # The two users selections merge into one field group like execution.
        assert [entry.path for entry in cost.breakdown] == [
            "users",
            "users.id",
            "users.name",
        ]
        assert cost.total == 21

    def fragment_reuse_is_billed_in_each_position():
        schema = build_test_schema()
        document = parse(
            """
            fragment UserFields on User { id name }
            {
              users { ...UserFields friends { ...UserFields } }
            }
            """
        )
        cost = analyze(schema, document, CostLimit(max_cost=10_000))
        paths = [entry.path for entry in cost.breakdown]
        # The fragment fields appear once under users and once under friends,
        # each at the multiplier of its own position.
        assert paths.count("users.id") == 1
        assert paths.count("users.friends.id") == 1
        friend_id = next(
            entry
            for entry in cost.breakdown
            if entry.path == "users.friends.id"
        )
        # users(10) * friends(5) = 50.
        assert friend_id.multiplier == 50

    def skip_and_include_are_evaluated_with_coerced_variables():
        schema = build_test_schema()
        document = parse(
            """
            query Q($withFriends: Boolean!, $skipName: Boolean!) {
              users {
                id
                name @skip(if: $skipName)
                friends(limit: 1) @include(if: $withFriends) { id }
              }
            }
            """
        )
        skipped = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000),
            variable_values={"withFriends": False, "skipName": True},
        )
        paths = {entry.path for entry in skipped.breakdown}
        assert "users.name" not in paths
        assert "users.friends" not in paths
        assert paths == {"users", "users.id"}

        included = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000),
            variable_values={"withFriends": True, "skipName": False},
        )
        included_paths = {entry.path for entry in included.breakdown}
        assert "users.name" in included_paths
        assert "users.friends" in included_paths
        assert "users.friends.id" in included_paths

    def inline_fragment_directive_is_evaluated_with_variables():
        schema = build_schema(
            """
            type Query { users: [User!]! }
            type User { id: ID! profile: Profile }
            type Profile { bio: String }
            """
        )
        schema.query_type.fields["users"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 10
        }
        document = parse(
            """
            query Q($show: Boolean!) {
              users {
                id
                ... @include(if: $show) { profile { bio } }
              }
            }
            """
        )
        hidden = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000),
            variable_values={"show": False},
        )
        assert {entry.path for entry in hidden.breakdown} == {
            "users",
            "users.id",
        }
        shown = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000),
            variable_values={"show": True},
        )
        assert "users.profile.bio" in {
            entry.path for entry in shown.breakdown
        }

    def introspection_typename_is_billed_like_a_normal_leaf():
        schema = build_schema(
            """
            type Query { users: [User!]! }
            type User { id: ID! }
            """
        )
        schema.query_type.fields["users"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 10
        }
        cost = analyze(
            schema,
            parse("{ users { __typename } }"),
            CostLimit(max_cost=10_000),
        )
        # users itself costs 1; __typename runs once per list item.
        assert cost.total == 1 + 10

    def deferred_and_streamed_selections_are_still_billed():
        schema = build_schema(
            """
            type Query { users: [User!]! }
            type User { id: ID! name: String }
            """
        )
        schema.query_type.fields["users"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 10
        }
        deferred = parse(
            "{ users { id ... @defer { name } } }"
        )
        cost = analyze(
            schema, deferred, CostLimit(max_cost=10_000)
        )
        assert {entry.path for entry in cost.breakdown} == {
            "users",
            "users.id",
            "users.name",
        }
        streamed = parse("{ users @stream(initialCount: 1) { id } }")
        stream_cost = analyze(
            schema, streamed, CostLimit(max_cost=10_000)
        )
        # Streaming changes delivery, not the static amount of work.
        assert stream_cost.total == 1 + 10

    def only_the_selected_operation_is_billed():
        schema = build_test_schema()
        document = parse(
            """
            query One { cheap }
            query Two { users { id name } }
            """
        )
        cost = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000),
            operation_name="One",
        )
        assert [entry.path for entry in cost.breakdown] == ["cheap"]
        assert cost.total == 1

    def abstract_type_bills_the_most_expensive_possible_branch():
        schema = build_test_schema()
        # Node can be User or Organization. The Organization branch contains
        # the members list with an unknown multiplier, so it is the worst case.
        document = parse(
            """
            { node(id: "1") {
                id
                ... on User { name }
                ... on Organization { name members { id } }
            } }
            """
        )
        cost = analyze(schema, document, CostLimit(max_cost=10_000))
        paths = {entry.path: entry for entry in cost.breakdown}
        # The User-only branch is not part of the worst case.
        assert "node.name" in paths  # present in both branches, billed once
        assert "node.members" in paths
        assert "node.members.id" in paths
        members_id = paths["node.members.id"]
        # No safe multiplier is known for members, so the conservative default
        # applies rather than an implicit 1.
        assert members_id.multiplier == DEFAULT_UNKNOWN_LIST_MULTIPLIER
        assert members_id.fallback_used is True
        # node(1) + id(1) + name(1) + members(1) + members.id(100) = 104
        assert cost.total == 104
        assert cost.fallback_paths == ["node.members"]

    def union_branch_selection_follows_runtime_type_conditions():
        schema = build_test_schema()
        document = parse(
            """
            { search(term: "x") {
                ... on User { name }
                ... on Organization { name }
            } }
            """
        )
        cost = analyze(schema, document, CostLimit(max_cost=10_000))
        paths = {entry.path for entry in cost.breakdown}
        # Both branches have identical cost (search multiplier 4, one leaf).
        assert paths == {"search", "search.name"}
        assert cost.total == 1 + 4

    def callback_receives_coerced_arguments_and_schema_metadata():
        schema = build_test_schema()
        seen = []

        def callback(info: FieldCostInfo):
            seen.append((info.field_name, info.args))
            if info.field_name == "users":
                # Derive the multiplier from the validated limit argument.
                return FieldCost(
                    weight=1, list_multiplier=max(1, info.args["limit"])
                )
            return None

        document = parse("query Q($limit: Int!) { users(limit: $limit) { id } }")
        cost = analyze(
            schema,
            document,
            CostLimit(max_cost=10_000, cost_callback=callback),
            variable_values={"limit": 3},
        )
        assert ("users", {"limit": 3}) in seen
        entry = next(
            entry for entry in cost.breakdown if entry.path == "users.id"
        )
        assert entry.multiplier == 3

    def callback_cannot_see_resolvers_or_context():
        schema = build_test_schema()

        def callback(info: FieldCostInfo):
            # The info object exposes only schema metadata and coerced args.
            assert not hasattr(info, "context")
            assert not hasattr(info, "root_value")
            assert info.schema is schema
            return FieldCost(weight=3)

        cost = analyze(
            schema,
            parse("{ cheap }"),
            CostLimit(max_cost=10_000, cost_callback=callback),
        )
        assert cost.total == 3


def describe_cost_limit_enforcement():
    def request_exactly_at_the_limit_is_allowed():
        schema = build_test_schema()
        document = parse("{ users { id name } }")
        # Exact computed cost is 21.
        executor = Executor.build(schema, document)
        assert not isinstance(executor, list)
        assert (
            check_cost_limit(
                schema,
                executor.fragments,
                executor.operation,
                executor.variable_values,
                CostLimit(max_cost=21),
            )
            is None
        )

    def request_over_the_limit_is_rejected_with_main_cost_source():
        schema = build_test_schema()
        document = parse("{ users { friends { name } } }")
        executor = Executor.build(schema, document)
        assert not isinstance(executor, list)
        error = check_cost_limit(
            schema,
            executor.fragments,
            executor.operation,
            executor.variable_values,
            CostLimit(max_cost=10),
        )
        assert error is not None
        assert "exceeds the maximum allowed cost 10" in error.message
        assert "users.friends.name" in error.message
        assert error.nodes and error.nodes[0].name.value == "name"

    def rejection_error_mentions_conservative_fallback_paths():
        schema = build_test_schema()
        document = parse(
            """
            { search(term: "x") {
                ... on Organization { members { id } }
            } }
            """
        )
        executor = Executor.build(schema, document)
        assert not isinstance(executor, list)
        error = check_cost_limit(
            schema,
            executor.fragments,
            executor.operation,
            executor.variable_values,
            CostLimit(max_cost=1),
        )
        assert error is not None
        assert "search.members" in error.message
        assert "conservative default list multiplier" in error.message

    def rejected_request_runs_no_resolver():
        schema = build_test_schema()
        calls = []

        def users_resolver(_source, _info, limit=10):
            calls.append(("users", limit))
            return [{"id": "1", "name": "Ada"} for _ in range(limit)]

        def friends_resolver(_source, _info, limit=None):
            calls.append(("friends", limit))
            return []

        schema.query_type.fields["users"].resolve = users_resolver
        schema.get_type("User").fields["friends"].resolve = friends_resolver

        result = execute_sync(
            schema,
            parse("{ users(limit: 3) { id friends(limit: 2) { id } } }"),
            cost_limit=CostLimit(max_cost=1),
        )
        assert result.data is None
        assert result.errors is not None
        assert "exceeds the maximum allowed cost" in result.errors[0].message
        # No business work started: neither root resolver nor nested resolver.
        assert calls == []

    def allowed_request_runs_resolvers_normally():
        schema = build_test_schema()
        calls = []

        def users_resolver(_source, _info, limit=10):
            calls.append("users")
            return [{"id": str(index)} for index in range(limit)]

        schema.query_type.fields["users"].resolve = users_resolver
        result = execute_sync(
            schema,
            parse("{ users(limit: 2) { id } }"),
            cost_limit=CostLimit(max_cost=10_000),
        )
        assert result.errors is None
        assert result.data == {"users": [{"id": "0"}, {"id": "1"}]}
        assert calls == ["users"]

    def no_cost_limit_leaves_execution_completely_unchanged():
        schema = build_test_schema()
        result = execute_sync(
            schema,
            parse("{ cheap }"),
            root_value={"cheap": "ok"},
        )
        assert result == ({"cheap": "ok"}, None)

    def high_level_graphql_sync_enforces_the_same_limit():
        from graphql import graphql_sync

        schema = build_test_schema()
        calls = []

        def users_resolver(_source, _info, limit=10):
            calls.append("users")
            return [{"id": str(index)} for index in range(limit)]

        schema.query_type.fields["users"].resolve = users_resolver
        result = graphql_sync(
            schema,
            "{ users(limit: 2) { id friends { id } } }",
            cost_limit=CostLimit(max_cost=1),
        )
        assert result.data is None
        assert result.errors is not None
        assert "exceeds the maximum allowed cost" in result.errors[0].message
        assert calls == []

    def coercion_errors_are_request_errors_and_skip_resolvers():
        schema = build_test_schema()
        calls = []
        schema.query_type.fields["search"].resolve = lambda *a, **k: calls.append(
            "search"
        ) or []
        # term is required; omitting it fails argument coercion.
        result = execute_sync(
            schema,
            parse("{ search { __typename } }"),
            cost_limit=CostLimit(max_cost=10_000),
        )
        assert result.data is None
        assert result.errors is not None
        assert calls == []

    def asynchronous_execution_is_rejected_before_resolvers():
        async def users_resolver(_source, _info, limit=10):
            raise AssertionError("resolver must not run when over the limit")

        schema = build_test_schema()
        schema.query_type.fields["users"].resolve = users_resolver

        import asyncio

        # The rejection happens before any resolver is invoked, so the result is
        # available synchronously even though the schema uses async resolvers.
        result = execute(
            schema,
            parse("{ users(limit: 3) { id friends { id } } }"),
            cost_limit=CostLimit(max_cost=1),
        )
        if hasattr(result, "__await__"):  # pragma: no cover
            result = asyncio.run(result)
        assert result.data is None
        assert result.errors is not None
        assert "exceeds the maximum allowed cost" in result.errors[0].message
        cleanup()

    def cost_limit_works_with_custom_executor_subclass():
        schema = build_test_schema()

        class CountingExecutor(Executor):
            cost_checks = 0

            def check_operation_cost(self):
                type(self).cost_checks += 1
                return super().check_operation_cost()

        result = execute_sync(
            schema,
            parse("{ cheap }"),
            root_value={"cheap": "ok"},
            executor_class=CountingExecutor,
            cost_limit=CostLimit(max_cost=1),
        )
        assert result == ({"cheap": "ok"}, None)
        assert CountingExecutor.cost_checks == 1


def describe_subscription_cost_limit():
    def subscription_over_limit_does_not_open_source_stream():
        subscription_schema = build_schema(
            """
            type Query {
              cheap: String
            }
            type Subscription {
              updates(limit: Int!): [Update!]!
            }
            type Update {
              id: ID!
              text: String
            }
            """
        )
        subscription_schema.subscription_type.fields["updates"].extensions[
            COST_EXTENSION_KEY
        ] = {"list_multiplier": 10}
        opened = []

        async def updates_resolver(_source, _info, limit):
            opened.append(limit)
            if False:  # pragma: no cover
                yield None

        subscription_schema.subscription_type.fields[
            "updates"
        ].subscribe = updates_resolver

        from graphql import subscribe
        from graphql.pyutils import is_awaitable

        # updates itself costs 1, but each of its two sub-fields runs per list
        # item: 1 + 2 * 10 = 21, exceeding the limit of 1.
        result_or_stream = subscribe(
            subscription_schema,
            parse("subscription { updates(limit: 5) { id text } }"),
            cost_limit=CostLimit(max_cost=1),
        )
        if is_awaitable(result_or_stream):  # pragma: no cover
            import asyncio

            result_or_stream = asyncio.run(result_or_stream)

        # Over the limit: an errors-only result instead of an event stream.
        assert hasattr(result_or_stream, "errors")
        assert result_or_stream.errors is not None
        assert "exceeds the maximum allowed cost" in (
            result_or_stream.errors[0].message
        )
        assert opened == []
        cleanup()


def describe_schema_construction_with_cost_extensions():
    def extensions_can_be_declared_directly_on_graphql_field():
        field = GraphQLField(
            GraphQLList(GraphQLString),
            extensions={COST_EXTENSION_KEY: {"weight": 5, "list_multiplier": 8}},
        )
        query_type = GraphQLObjectType("Query", {"items": field})
        schema = GraphQLSchema(query_type)

        cost = analyze(
            schema, parse("{ items }"), CostLimit(max_cost=10_000)
        )
        assert cost.total == 5
        entry = cost.breakdown[0]
        assert entry.weight == 5

    def non_null_list_type_keeps_multiplier_semantics():
        schema = build_schema(
            """
            type Query {
              matrix: [[Int!]!]!
            }
            """
        )
        schema.query_type.fields["matrix"].extensions[COST_EXTENSION_KEY] = {
            "list_multiplier": 3,
        }
        cost = analyze(
            schema,
            parse("{ matrix }"),
            CostLimit(max_cost=10_000),
        )
        # No sub-selections on a leaf list; the field itself costs its weight.
        assert cost.breakdown[0].cost == 1
        # Two list dimensions are recorded structurally via the multiplier that
        # would apply to sub-selections (3 ** 2) when the element is an object.
        nested_schema = build_schema(
            """
            type Query {
              grid: [[Cell!]!]!
            }
            type Cell {
              value: Int
            }
            """
        )
        nested_schema.query_type.fields["grid"].extensions[
            COST_EXTENSION_KEY
        ] = {"list_multiplier": 3}
        nested_cost = analyze(
            nested_schema,
            parse("{ grid { value } }"),
            CostLimit(max_cost=10_000),
        )
        entry = next(
            entry
            for entry in nested_cost.breakdown
            if entry.path == "grid.value"
        )
        assert entry.multiplier == 9


def describe_docstring_examples():
    def default_callback_reads_extensions():
        from graphql.execution.cost_analysis import default_cost_callback

        schema = build_schema("type Query { users: [String] }")
        field = schema.query_type.fields["users"]
        info = FieldCostInfo(
            field=field,
            parent_type=schema.query_type,
            field_name="users",
            response_name="users",
            args={},
            schema=schema,
            is_list=True,
            default_list_multiplier=DEFAULT_UNKNOWN_LIST_MULTIPLIER,
        )
        assert default_cost_callback(info) is None
        field.extensions[COST_EXTENSION_KEY] = {"weight": 7}
        assert default_cost_callback(info) == FieldCost(weight=7,
                                                        list_multiplier=None)
