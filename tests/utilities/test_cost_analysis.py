"""Tests for the pre-execution query cost analysis."""
from pytest import raises

from graphql import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLError,
    GraphQLField,
    GraphQLInt,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    GraphQLUnionType,
    execute,
    execute_sync,
    graphql_sync,
    parse,
)
from graphql.utilities import (
    CostAnalysisError,
    CostBudgetExceededError,
    CostAnalysisResult,
    assert_within_cost_budget,
    get_operation_cost,
)


def build_test_schema():
    """Build a schema exercising abstract types, nested lists and arguments.

    Field weights:

    Query.single       weight 1
    Query.object       weight 2
    Query.items        weight 3, listMultiplier "first" (default 2)
    Query.matrix       weight 1, listMultiplier "first" (default 2),
                       return type [[Item!]!]!
    Query.constList    weight 1, listMultiplier constant 4
    Object.name        weight 4
    Object.pets        weight 5, listMultiplier "first" (default 3)
    Object.items       weight 0, listMultiplier "first" (default 2)
    Dog/Cat.name       weight 6
    Dog.heavy          weight 9
    Cat.heavy          weight 2
    Item.value         weight 7
    """
    item_type = GraphQLObjectType(
        "Item",
        {
            "value": GraphQLField(
                GraphQLInt, extensions={"cost": {"weight": 7}}
            )
        },
    )

    pet_interface = GraphQLInterfaceType(
        "Pet",
        lambda: {
            "name": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"cost": {"weight": 6}},
            )
        },
    )

    dog_type = GraphQLObjectType(
        "Dog",
        lambda: {
            "name": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"cost": {"weight": 6}},
            ),
            "heavy": GraphQLField(
                GraphQLString, extensions={"cost": {"weight": 9}}
            ),
            "toy": GraphQLField(GraphQLString),
        },
        interfaces=[pet_interface],
    )
    cat_type = GraphQLObjectType(
        "Cat",
        lambda: {
            "name": GraphQLField(
                GraphQLNonNull(GraphQLString),
                extensions={"cost": {"weight": 6}},
            ),
            "heavy": GraphQLField(
                GraphQLString, extensions={"cost": {"weight": 2}}
            ),
        },
        interfaces=[pet_interface],
    )

    object_type = GraphQLObjectType(
        "Object",
        lambda: {
            "name": GraphQLField(
                GraphQLString, extensions={"cost": {"weight": 4}}
            ),
            "pets": GraphQLField(
                GraphQLNonNull(
                    GraphQLList(GraphQLNonNull(pet_interface))
                ),
                args={
                    "first": GraphQLArgument(
                        GraphQLInt, default_value=3
                    )
                },
                extensions={
                    "cost": {"weight": 5, "listMultiplier": "first"}
                },
            ),
            "items": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(item_type))),
                args={
                    "first": GraphQLArgument(
                        GraphQLInt, default_value=2
                    )
                },
                extensions={"cost": {"listMultiplier": "first"}},
            ),
        },
    )

    query_type = GraphQLObjectType(
        "Query",
        {
            "single": GraphQLField(
                GraphQLString, extensions={"cost": {"weight": 1}}
            ),
            "object": GraphQLField(
                object_type, extensions={"cost": {"weight": 2}}
            ),
            "items": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(item_type))),
                args={
                    "first": GraphQLArgument(
                        GraphQLInt, default_value=2
                    )
                },
                extensions={
                    "cost": {"weight": 3, "listMultiplier": "first"}
                },
            ),
            "matrix": GraphQLField(
                GraphQLNonNull(
                    GraphQLList(
                        GraphQLNonNull(
                            GraphQLList(GraphQLNonNull(item_type))
                        )
                    )
                ),
                args={
                    "first": GraphQLArgument(
                        GraphQLInt, default_value=2
                    )
                },
                extensions={
                    "cost": {"weight": 1, "listMultiplier": "first"}
                },
            ),
            "constList": GraphQLField(
                GraphQLNonNull(GraphQLList(GraphQLNonNull(item_type))),
                extensions={
                    "cost": {"weight": 1, "listMultiplier": 4}
                },
            ),
        },
    )

    return GraphQLSchema(query=query_type, types=[dog_type, cat_type])


def cost_map(result):
    return {
        ".".join(path): value for path, value in result.contributions.items()
    }
