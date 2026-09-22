from pytest import raises

from graphql import (
    CostBudgetExceededError,
    GraphQLError,
    GraphQLArgument,
    GraphQLField,
    GraphQLInterfaceType,
    GraphQLInt,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    GraphQLUnionType,
    analyze_cost,
    execute_sync,
    graphql_sync,
    parse,
)


def make_schema(budget=None, count_resolver=None):
    def resolve_type(obj, _info, _abstract_type):
        return obj["__typename"]

    node_type = GraphQLInterfaceType(
        "Node",
        lambda: {
            "id": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"costWeight": 2},
            )
        },
    )

    human_type = GraphQLObjectType(
        "Human",
        lambda: {
            "id": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"costWeight": 2},
            ),
            "name": GraphQLField(GraphQLString, extensions={"costWeight": 10}),
        },
        interfaces=[node_type],
    )
    droid_type = GraphQLObjectType(
        "Droid",
        lambda: {
            "id": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"costWeight": 2},
            ),
            "model": GraphQLField(GraphQLString, extensions={"costWeight": 7}),
        },
        interfaces=[node_type],
    )
    search_result_type = GraphQLUnionType(
        "SearchResult", [human_type, droid_type], resolve_type
    )

    item_type = GraphQLObjectType(
        "Item",
        {"name": GraphQLField(GraphQLString, extensions={"costWeight": 1})},
    )
    group_type = GraphQLObjectType(
        "Group",
        lambda: {
            "items": GraphQLField(
                GraphQLNonNull(
                    GraphQLList(GraphQLNonNull(item_type))
                ),
                args={
                    "cols": GraphQLArgument(
                        GraphQLInt, default_value=3
                    )
                },
                extensions={"listMultiplier": "cols"},
            )
        },
    )

    def resolve_groups(_root, _info, rows):
        if count_resolver is not None:
            count_resolver(rows)
        return [{"items": [{"name": "item"}]} for _ in range(rows)]

    query_type = GraphQLObjectType(
        "Query",
        {
            "groups": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(group_type))),
                args={"rows": GraphQLArgument(GraphQLInt, default_value=2)},
                resolve=resolve_groups,
                extensions={"listMultiplier": "rows"},
            ),
            "beings": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(node_type))),
                args={"limit": GraphQLArgument(GraphQLInt, default_value=4)},
                resolve=lambda *_: [],
                extensions={"listMultiplier": "limit"},
            ),
            "search": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(search_result_type))),
                args={"limit": GraphQLArgument(GraphQLInt, default_value=4)},
                resolve=lambda *_: [],
                extensions={"listMultiplier": "limit"},
            ),
            "included": GraphQLField(GraphQLString),
            "excluded": GraphQLField(GraphQLString),
        },
    )

    extensions = {"costBudget": budget} if budget is not None else {}
    return GraphQLSchema(query_type, extensions=extensions)


def test_nested_list_multipliers_and_argument_defaults():
    analysis = analyze_cost(
        make_schema(),
        parse("{ groups { items { name } } }"),
    )

    assert analysis.cost == 9
    assert analysis.path_costs == {
        ("groups",): 1,
        ("groups", "items"): 2,
        ("groups", "items", "name"): 6,
    }


def test_same_field_nested_list_multipliers_are_multiplied():
    cell_type = GraphQLObjectType(
        "Cell", {"value": GraphQLField(GraphQLInt, extensions={"costWeight": 1})}
    )
    query_type = GraphQLObjectType(
        "Query",
        {
            "matrix": GraphQLField(
                GraphQLList(GraphQLList(cell_type)),
                args={
                    "rows": GraphQLArgument(GraphQLInt, default_value=2),
                    "cols": GraphQLArgument(GraphQLInt, default_value=3),
                },
                extensions={"listMultipliers": ["rows", "cols"]},
            )
        },
    )

    analysis = analyze_cost(GraphQLSchema(query_type), parse("{ matrix { value } }"))

    assert analysis.cost == 7
    assert analysis.path_costs == {
        ("matrix",): 1,
        ("matrix", "value"): 6,
    }


def test_list_multiplier_uses_coerced_variable_defaults():
    analysis = analyze_cost(
        make_schema(),
        parse(
            "query ($rows: Int = 7) { groups(rows: $rows) { items { name } } }"
        ),
    )

    assert analysis.cost == 29
    assert analysis.path_costs[("groups", "items", "name")] == 21


