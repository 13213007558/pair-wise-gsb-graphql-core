"""Pre-execution query cost analysis.

This module implements a static cost check that is run after the usual
validation phase but before any field resolvers are called. It never executes
user-provided resolvers.

Cost information is taken from the extensions of schema elements:

- Field weights are configured as extensions={'cost': {'weight': 1}} on a
  GraphQLField. Fields without an explicit weight contribute 0.
- List multipliers are configured as
  extensions={'cost': {'listMultiplier': 'first'}} on a field whose type is a
  list. The value is either a non-negative integer constant or the name of an
  argument of the field, in which case the coerced argument value (including
  argument default values) is used. A list field without a valid explicitly
  configured multiplier is rejected.

The analysis follows GraphQL execution semantics:

- Fields with the same response name are merged, including fields brought in
  by fragments that are spread repeatedly. Different aliases are counted
  independently.
- @skip and @include are evaluated using the coerced variable values
  (including variable default values).
- For Interface and Union fields, the cost is the maximum cost over all
  possible runtime object types; common fields are counted once, not once per
  concrete type.
"""
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from ..error import GraphQLError
from ..execution.collect_fields import (
    collect_fields_impl,
    get_field_entry_key,
)
from ..execution.values import get_argument_values, get_variable_values
from ..language import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    SelectionSetNode,
)
from ..type import (
    GraphQLAbstractType,
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    is_abstract_type,
)
from ..type import get_named_type, is_list_type, is_non_null_type

__all__ = [
    "CostAnalysisError",
    "CostBudgetExceededError",
    "CostAnalysisResult",
    "get_operation_cost",
    "cost_analysis",
    "assert_within_cost_budget",
    "check_cost_budget",
]


CostPath = Tuple[str, ...]


class CostAnalysisError(GraphQLError):
    """Invalid cost configuration or analyzer usage.

    Raised for example when a list field does not declare an explicit
    multiplier or when a multiplier does not resolve to a non-negative
    integer.
    """


class CostBudgetExceededError(GraphQLError):
    """An operation exceeds the configured cost budget.

    Attributes:
        budget: the configured maximum allowed cost.
        total_cost: the cost computed for the operation.
        max_path: the response path with the largest cost contribution.
        max_path_cost: the cost contributed by ``max_path``.
        contributions: the cost contribution of every response path. The
            contributions add up exactly to ``total_cost``.
    """

    budget: int
    total_cost: int
    max_path: CostPath
    max_path_cost: int
    contributions: Dict[CostPath, int]

    def __init__(
        self,
        budget: int,
        total_cost: int,
        max_path: CostPath,
        max_path_cost: int,
        contributions: Dict[CostPath, int],
        nodes: Optional[Sequence[FieldNode]] = None,
    ) -> None:
        self.budget = budget
        self.total_cost = total_cost
        self.max_path = max_path
        self.max_path_cost = max_path_cost
        self.contributions = contributions
        path_string = ".".join(max_path)
        super().__init__(
            f"Query cost of {total_cost} exceeds the budget of {budget}. "
            f"The largest contributing response path is '{path_string}' "
            f"with a cost of {max_path_cost}.",
            nodes=list(nodes) if nodes else None,
            extensions={
                "costAnalysis": {
                    "budget": budget,
                    "totalCost": total_cost,
                    "maxPath": list(max_path),
                    "maxPathCost": max_path_cost,
                    "contributions": {
                        ".".join(cpath): ccost
                        for cpath, ccost in contributions.items()
                    },
                }
            },
        )

class CostAnalysisResult:
    """Result of a static cost analysis.

    Attributes:
        total_cost: total cost of the operation.
        contributions: mapping from every response path to the cost incurred
            at that path (its field weight multiplied by all ancestor list
            multipliers). The values add up exactly to ``total_cost``.
    """

    __slots__ = "total_cost", "contributions"

    total_cost: int
    contributions: Dict[CostPath, int]

    def __init__(
        self, total_cost: int, contributions: Dict[CostPath, int]
    ) -> None:
        self.total_cost = total_cost
        self.contributions = contributions

    def __repr__(self) -> str:
        return (
            f"CostAnalysisResult(total_cost={self.total_cost}, "
            f"contributions={self.contributions!r})"
        )

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, CostAnalysisResult)
            and other.total_cost == self.total_cost
            and other.contributions == self.contributions
        )