def describe_basic_costs():
    def test_single_scalar_field():
        schema = build_test_schema()
        result = get_operation_cost(schema, parse('{ single }'))
        assert result == CostAnalysisResult(1, {('single',): 1})
        assert cost_map(result) == {'single': 1}

    def test_object_subtree_without_lists():
        schema = build_test_schema()
        result = get_operation_cost(
            schema, parse('{ object { name } }')
        )
        assert result.total_cost == 6
        assert cost_map(result) == {'object': 2, 'object.name': 4}

    def test_constant_list_multiplier():
        schema = build_test_schema()
        result = get_operation_cost(
            schema, parse('{ constList { value } }')
        )
        assert result.total_cost == 1 + 4 * 7
        assert cost_map(result) == {
            'constList': 1,
            'constList.value': 28,
        }

    def test_list_multiplier_from_argument_and_default():
        schema = build_test_schema()
        defaulted = get_operation_cost(
            schema, parse('{ items { value } }')
        )
        assert defaulted.total_cost == 3 + 2 * 7
        assert cost_map(defaulted) == {
            'items': 3,
            'items.value': 14,
        }

        explicit = get_operation_cost(
            schema, parse('{ items(first: 5) { value } }')
        )
        assert explicit.total_cost == 3 + 5 * 7
        assert cost_map(explicit) == {
            'items': 3,
            'items.value': 35,
        }

    def test_list_multiplier_from_coerced_variable():
        schema = build_test_schema()
        document = parse(
            'query Q($n: Int = 4) { items(first: $n) { value } }'
        )
        defaulted = get_operation_cost(schema, document, {})
        assert defaulted.total_cost == 3 + 4 * 7
        assert cost_map(defaulted)['items.value'] == 28

        supplied = get_operation_cost(schema, document, {'n': 10})
        assert supplied.total_cost == 3 + 10 * 7
        assert cost_map(supplied)['items.value'] == 70

    def test_nested_list_multiplier_applies_per_layer():
        schema = build_test_schema()
        defaulted = get_operation_cost(
            schema, parse('{ matrix { value } }')
        )
        assert defaulted.total_cost == 1 + 2 * 2 * 7
        assert cost_map(defaulted) == {
            'matrix': 1,
            'matrix.value': 28,
        }

        explicit = get_operation_cost(
            schema, parse('{ matrix(first: 3) { value } }')
        )
        assert explicit.total_cost == 1 + 3 * 3 * 7
        assert cost_map(explicit) == {
            'matrix': 1,
            'matrix.value': 63,
        }

    def test_zero_multiplier_zeroes_subtree_cost():
        schema = build_test_schema()
        result = get_operation_cost(
            schema, parse('{ items(first: 0) { value } }')
        )
        assert result.total_cost == 3
        assert cost_map(result) == {'items': 3, 'items.value': 0}

    def test_contributions_always_add_up_to_total():
        schema = build_test_schema()
        document = parse(
            '{ items(first: 2) { value } object {'
            ' items(first: 3) { value } } }'
        )
        result = get_operation_cost(schema, document)
        assert sum(result.contributions.values()) == result.total_cost
        assert result.total_cost == 3 + 2 * 7 + 2 + 3 * 7
        assert cost_map(result) == {
            'items': 3,
            'items.value': 14,
            'object': 2,
            'object.items': 0,
            'object.items.value': 21,
        }
