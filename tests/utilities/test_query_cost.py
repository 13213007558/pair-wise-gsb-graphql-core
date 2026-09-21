import pytest

from graphql import (
    GraphQLError,
    QueryCostError,
    QueryCostExceededError,
    assert_cost_within_budget,
    build_schema,
    estimate_query_cost,
    execute_sync,
    parse,
)
from graphql.utilities import (
    DEFAULT_COST_LIMIT,
    DEFAULT_EFFORT_LIMIT,
    MAX_LIST_MULTIPLIER,
)

schema = build_schema(
    """
    type Query {
      version: String!
      user(id: ID): User
      users: [User!]!
      node: Node
      search: [SearchResult!]!
    }

    interface Node {
      id: ID!
    }

    type User implements Node {
      id: ID!
      name: String!
      friends: [User!]!
    }

    type Post implements Node {
      id: ID!
      title: String!
      tags: [[String!]!]!
    }

    union SearchResult = User | Post
    """
)

MULTIPLIERS = {
    "Query.users": 2,
    "User.friends": 3,
    "Query.search": 5,
    "Post.tags": 4,
}


def describe_estimate_query_cost():
    def counts_a_single_field_with_default_cost():
        assert estimate_query_cost(schema, "{ version }") == 1

    def accepts_a_parsed_document():
        document = parse("{ version version }")
        # Same response key is merged and counted only once.
        assert estimate_query_cost(schema, document) == 1

    def uses_configured_field_costs():
        field_costs = {"Query.user": 10, "User.id": 2}
        cost = estimate_query_cost(schema, "{ user { id } }", field_costs=field_costs)
        assert cost == 12

    def uses_default_field_cost_for_unconfigured_fields():
        field_costs = {"Query.user": 10}
        cost = estimate_query_cost(schema, "{ user { id } }", field_costs=field_costs)
        assert cost == 11

    def merges_fields_with_the_same_response_key():
        assert estimate_query_cost(schema, "{ user { id id } }") == 2
        query = "{ user { id ...F } } fragment F on User { id name }"
        assert estimate_query_cost(schema, query) == 3

    def counts_repeated_fragment_spreads_only_once():
        query = "{ user { ...F ...F } } fragment F on User { name }"
        assert estimate_query_cost(schema, query) == 2

    def counts_nested_fragment_spreads_only_once():
        query = """
        { user { ...A ...B } }
        fragment A on User { ...C }
        fragment B on User { ...C }
        fragment C on User { name }
        """
        assert estimate_query_cost(schema, query) == 2

    def counts_different_aliases_separately():
        assert estimate_query_cost(schema, "{ a: user { id } b: user { id } }") == 4
        assert estimate_query_cost(schema, "{ user { x: name y: name } }") == 3

    def evaluates_skip_and_include_with_variable_defaults():
        query = """
        query($withName: Boolean = true, $skipFriends: Boolean = false) {
          user {
            id
            name @include(if: $withName)
            friends @skip(if: $skipFriends) { id }
          }
        }
        """
        # user(1) + id(1) + name(1) + friends(1 + id(1)) * 3 = 9
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 9
        assert (
            estimate_query_cost(
                schema, query, variable_values={}, list_multipliers=MULTIPLIERS
            )
            == 9
        )

    def evaluates_skip_and_include_with_provided_variables():
        query = """
        query($withName: Boolean = true, $skipFriends: Boolean = false) {
          user {
            id
            name @include(if: $withName)
            friends @skip(if: $skipFriends) { id }
          }
        }
        """
        variables = {"withName": False, "skipFriends": True}
        # user(1) + id(1) = 2
        assert (
            estimate_query_cost(
                schema, query, variable_values=variables, list_multipliers=MULTIPLIERS
            )
            == 2
        )

    def evaluates_directives_on_fragment_spreads():
        query = """
        query($details: Boolean = false) {
          user { id ...F @include(if: $details) }
        }
        fragment F on User { name }
        """
        assert estimate_query_cost(schema, query) == 2
        cost = estimate_query_cost(schema, query, variable_values={"details": True})
        assert cost == 3

    def uses_the_most_expensive_branch_for_interfaces():
        query = """
        { node { id ... on User { name friends { id } } ... on Post { title } } }
        """
        # User branch: id(1) + name(1) + friends(1 + id(1)) * 3 = 8
        # Post branch: id(1) + title(1) = 2
        # node(1) + max(8, 2) = 9 (not the sum 11)
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 9

    def uses_the_most_expensive_branch_for_unions():
        query = "{ search { ... on User { name } ... on Post { title } } }"
        # search(1 + max(name(1), title(1))) * 5 = 10
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 10

    def ignores_branches_that_cannot_match():
        query = "{ node { ... on Post { tags } } }"
        # node(1) + max(User branch: 0, Post branch: tags(1) * 4 * 4) = 17
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 17

    def multiplies_nested_list_fields_layer_by_layer():
        # users(1 + friends(1 + id(1)) * 3) * 2 = 14
        query = "{ users { friends { id } } }"
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 14

    def multiplies_each_layer_of_a_nested_list_type():
        # tags is [[String!]!]!, so its multiplier applies twice: 1 * 4 * 4
        query = "{ node { ... on Post { tags } } }"
        assert estimate_query_cost(schema, query, list_multipliers=MULTIPLIERS) == 17

    def selects_between_multiple_named_operations():
        document = """
        query A { version }
        query B { user { id } }
        """
        assert estimate_query_cost(schema, document, "A") == 1
        assert estimate_query_cost(schema, document, "B") == 2

    def requires_an_operation_name_for_multiple_operations():
        document = """
        query A { version }
        query B { user { id } }
        """
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, document)
        assert "Must provide operation name" in str(exc_info.value)

    def rejects_an_unknown_operation_name():
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, "{ version }", "C")
        assert "Unknown operation named 'C'" in str(exc_info.value)

    def rejects_a_document_without_operation():
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, "fragment F on User { id }")
        assert "Cannot estimate cost" in str(exc_info.value)

    def rejects_syntax_errors():
        with pytest.raises(GraphQLError):
            estimate_query_cost(schema, "{ user {")

    def rejects_invalid_operations_instead_of_returning_a_low_cost():
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, "{ user { noSuchField } }")
        assert "invalid operation" in str(exc_info.value)
        assert "noSuchField" in str(exc_info.value)

    def rejects_unknown_fragments():
        with pytest.raises(QueryCostError):
            estimate_query_cost(schema, "{ user { ...Missing } }")

    def rejects_cyclic_fragments_without_hanging():
        query = """
        { user { ...A } }
        fragment A on User { ...B }
        fragment B on User { ...A }
        """
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, query)
        assert "invalid operation" in str(exc_info.value)

    def rejects_invalid_variable_values():
        query = "query($id: ID!) { user(id: $id) { id } }"
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, query, variable_values={})
        assert "invalid variable values" in str(exc_info.value)

    @pytest.mark.parametrize("multiplier", [0, -1, 10**9, "many", 2.5, True])
    def rejects_invalid_list_multipliers(multiplier):
        with pytest.raises(QueryCostError):
            estimate_query_cost(
                schema, "{ users { id } }", list_multipliers={"Query.users": multiplier}
            )

    def accepts_the_maximum_list_multiplier():
        query = "{ users { id } }"
        cost = estimate_query_cost(
            schema,
            query,
            list_multipliers={"Query.users": MAX_LIST_MULTIPLIER},
            cost_limit=10 * DEFAULT_COST_LIMIT,
        )
        assert cost == 2 * MAX_LIST_MULTIPLIER

    def rejects_an_invalid_default_list_multiplier():
        with pytest.raises(QueryCostError):
            estimate_query_cost(schema, "{ users { id } }", default_list_multiplier=0)

    def rejects_absurdly_large_field_costs():
        with pytest.raises(QueryCostError):
            estimate_query_cost(
                schema, "{ version }", field_costs={"Query.version": 10**12}
            )

    def stops_when_the_cost_limit_is_exceeded():
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, "{ user { id name } }", cost_limit=2)
        assert "cost limit" in str(exc_info.value)

    def stops_when_the_effort_limit_is_exceeded():
        with pytest.raises(QueryCostError) as exc_info:
            estimate_query_cost(schema, "{ user { id name } }", effort_limit=2)
        assert "effort limit" in str(exc_info.value)

    def exposes_sensible_default_limits():
        assert DEFAULT_COST_LIMIT > 0
        assert DEFAULT_EFFORT_LIMIT > 0
        assert MAX_LIST_MULTIPLIER > 0


