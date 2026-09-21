import pytest

from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLFloat,
    GraphQLInt,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    GraphQLUnionType,
    parse,
)
from graphql.execution import (
    CostAnalysisError,
    CostBudgetExceededError,
    CostFieldContext,
    CostOptions,
    execute_with_cost_budget,
    get_operation_cost,
)


def first_multiplier(context: CostFieldContext) -> int | float | None:
    first = context.args.get("first")
    if isinstance(first, (int, float)) and not isinstance(first, bool):
        return first
    return context.field.extensions.get("listMultiplier")


@pytest.fixture
def schema_and_calls():
    calls: list[str] = []

    def tracked(name: str, value=""):
        def resolver(*_args):
            calls.append(name)
            return value

        return resolver

    cat_type: GraphQLObjectType | None = None
    dog_type: GraphQLObjectType | None = None
    node_type: GraphQLObjectType | None = None
    user_type: GraphQLObjectType | None = None

    pet_type = GraphQLInterfaceType(
        "Pet",
        fields=lambda: {
            "name": GraphQLField(GraphQLString, resolve=tracked("pet.name")),
        },
    )

    cat_type = GraphQLObjectType(
        "Cat",
        interfaces=[pet_type],
        fields=lambda: {
            "name": GraphQLField(GraphQLString, resolve=tracked("cat.name")),
            "lives": GraphQLField(GraphQLInt, resolve=tracked("cat.lives", 9)),
        },
    )
    dog_type = GraphQLObjectType(
        "Dog",
        interfaces=[pet_type],
        fields=lambda: {
            "name": GraphQLField(GraphQLString, resolve=tracked("dog.name")),
            "toys": GraphQLField(
                GraphQLString,
                resolve=tracked("dog.toys"),
                extensions={"cost": 5},
            ),
        },
    )
    search_result_type = GraphQLUnionType("SearchResult", [cat_type, dog_type])

    node_type = GraphQLObjectType(
        "Node",
        fields=lambda: {
            "id": GraphQLField(GraphQLString, resolve=tracked("node.id", "1")),
            "children": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(node_type))),
                args={
                    "first": GraphQLArgument(GraphQLInt, default_value=3),
                },
                resolve=lambda *_args, first=3: [{} for _ in range(first)],
            ),
        },
    )

    def resolve_users(*_args, first=1):
        calls.append("users")
        return [{} for _ in range(first)]

    user_type = GraphQLObjectType(
        "User",
        fields=lambda: {
            "name": GraphQLField(GraphQLString, resolve=tracked("user.name")),
            "friends": GraphQLField(
                GraphQLList(user_type),
                args={
                    "first": GraphQLArgument(GraphQLInt, default_value=2),
                },
                resolve=resolve_users,
            ),
            "pets": GraphQLField(
                GraphQLList(pet_type),
                args={"first": GraphQLArgument(GraphQLInt, default_value=1)},
                resolve=lambda *_args, first=1: [{} for _ in range(first)],
            ),
        },
    )

    query_type = GraphQLObjectType(
        "Query",
        fields=lambda: {
            "me": GraphQLField(user_type, resolve=tracked("me", {})),
            "users": GraphQLField(
                GraphQLList(user_type),
                args={
                    "first": GraphQLArgument(GraphQLInt, default_value=1),
                    "huge": GraphQLArgument(GraphQLFloat),
                    "listMultiplier": GraphQLArgument(GraphQLInt),
                },
                resolve=resolve_users,
            ),
            "pet": GraphQLField(pet_type, resolve=tracked("pet", {})),
            "search": GraphQLField(
                GraphQLList(search_result_type),
                args={"first": GraphQLArgument(GraphQLInt, default_value=1)},
                resolve=lambda *_args, first=1: [{} for _ in range(first)],
            ),
            "node": GraphQLField(node_type, resolve=tracked("node", {})),
        },
    )

    schema = GraphQLSchema(query_type, types=[cat_type, dog_type])
    return schema, calls


OPTIONS = CostOptions(list_multiplier=first_multiplier)


def cost(schema, document, variables=None, operation_name=None, options=OPTIONS):
    return get_operation_cost(schema, document, variables, operation_name, options)


def test_uses_variable_defaults_for_directives_and_list_arguments(
    schema_and_calls,
):
    schema, _calls = schema_and_calls
    document = parse(
        """
        query Defaults($first: Int = 2, $includeName: Boolean = true) {
          users(first: $first) {
            name @include(if: $includeName)
            skipped: name @skip(if: $includeName)
          }
        }
        """
    )

    result = cost(schema, document)

    assert result.total_cost == 3
    users_field = result.object_cost.fields[0]
    assert users_field.list_multiplier == 2
    assert users_field.child_cost == 1