def describe_field_merging():
    def test_repeated_fragment_spread_is_counted_once():
        schema = build_test_schema()
        document = parse(
            'fragment F on Object { name }'
            ' { object { ...F ...F } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 6
        assert cost_map(result) == {'object': 2, 'object.name': 4}

    def test_fields_shared_by_fragments_merge():
        schema = build_test_schema()
        document = parse(
            'fragment A on Object { name }'
            ' fragment B on Object { name }'
            ' { object { ...A ...B } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 6
        assert cost_map(result) == {'object': 2, 'object.name': 4}

    def test_inline_fragments_on_same_type_merge():
        schema = build_test_schema()
        document = parse(
            '{ object { ... on Object { name }'
            ' ... on Object { name } } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 6
        assert cost_map(result) == {'object': 2, 'object.name': 4}

    def test_different_aliases_are_counted_twice():
        schema = build_test_schema()
        document = parse('{ object { a: name b: name } }')
        result = get_operation_cost(schema, document)
        assert result.total_cost == 2 + 4 + 4
        assert cost_map(result) == {
            'object': 2,
            'object.a': 4,
            'object.b': 4,
        }

    def test_aliased_list_fields_use_their_own_arguments():
        schema = build_test_schema()
        document = parse(
            '{ items(first: 1) { value }'
            ' more: items(first: 5) { value } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == (3 + 7) + (3 + 35)
        assert cost_map(result) == {
            'items': 3,
            'items.value': 7,
            'more': 3,
            'more.value': 35,
        }

    def test_merged_fields_through_nested_fragments_with_lists():
        schema = build_test_schema()
        document = parse(
            'fragment PetName on Pet { name }'
            ' fragment DogStuff on Dog { ...PetName heavy }'
            ' { object { pets(first: 2) { ...PetName'
            ' ... on Dog { ...DogStuff } } } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 2 + 5 + 2 * (6 + 9)
        assert cost_map(result) == {
            'object': 2,
            'object.pets': 5,
            'object.pets.name': 12,
            'object.pets.heavy': 18,
        }
def describe_abstract_types():
    def test_max_branch_is_chosen_not_sum():
        schema = build_test_schema()
        document = parse(
            '{ object { pets(first: 2) { name'
            ' ... on Dog { heavy toy }'
            ' ... on Cat { heavy } } } }'
        )
        result = get_operation_cost(schema, document)
        # Dog: name 6 + heavy 9 + toy 0 = 15; Cat: 6 + 2 = 8
        assert result.total_cost == 2 + 5 + 2 * 15
        assert cost_map(result) == {
            'object': 2,
            'object.pets': 5,
            'object.pets.name': 12,
            'object.pets.heavy': 18,
            'object.pets.toy': 0,
        }

    def test_common_fields_counted_once():
        schema = build_test_schema()
        document = parse(
            '{ object { pets(first: 1) { name'
            ' ... on Dog { name heavy }'
            ' ... on Cat { name heavy } } } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 2 + 5 + 1 * 15
        assert cost_map(result) == {
            'object': 2,
            'object.pets': 5,
            'object.pets.name': 6,
            'object.pets.heavy': 9,
        }

    def test_fragment_on_interface_is_common_to_all_branches():
        schema = build_test_schema()
        document = parse(
            'fragment P on Pet { name }'
            ' { object { pets(first: 3) { ...P'
            ' ... on Dog { heavy } } } }'
        )
        result = get_operation_cost(schema, document)
        assert result.total_cost == 2 + 5 + 3 * 15
        assert cost_map(result) == {
            'object': 2,
            'object.pets': 5,
            'object.pets.name': 18,
            'object.pets.heavy': 27,
        }

    def test_union_branches_also_use_max():
        schema = build_test_schema()
        dog_type = schema.get_type('Dog')
        cat_type = schema.get_type('Cat')
        mammal = GraphQLUnionType('Mammal', [dog_type, cat_type])
        query_type = GraphQLObjectType(
            'UnionQuery',
            {
                'pets': GraphQLField(
                    GraphQLList(mammal),
                    args={
                        'first': GraphQLArgument(
                            GraphQLInt, default_value=2
                        )
                    },
                    extensions={
                        'cost': {'weight': 5, 'listMultiplier': 'first'}
                    },
                )
            },
        )
        union_schema = GraphQLSchema(
            query=query_type, types=[dog_type, cat_type]
        )
        document = parse(
            '{ pets(first: 4) { ... on Dog { name heavy }'
            ' ... on Cat { name heavy } } }'
        )
        result = get_operation_cost(union_schema, document)
        assert result.total_cost == 5 + 4 * 15
        assert cost_map(result) == {
            'pets': 5,
            'pets.name': 24,
            'pets.heavy': 36,
        }
    def test_unweighted_fields_default_to_zero():
        schema = build_test_schema()
        result = get_operation_cost(
            schema, parse('{ object { pets(first: 2) { ... on Dog { toy } } } }')
        )
        # toy and the unweighted branch fields contribute nothing
        assert result.total_cost == 2 + 5
        assert cost_map(result) == {
            'object': 2,
            'object.pets': 5,
            'object.pets.toy': 0,
        }

    def test_abstract_branch_contributions_sum_to_branch_total():
        schema = build_test_schema()
        document = parse(
            '{ object { pets(first: 2) { name'
            ' ... on Dog { heavy } ... on Cat { heavy } } } }'
        )
        result = get_operation_cost(schema, document)
        # Dog wins with 15; contributions must reflect exactly that branch
        assert result.total_cost == 7 + 2 * 15
        subtree = {
            path: cost
            for path, cost in result.contributions.items()
            if path[:2] == ('object', 'pets')
        }
        assert sum(subtree.values()) == 5 + 2 * 15
def describe_skip_include_directives():
    def test_include_uses_coerced_variable_default():
        schema = build_test_schema()
        document = parse(
            'query Q($withName: Boolean! = true) { object { name'
            ' @include(if: $withName) } }'
        )
        included = get_operation_cost(schema, document, {})
        assert included.total_cost == 6
        assert cost_map(included) == {
            'object': 2,
            'object.name': 4,
        }

        excluded = get_operation_cost(
            schema, document, {'withName': False}
        )
        assert excluded.total_cost == 2
        assert cost_map(excluded) == {'object': 2}

    def test_skip_uses_coerced_variable_value():
        schema = build_test_schema()
        document = parse(
            'query Q($skipItems: Boolean! = false) { items(first: 10)'
            ' @skip(if: $skipItems) { value } }'
        )
        not_skipped = get_operation_cost(schema, document, {})
        assert not_skipped.total_cost == 3 + 10 * 7

        skipped = get_operation_cost(
            schema, document, {'skipItems': True}
        )
        assert skipped.total_cost == 0
        assert skipped.contributions == {}

    def test_directives_on_fragment_spreads():
        schema = build_test_schema()
        document = parse(
            'fragment F on Object { name }'
            ' query Q($expand: Boolean! = false) { object { ...F'
            ' @include(if: $expand) } }'
        )
        collapsed = get_operation_cost(schema, document, {})
        assert collapsed.total_cost == 2
        expanded = get_operation_cost(
            schema, document, {'expand': True}
        )
        assert expanded.total_cost == 6

    def test_literal_directive_values():
        schema = build_test_schema()
        document = parse('{ object { name @skip(if: true) } }')
        result = get_operation_cost(schema, document)
        assert result.total_cost == 2
def describe_budget_enforcement():
    def test_within_budget_returns_result():
        schema = build_test_schema()
        document = parse('{ single }')
        result = assert_within_cost_budget(schema, document, 1)
        assert result.total_cost == 1

    def test_boundary_value_is_allowed():
        schema = build_test_schema()
        document = parse('{ single }')
        result = assert_within_cost_budget(schema, document, 1)
        assert result.total_cost == 1
        with raises(CostBudgetExceededError):
            assert_within_cost_budget(schema, document, 0)

    def test_exceeding_budget_raises_with_details():
        schema = build_test_schema()
        document = parse('{ items(first: 5) { value } single }')
        with raises(CostBudgetExceededError) as exc_info:
            assert_within_cost_budget(schema, document, 10)
        error = exc_info.value
        assert error.budget == 10
        assert error.total_cost == 39
        assert error.max_path == ('items', 'value')
        assert error.max_path_cost == 35
        assert error.contributions[('items', 'value')] == 35
        assert '10' in error.message
        assert '39' in error.message
        assert 'items.value' in error.message
        assert error.extensions['costAnalysis'] == {
            'budget': 10,
            'totalCost': 39,
            'maxPath': ['items', 'value'],
            'maxPathCost': 35,
            'contributions': {
                'items': 3,
                'items.value': 35,
                'single': 1,
            },
        }
        assert error.nodes
        assert error.nodes[0].name.value == 'items'

    def test_budget_tie_prefers_path_close_to_root():
        schema = build_test_schema()
        document = parse('{ object { name } single }')
        with raises(CostBudgetExceededError) as exc_info:
            assert_within_cost_budget(schema, document, 2)
        error = exc_info.value
        assert error.total_cost == 7
        assert error.max_path == ('object', 'name')
        assert error.max_path_cost == 4

    def test_cost_error_is_a_graphql_error():
        schema = build_test_schema()
        with raises(GraphQLError):
            assert_within_cost_budget(
                schema, parse('{ single }'), 0
            )

    def test_operation_name_selects_the_operation():
        schema = build_test_schema()
        document = parse(
            'query A { single }'
            ' query B { object { name } }'
        )
        result = get_operation_cost(schema, document, {}, 'B')
        assert result.total_cost == 6
        with raises(CostAnalysisError):
            get_operation_cost(schema, document, {}, 'Unknown')
        with raises(CostAnalysisError):
            get_operation_cost(schema, document)
def describe_invalid_configuration():
    def test_list_field_without_multiplier_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    extensions={'cost': {'weight': 1}},
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError) as exc_info:
            get_operation_cost(schema, parse('{ things { value } }'))
        assert 'listMultiplier' in exc_info.value.message

    def test_missing_multiplier_is_not_reported_for_scalar_field():
        query_type = GraphQLObjectType(
            'Query',
            {
                'thing': GraphQLField(
                    GraphQLInt, extensions={'cost': {'weight': 2}}
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        result = get_operation_cost(schema, parse('{ thing }'))
        assert result.total_cost == 2

    def test_negative_constant_multiplier_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    extensions={
                        'cost': {'weight': 1, 'listMultiplier': -1}
                    },
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError):
            get_operation_cost(schema, parse('{ things { value } }'))

    def test_multiplier_referencing_unknown_argument_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    extensions={
                        'cost': {'listMultiplier': 'limit'}
                    },
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError) as exc_info:
            get_operation_cost(schema, parse('{ things { value } }'))
        assert 'unknown argument' in exc_info.value.message

    def test_non_integer_multiplier_argument_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    args={
                        'first': GraphQLArgument(GraphQLString, 'x')
                    },
                    extensions={
                        'cost': {'listMultiplier': 'first'}
                    },
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError) as exc_info:
            get_operation_cost(
                schema, parse('{ things(first: "x") { value } }')
            )
        assert 'non-negative integer' in exc_info.value.message

    def test_invalid_multiplier_type_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    extensions={
                        'cost': {'listMultiplier': ['first']}
                    },
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError):
            get_operation_cost(schema, parse('{ things { value } }'))

    def test_boolean_multiplier_is_rejected():
        item_type = GraphQLObjectType(
            'Item', {'value': GraphQLField(GraphQLInt)}
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'things': GraphQLField(
                    GraphQLList(item_type),
                    extensions={
                        'cost': {'listMultiplier': True}
                    },
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError):
            get_operation_cost(schema, parse('{ things { value } }'))

    def test_invalid_weight_is_rejected():
        query_type = GraphQLObjectType(
            'Query',
            {
                'thing': GraphQLField(
                    GraphQLString, extensions={'cost': {'weight': -2}}
                )
            },
        )
        schema = GraphQLSchema(query=query_type)
        with raises(CostAnalysisError):
            get_operation_cost(schema, parse('{ thing }'))
def describe_execution_integration():
    def _build_executable_schema():
        calls = []

        def resolve_single(_obj, _info):
            calls.append('single')
            return 'value'

        item_type = GraphQLObjectType(
            'Item',
            {
                'value': GraphQLField(
                    GraphQLInt,
                    resolve=lambda _obj, _info: (
                        calls.append('value') or 7
                    ),
                    extensions={'cost': {'weight': 7}},
                )
            },
        )
        query_type = GraphQLObjectType(
            'Query',
            {
                'single': GraphQLField(
                    GraphQLString,
                    resolve=resolve_single,
                    extensions={'cost': {'weight': 1}},
                ),
                'items': GraphQLField(
                    GraphQLNonNull(GraphQLList(GraphQLNonNull(item_type))),
                    args={
                        'first': GraphQLArgument(
                            GraphQLInt, default_value=2
                        )
                    },
                    resolve=lambda _obj, _info, first: (
                        calls.append('items') or [{} for _ in range(first)]
                    ),
                    extensions={
                        'cost': {'weight': 3, 'listMultiplier': 'first'}
                    },
                ),
            },
        )
        return GraphQLSchema(query=query_type), calls

    def test_execute_sync_blocks_resolvers_when_over_budget():
        schema, calls = _build_executable_schema()
        document = parse('{ items(first: 5) { value } }')
        result = execute_sync(schema, document, max_cost=10)
        assert result.data is None
        assert result.errors
        assert isinstance(result.errors[0], CostBudgetExceededError)
        assert calls == []

    def test_execute_sync_runs_resolvers_when_within_budget():
        schema, calls = _build_executable_schema()
        document = parse('{ items(first: 1) { value } single }')
        result = execute_sync(schema, document, max_cost=100)
        assert result.errors is None
        assert result.data == {
            'items': [{'value': 7}],
            'single': 'value',
        }
        assert sorted(calls) == ['items', 'single', 'value']

    def test_graphql_sync_blocks_resolvers_when_over_budget():
        schema, calls = _build_executable_schema()
        result = graphql_sync(
            schema,
            '{ items(first: 9) { value } }',
            max_cost=10,
        )
        assert result.data is None
        assert result.errors
        assert isinstance(result.errors[0], CostBudgetExceededError)
        assert calls == []

    def test_validation_still_runs_before_cost_check():
        schema, calls = _build_executable_schema()
        # 'unknownField' is invalid; validation must fail first and the
        # invalid cost configuration on other fields must be irrelevant.
        result = graphql_sync(
            schema,
            '{ unknownField }',
            max_cost=0,
        )
        assert result.data is None
        assert result.errors
        assert not isinstance(
            result.errors[0], CostBudgetExceededError
        )
        assert calls == []

    def test_execute_returns_cost_error_without_running_resolvers():
        schema, calls = _build_executable_schema()
        document = parse('{ single }')
        result = execute(schema, document, max_cost=0)
        assert result.errors
        assert isinstance(result.errors[0], CostBudgetExceededError)
        assert calls == []