def get_operation_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    variable_values: Optional[Dict[str, Any]] = None,
    operation_name: Optional[str] = None,
) -> CostAnalysisResult:
    """Compute the cost of an operation without executing any resolvers.

    The document must already have passed the usual GraphQL validation.
    This is a static analysis and never calls user-provided resolvers.

    Variable values are coerced according to the variable definitions of
    the selected operation, so variable default values are taken into
    account when evaluating @skip/@include and multiplier arguments.

    Raises :class:`CostAnalysisError` for invalid cost configuration, such
    as list fields without an explicitly configured multiplier.
    """
    operation, fragments = _get_operation_and_fragments(
        document, operation_name
    )

    coerced_variable_values = get_variable_values(
        schema, operation.variable_definitions or (), variable_values or {}
    )
    if isinstance(coerced_variable_values, list):  # pragma: no cover
        raise coerced_variable_values[0]

    root_type = schema.get_root_type(operation.operation)
    if root_type is None:
        raise CostAnalysisError(
            "Schema is not configured to execute"
            f" {operation.operation.value} operation.",
            operation,
        )

    contributions: Dict[CostPath, int] = {}
    total_cost = _selection_cost(
        schema,
        fragments,
        coerced_variable_values,
        root_type,
        operation.selection_set,
        (),
        1,
        contributions,
    )
    return CostAnalysisResult(total_cost, contributions)


# Alias matching the naming style of other utilities.
cost_analysis = get_operation_cost


def _get_operation_and_fragments(
    document: DocumentNode, operation_name: Optional[str]
) -> Tuple[OperationDefinitionNode, Dict[str, FragmentDefinitionNode]]:
    operations: List[OperationDefinitionNode] = []
    fragments: Dict[str, FragmentDefinitionNode] = {}
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            operations.append(definition)
        elif isinstance(definition, FragmentDefinitionNode):
            fragments[definition.name.value] = definition

    operation: Optional[OperationDefinitionNode] = None
    if operation_name is not None:
        for candidate in operations:
            if candidate.name and candidate.name.value == operation_name:
                operation = candidate
                break
        if operation is None:
            raise CostAnalysisError(
                f"Unknown operation named '{operation_name}'."
            )
    elif len(operations) == 1:
        operation = operations[0]
    elif not operations:
        raise CostAnalysisError("Must provide an operation.")
    else:
        raise CostAnalysisError(
            "Must provide operation name if query contains multiple operations."
        )
    return operation, fragments


def _selection_cost(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    parent_type: GraphQLObjectType,
    selection_set: SelectionSetNode,
    path: CostPath,
    parent_multiplier: int,
    contributions: Dict[CostPath, int],
) -> int:
    """Collect the merged fields of a selection set and sum their costs."""
    fields: Dict[str, List[FieldNode]] = {}
    collect_fields_impl(
        schema,
        fragments,
        variable_values,
        parent_type,
        selection_set,
        fields,
        set(),
    )

    total = 0
    for response_name, field_nodes in fields.items():
        field_path = path + (response_name,)
        total += _field_cost(
            schema,
            fragments,
            variable_values,
            parent_type,
            field_nodes,
            field_path,
            parent_multiplier,
            contributions,
        )
    return total