def test_merges_repeated_spreads_but_charges_aliases_separately(schema_and_calls):
    schema, _calls = schema_and_calls
    merged = parse(
        """
        {
          me {
            name
            ...UserName
            ...UserName
          }
        }
        fragment UserName on User { name }
        """
    )
    aliased = parse(
        """
        {
          me {
            name
            secondName: name
          }
        }
        """
    )

    assert cost(schema, merged).total_cost == 2
    assert cost(schema, aliased).total_cost == 3


def test_uses_maximum_cost_of_interface_and_union_branches(schema_and_calls):
    schema, _calls = schema_and_calls
    document = parse(
        """
        {
          pet {
            ... on Dog { name toys }
            ... on Cat { name lives }
          }
          search(first: 10) {
            ... on Dog { name toys }
            ... on Cat { name lives }
          }
        }
        """
    )

    result = cost(schema, document)
    pet_field, search_field = result.object_cost.fields

    assert pet_field.total_cost == 7
    assert [type_cost.total_cost for type_cost in pet_field.type_costs] == [
        2,
        6,
    ]
    assert search_field.total_cost == 61
    assert search_field.child_cost == 6
    assert search_field.multiplied_child_cost == 60
    assert [type_cost.total_cost for type_cost in search_field.type_costs] == [
        2,
        6,
    ]


def test_multiplies_nested_list_dimensions(schema_and_calls):
    schema, _calls = schema_and_calls
    document = parse(
        """
        {
          node {
            children(first: 2) {
              children(first: 4) {
                id
              }
            }
          }
        }
        """
    )

    result = cost(schema, document)
    root_node = result.object_cost.fields[0]
    outer_children = root_node.type_costs[0].object_cost.fields[0]
    inner_children = outer_children.type_costs[0].object_cost.fields[0]

    assert inner_children.total_cost == 5
    assert outer_children.total_cost == 11
    assert root_node.total_cost == 12


def test_selects_named_operations_and_enforces_budget_boundaries(schema_and_calls):
    schema, calls = schema_and_calls
    document = parse(
        """
        query Cheap {
          me { name }
        }
        query Expensive {
          users(first: 10) { name }
        }
        """
    )

    with pytest.raises(CostAnalysisError):
        cost(schema, document)
    assert cost(schema, document, operation_name="Cheap").total_cost == 2

    cheap_cost, result = execute_with_cost_budget(
        schema=schema,
        document=document,
        operation_name="Cheap",
        budget=2,
        cost_options=OPTIONS,
    )
    assert cheap_cost.total_cost == 2
    assert result.errors is None
    assert calls == ["me", "user.name"]

    calls.clear()
    with pytest.raises(CostBudgetExceededError) as exc_info:
        execute_with_cost_budget(
            schema=schema,
            document=document,
            operation_name="Expensive",
            budget=10,
            cost_options=OPTIONS,
        )
    assert exc_info.value.extensions == {
        "cost": 11,
        "budget": 10,
        "code": "COST_BUDGET_EXCEEDED",
    }
    assert calls == []


def test_rejected_budget_runs_no_sibling_or_nested_resolver(schema_and_calls):
    schema, calls = schema_and_calls
    document = parse(
        """
        {
          me { name }
          users(first: 10) { name }
        }
        """
    )

    with pytest.raises(CostBudgetExceededError):
        execute_with_cost_budget(
            schema=schema,
            document=document,
            budget=3,
            cost_options=OPTIONS,
        )

    assert calls == []


def test_rejects_cyclic_fragment_and_illegal_or_huge_multipliers(schema_and_calls):
    schema, _calls = schema_and_calls
    cyclic = parse(
        """
        { me { ...Cat }
        }
        fragment Cat on User { ...Dog }
        fragment Dog on User { ...Cat }
        """
    )
    negative = parse("{ users(first: -1) { name } }")
    huge = parse("{ users(huge: 1e100) { name } }")
    huge_options = CostOptions(list_multiplier=lambda context: context.args["huge"])

    with pytest.raises(CostAnalysisError) as cyclic_error:
        cost(schema, cyclic)
    assert cyclic_error.value.extensions["code"] == "INVALID_OPERATION"

    with pytest.raises(CostAnalysisError, match="non-negative"):
        cost(schema, negative)

    with pytest.raises(CostAnalysisError, match="safe limit"):
        cost(schema, huge, options=huge_options)