# Cost of this query with the configured multipliers is 14 (see above).
BUDGET_QUERY = "{ users { friends { id } } }"


def describe_assert_cost_within_budget():
    def returns_the_cost_when_within_budget():
        cost = assert_cost_within_budget(
            schema,
            BUDGET_QUERY,
            14,
            list_multipliers=MULTIPLIERS,
        )
        assert cost == 14

    def raises_when_the_budget_is_exceeded():
        with pytest.raises(QueryCostExceededError) as exc_info:
            assert_cost_within_budget(
                schema,
                BUDGET_QUERY,
                13,
                list_multipliers=MULTIPLIERS,
            )
        assert exc_info.value.budget == 13
        assert "exceeds the budget of 13" in str(exc_info.value)

    def is_a_query_cost_error():
        with pytest.raises(QueryCostError):
            assert_cost_within_budget(
                schema,
                BUDGET_QUERY,
                1,
                list_multipliers=MULTIPLIERS,
            )


def describe_admission_control_integration():
    def rejected_queries_never_execute_resolvers():
        calls = []

        def counting_resolver(_obj, _info):
            calls.append("user")
            return {"id": "1", "friends": []}

        query_type = schema.query_type
        assert query_type is not None
        user_field = query_type.fields["user"]
        original_resolve = user_field.resolve
        user_field.resolve = counting_resolver
        try:

            def execute_with_budget(source, budget, variable_values=None):
                document = parse(source)
                assert_cost_within_budget(
                    schema,
                    document,
                    budget,
                    variable_values=variable_values,
                    list_multipliers=MULTIPLIERS,
                )
                return execute_sync(schema, document, variable_values=variable_values)

            query = """
            query($friends: Boolean = true) {
              user { id friends @include(if: $friends) { id } }
            }
            """
            # user(1) + id(1) + friends(1 + id(1)) * 3 = 8
            result = execute_with_budget(query, budget=8)
            assert not result.errors
            assert result.data == {"user": {"id": "1", "friends": []}}
            assert calls == ["user"]

            calls.clear()
            with pytest.raises(QueryCostExceededError):
                execute_with_budget(query, budget=7)
            assert calls == []

            # The same query fits the budget when the expensive part is
            # excluded via variables, and then it does execute.
            result = execute_with_budget(
                query, budget=7, variable_values={"friends": False}
            )
            assert not result.errors
            assert result.data == {"user": {"id": "1"}}
            assert calls == ["user"]
        finally:
            user_field.resolve = original_resolve