def _field_cost(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    parent_type: GraphQLObjectType,
    field_nodes: List[FieldNode],
    path: CostPath,
    parent_multiplier: int,
    contributions: Dict[CostPath, int],
) -> int:
    field_def = _get_field_def(schema, parent_type, field_nodes[0])
    cost_config = field_def.extensions.get("cost", {})
    field_name = get_field_entry_key(field_nodes[0])
    if not isinstance(cost_config, dict):
        raise CostAnalysisError(
            f"The 'cost' extension of field '{field_name}' must be a"
            " dictionary.",
            field_nodes,
        )

    weight = _get_weight(field_name, cost_config, field_nodes)
    own_cost = parent_multiplier * weight
    contributions[path] = contributions.get(path, 0) + own_cost

    selection_set = _merged_selection_set(field_nodes)
    if selection_set is None:
        return own_cost

    multiplier, list_depth = _get_list_multiplier(
        field_name, field_def, cost_config, field_nodes, variable_values
    )
    # The configured multiplier applies once per list layer.
    subtree_multiplier = parent_multiplier * multiplier ** list_depth
    named_type = cast(GraphQLObjectType, get_named_type(field_def.type))

    if is_abstract_type(named_type):
        subtree_cost = _abstract_selection_cost(
            schema,
            fragments,
            variable_values,
            cast(GraphQLAbstractType, named_type),
            selection_set,
            path,
            subtree_multiplier,
            contributions,
        )
    else:
        subtree_cost = _selection_cost(
            schema,
            fragments,
            variable_values,
            named_type,
            selection_set,
            path,
            subtree_multiplier,
            contributions,
        )
    return own_cost + subtree_cost


def _abstract_selection_cost(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    abstract_type: GraphQLAbstractType,
    selection_set: SelectionSetNode,
    path: CostPath,
    parent_multiplier: int,
    contributions: Dict[CostPath, int],
) -> int:
    """Return the maximum selection cost over the possible runtime types.

    Only the cost of the most expensive possible branch is counted; the
    branches are never summed. The contributions of the winning branch are
    the ones merged into the outer result. Common fields are collected once
    by the field merging rules.
    """
    possible_types = schema.get_possible_types(abstract_type)
    best_cost: Optional[int] = None
    best_contributions: Optional[Dict[CostPath, int]] = None
    for object_type in possible_types:
        branch_contributions: Dict[CostPath, int] = {}
        branch_cost = _selection_cost(
            schema,
            fragments,
            variable_values,
            object_type,
            selection_set,
            path,
            parent_multiplier,
            branch_contributions,
        )
        if best_cost is None or branch_cost > best_cost:
            best_cost = branch_cost
            best_contributions = branch_contributions
    assert best_contributions is not None
    for branch_path, branch_cost in best_contributions.items():
        contributions[branch_path] = (
            contributions.get(branch_path, 0) + branch_cost
        )
    return best_cost or 0


def _get_weight(
    field_name: str,
    cost_config: Dict[str, Any],
    field_nodes: List[FieldNode],
) -> int:
    if "weight" not in cost_config:
        return 0
    weight = cost_config["weight"]
    if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
        raise CostAnalysisError(
            f"Invalid cost weight for field '{field_name}': expected a"
            f" non-negative integer, got {weight!r}.",
            field_nodes,
        )
    return weight