def test_repeated_fragment_fields_are_merged_but_aliases_are_separate():
    repeated = analyze_cost(
        make_schema(),
        parse(
            """
            query {
              groups(rows: 2) {
                ...GroupFields
                ...GroupFields
              }
            }
            fragment GroupFields on Group {
              items(cols: 3) { name }
            }
            """
        ),
    )
    assert repeated.cost == 9

    aliased = analyze_cost(
        make_schema(),
        parse(
            """
            {
              groups(rows: 2) {
                first: items(cols: 3) { name }
                second: items(cols: 4) { name }
              }
            }
            """
        ),
    )
    assert aliased.cost == 19
    assert aliased.path_costs == {
        ("groups",): 1,
        ("groups", "first"): 2,
        ("groups", "first", "name"): 6,
        ("groups", "second"): 2,
        ("groups", "second", "name"): 8,
    }


def test_skip_and_include_use_coerced_variable_values():
    analysis = analyze_cost(
        make_schema(),
        parse(
            """
            query ($skip: Boolean = true, $include: Boolean = false) {
              included @skip(if: $skip)
              excluded @include(if: $include)
            }
            """
        ),
    )

    assert analysis.cost == 0
    assert analysis.path_costs == {}


def test_interface_branches_use_max_without_adding_branches():
    analysis = analyze_cost(
        make_schema(),
        parse(
            """
            {
              beings(limit: 4) {
                id
                ... on Human { name }
                ... on Droid { model }
              }
            }
            """
        ),
    )

    assert analysis.cost == 49
    assert analysis.path_costs == {
        ("beings",): 1,
        ("beings", "id"): 8,
        ("beings", "name"): 40,
        ("beings", "model"): 28,
    }
    assert analysis.max_cost_path == ("beings", "name")


def test_union_branches_use_max_without_adding_branches():
    analysis = analyze_cost(
        make_schema(),
        parse(
            """
            {
              search(limit: 4) {
                ... on Human { id name }
                ... on Droid { id model }
              }
            }
            """
        ),
    )

    assert analysis.cost == 49
    assert analysis.path_costs[("search", "name")] == 40
    assert analysis.path_costs[("search", "model")] == 28


def test_budget_exceeded_error_identifies_largest_path():
    document = parse("{ groups { items { name } } }")
    with raises(CostBudgetExceededError) as exc_info:
        from graphql import assert_cost_within_budget

        assert_cost_within_budget(make_schema(), document, 8)

    error = exc_info.value
    assert error.extensions["budget"] == 8
    assert error.extensions["cost"] == 9
    assert error.extensions["max_cost_path"] == ["groups", "items", "name"]
    assert error.formatted["extensions"]["path_costs"] == {
        "groups": 1,
        "groups.items": 2,
        "groups.items.name": 6,
    }


def test_schema_budget_blocks_execution_before_any_resolver():
    calls = []
    result = graphql_sync(
        make_schema(8, calls.append),
        "{ groups { items { name } } }",
    )

    assert result.data is None
    assert calls == []
    error = result.errors[0]
    assert error.extensions["budget"] == 8
    assert error.extensions["cost"] == 9
    assert error.extensions["max_cost_path"] == ["groups", "items", "name"]


def test_explicit_execution_budget_blocks_resolver():
    calls = []
    result = execute_sync(
        make_schema(None, calls.append),
        parse("{ groups { items { name } } }"),
        cost_budget=8,
    )

    assert result.data is None
    assert calls == []
    assert result.errors[0].extensions["cost"] == 9


def test_missing_list_multiplier_is_rejected():
    item_type = GraphQLObjectType("Item", {"name": GraphQLField(GraphQLString)})
    query_type = GraphQLObjectType(
        "Query", {"items": GraphQLField(GraphQLList(item_type))}
    )
    schema = GraphQLSchema(query_type)

    with raises(GraphQLError, match="list fields require a list multiplier"):
        analyze_cost(schema, parse("{ items { name } }"))


def test_null_list_multiplier_argument_is_rejected():
    with raises(GraphQLError, match="non-negative integers"):
        analyze_cost(make_schema(), parse("{ groups(rows: null) { items { name } } }"))


def test_null_field_weight_is_rejected():
    item_type = GraphQLObjectType(
        "Item",
        {"name": GraphQLField(GraphQLString, extensions={"costWeight": None})},
    )
    query_type = GraphQLObjectType(
        "Query",
        {
            "items": GraphQLField(
                GraphQLList(item_type), extensions={"listMultiplier": 1}
            )
        },
    )

    with raises(GraphQLError, match="non-negative integers"):
        analyze_cost(GraphQLSchema(query_type), parse("{ items { name } }"))