def _get_list_multiplier(
    field_name: str,
    field_def: GraphQLField,
    cost_config: Dict[str, Any],
    field_nodes: List[FieldNode],
    variable_values: Dict[str, Any],
) -> Tuple[int, int]:
    """Resolve the list multiplier and the number of list layers.

    The multiplier must be configured explicitly for list fields and
    resolve to a non-negative integer.
    """
    list_depth = _list_depth(field_def.type)
    if list_depth == 0:
        return 1, 0

    if "listMultiplier" not in cost_config:
        raise CostAnalysisError(
            f"Field '{field_name}' has a list return type but does not"
            " configure a 'listMultiplier' in its cost extensions;"
            " configure an explicit non-negative integer or an argument"
            " name.",
            field_nodes,
        )

    configured = cost_config["listMultiplier"]
    if isinstance(configured, bool):
        raise _invalid_multiplier_error(field_name, configured, field_nodes)
    if isinstance(configured, int):
        if configured < 0:
            raise _invalid_multiplier_error(
                field_name, configured, field_nodes
            )
        return configured, list_depth
    if not isinstance(configured, str) or not configured:
        raise _invalid_multiplier_error(field_name, configured, field_nodes)

    argument_name = configured
    arg_def = field_def.args.get(argument_name)
    if arg_def is None:
        raise CostAnalysisError(
            f"The list multiplier of field '{field_name}' references"
            f" unknown argument '{argument_name}'.",
            field_nodes,
        )

    # Merged field nodes are guaranteed by validation to use identical
    # arguments, so resolving the multiplier on the first node suffices.
    args = get_argument_values(field_def, field_nodes[0], variable_values)
    value = args.get(arg_def.out_name or argument_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CostAnalysisError(
            f"List multiplier argument '{argument_name}' of field"
            f" '{field_name}' must resolve to a non-negative integer,"
            f" got {value!r}.",
            field_nodes,
        )
    return value, list_depth


def _invalid_multiplier_error(
    field_name: str, configured: Any, field_nodes: List[FieldNode]
) -> CostAnalysisError:
    return CostAnalysisError(
        f"Invalid list multiplier configuration for field '{field_name}':"
        " expected a non-negative integer or the name"
        f" of an argument, got {configured!r}.",
        field_nodes,
    )


def _list_depth(type_: Any) -> int:
    depth = 0
    while is_list_type(type_) or is_non_null_type(type_):
        if is_list_type(type_):
            depth += 1
        type_ = type_.of_type
    return depth


def _merged_selection_set(
    field_nodes: List[FieldNode]
) -> Optional[SelectionSetNode]:
    for node in field_nodes:
        if node.selection_set:
            return node.selection_set
    return None


def _get_field_def(
    schema: GraphQLSchema,
    parent_type: GraphQLObjectType,
    field_node: FieldNode,
) -> GraphQLField:
    field_name = field_node.name.value
    if field_name == "__schema" and schema.query_type == parent_type:
        from ..type import SchemaMetaFieldDef

        return SchemaMetaFieldDef
    if field_name == "__type" and schema.query_type == parent_type:
        from ..type import TypeMetaFieldDef

        return TypeMetaFieldDef
    if field_name == "__typename":
        from ..type import TypeNameMetaFieldDef

        return TypeNameMetaFieldDef
    return parent_type.fields[field_name]


def assert_within_cost_budget(
    schema: GraphQLSchema,
    document: DocumentNode,
    max_cost: int,
    variable_values: Optional[Dict[str, Any]] = None,
    operation_name: Optional[str] = None,
) -> CostAnalysisResult:
    """Check that the cost of an operation does not exceed ``max_cost``.

    Returns the :class:`CostAnalysisResult` when the operation is within
    the budget. Otherwise raises :class:`CostBudgetExceededError`, which
    carries the budget, the computed total cost and the response path
    contributing the most.
    """
    result = get_operation_cost(
        schema, document, variable_values, operation_name
    )
    if result.total_cost > max_cost:
        max_path, max_path_cost = _pick_max_contribution(
            result.contributions
        )
        nodes = _find_path_nodes(document, max_path, operation_name)
        raise CostBudgetExceededError(
            max_cost,
            result.total_cost,
            max_path,
            max_path_cost,
            result.contributions,
            nodes,
        )
    return result


check_cost_budget = assert_within_cost_budget


def _pick_max_contribution(
    contributions: Dict[CostPath, int]
) -> Tuple[CostPath, int]:
    # Deterministic tie breaker: prefer paths close to the root and then
    # lexicographically smaller paths.
    return max(
        contributions.items(),
        key=lambda item: (item[1], -len(item[0]), _path_key(item[0])),
    )


def _path_key(path: CostPath) -> Tuple[str, ...]:
    return tuple(reversed(path))


def _find_path_nodes(
    document: DocumentNode,
    path: CostPath,
    operation_name: Optional[str],
) -> List[FieldNode]:
    if not path:
        return []
    operation, _ = _get_operation_and_fragments(document, operation_name)
    field_nodes: List[FieldNode] = []
    selection_set = operation.selection_set
    # Only the top-level field node is needed to point callers at the
    # response subtree contributing the most.
    top_name = path[0]
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            response_name = (
                selection.alias.value
                if selection.alias
                else selection.name.value
            )
            if response_name == top_name:
                field_nodes.append(selection)
    return field_nodes
