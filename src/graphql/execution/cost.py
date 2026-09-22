from numbers import Integral
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, cast

from ..error import GraphQLError
from ..language import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    SelectionSetNode,
    VariableNode,
)
from ..type import (
    GraphQLAbstractType,
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    SchemaMetaFieldDef,
    TypeMetaFieldDef,
    TypeNameMetaFieldDef,
    get_named_type,
    is_abstract_type,
    is_list_type,
    is_non_null_type,
    is_object_type,
)
from ..utilities.get_operation_ast import get_operation_ast
from ..pyutils import Undefined
from .collect_fields import collect_fields, collect_sub_fields
from .values import get_argument_values, get_variable_values

__all__ = [
    "CostAnalysis",
    "CostBudgetExceededError",
    "calculate_cost",
    "analyze_cost",
    "analyze_operation_cost",
    "assert_cost_within_budget",
    "get_operation_cost",
    "get_cost_budget",
]

ResponsePath = Tuple[Any, ...]
PathCostMap = Dict[ResponsePath, int]

WEIGHT_KEYS = (
    "costWeight",
    "cost_weight",
    "fieldCost",
    "field_cost",
    "fieldWeight",
    "field_weight",
    "complexity",
    "weight",
    "cost",
)
MULTIPLIER_KEYS = (
    "listMultipliers",
    "listMultiplier",
    "list_multipliers",
    "list_multiplier",
    "multipliers",
    "multiplier",
    "listCount",
    "list_count",
)
BUDGET_KEYS = (
    "costBudget",
    "cost_budget",
    "costLimit",
    "cost_limit",
    "maxCost",
    "max_cost",
    "maxComplexity",
    "max_complexity",
    "budget",
)
_MISSING = object()


class CostAnalysis:
    """Result of a static operation cost analysis."""

    __slots__ = ("cost", "path_costs", "subtree_costs", "max_cost_path")

    cost: int
    path_costs: PathCostMap
    subtree_costs: PathCostMap
    max_cost_path: ResponsePath

    def __init__(
        self,
        cost: int,
        path_costs: PathCostMap,
        subtree_costs: PathCostMap,
        max_cost_path: ResponsePath,
    ) -> None:
        self.cost = cost
        self.path_costs = path_costs
        self.subtree_costs = subtree_costs
        self.max_cost_path = max_cost_path

    @property
    def total_cost(self) -> int:
        return self.cost

    @property
    def contributions(self) -> PathCostMap:
        return dict(self.path_costs)

    @property
    def cost_by_path(self) -> PathCostMap:
        return self.path_costs

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cost": self.cost,
            "total_cost": self.cost,
            "path_costs": dict(self.path_costs),
            "subtree_costs": dict(self.subtree_costs),
            "max_cost_path": tuple(self.max_cost_path),
        }

    def __repr__(self) -> str:
        return (
            f"CostAnalysis(cost={self.cost}, "
            f"max_cost_path={list(self.max_cost_path)!r}, "
            f"path_costs={self.path_costs!r})"
        )

    def __eq__(self, other: Any) -> bool:
        return (
            isinstance(other, CostAnalysis)
            and self.cost == other.cost
            and self.path_costs == other.path_costs
            and self.subtree_costs == other.subtree_costs
            and self.max_cost_path == other.max_cost_path
        )


class CostBudgetExceededError(GraphQLError):
    """Raised when an operation static cost exceeds its budget."""

    def __init__(self, budget: int, analysis: CostAnalysis) -> None:
        max_path = list(analysis.max_cost_path)
        message = (
            f"Operation cost {analysis.cost} exceeds budget {budget}. "
            f"The largest contribution is at response path {max_path}."
        )
        path_costs = analysis.contributions
        details = {
            "budget": budget,
            "cost": analysis.cost,
            "total_cost": analysis.cost,
            "max_cost_path": max_path,
            "maxCostPath": max_path,
            "path_costs": {
                ".".join(str(part) for part in path): cost
                for path, cost in path_costs.items()
            },
        }
        details["pathCosts"] = details["path_costs"]
        super().__init__(
            message,
            path=max_path,
            extensions={"code": "COST_BUDGET_EXCEEDED", **details},
        )


class _CostState:
    __slots__ = ("total", "path_costs", "subtree_costs")

    total: int
    path_costs: PathCostMap
    subtree_costs: PathCostMap

    def __init__(
        self,
        total: int = 0,
        path_costs: Optional[PathCostMap] = None,
        subtree_costs: Optional[PathCostMap] = None,
    ) -> None:
        self.total = total
        self.path_costs = path_costs or {}
        self.subtree_costs = subtree_costs or {}

    def add(
        self, path: ResponsePath, field_cost: int, child_state: "_CostState"
    ) -> None:
        self.total += field_cost + child_state.total
        self.path_costs[path] = self.path_costs.get(path, 0) + field_cost
        self.subtree_costs[path] = (
            self.subtree_costs.get(path, 0) + field_cost + child_state.total
        )
        for child_path, cost in child_state.path_costs.items():
            self.path_costs[child_path] = self.path_costs.get(child_path, 0) + cost
        for child_path, cost in child_state.subtree_costs.items():
            self.subtree_costs[child_path] = (
                self.subtree_costs.get(child_path, 0) + cost
            )

    def scaled(self, factor: int) -> "_CostState":
        return _CostState(
            self.total * factor,
            {path: cost * factor for path, cost in self.path_costs.items()},
            {path: cost * factor for path, cost in self.subtree_costs.items()},
        )

    def include_max_branch(self, other: "_CostState") -> None:
        self.total = max(self.total, other.total)
        _merge_max(self.path_costs, other.path_costs)
        _merge_max(self.subtree_costs, other.subtree_costs)


def _merge_max(target: PathCostMap, source: PathCostMap) -> None:
    for path, cost in source.items():
        current = target.get(path)
        if current is None or (cost, path) > (current, path):
            target[path] = cost


def _extension_value(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _cost_extension(
    extensions: Mapping[str, Any], keys: Sequence[str], default: Any
) -> Any:
    nested = extensions.get("cost")
    if isinstance(nested, Mapping):
        for key in keys:
            if key in nested:
                return nested[key]
    for key in keys:
        if key in extensions:
            return extensions[key]
    return default


def get_cost_budget(schema: GraphQLSchema) -> Optional[int]:
    """Get a non-negative cost budget from the schema extensions."""
    extensions = schema.extensions
    budget = _extension_value(extensions, BUDGET_KEYS)
    nested = extensions.get("cost")
    if budget is None and isinstance(nested, Mapping):
        budget = _extension_value(nested, BUDGET_KEYS)
    if budget is None:
        return None
    return validate_cost_budget(budget)


def validate_cost_budget(budget: Any) -> Optional[int]:
    if budget is None:
        return None
    if isinstance(budget, bool) or not isinstance(budget, Integral) or budget < 0:
        raise GraphQLError("Cost budget must be a non-negative integer.")
    return int(budget)


def calculate_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    variable_values: Optional[Dict[str, Any]] = None,
    operation_name: Optional[str] = None,
) -> CostAnalysis:
    """Alias for analyzing the selected operation cost."""
    return analyze_cost(schema, document, variable_values, operation_name)


def analyze_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    variable_values: Optional[Dict[str, Any]] = None,
    operation_name: Optional[str] = None,
) -> CostAnalysis:
    """Analyze the selected operation using weights in schema extensions."""
    operation = get_operation_ast(document, operation_name)
    if operation is None:
        if operation_name is None:
            raise GraphQLError("Must provide an operation.")
        raise GraphQLError(f"Unknown operation named '{operation_name}'.")

    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    return analyze_operation_cost(schema, operation, variable_values, fragments)


def analyze_operation_cost(
    schema: GraphQLSchema,
    operation: OperationDefinitionNode,
    variable_values: Optional[Dict[str, Any]] = None,
    fragments: Optional[Dict[str, FragmentDefinitionNode]] = None,
    _coerced_variable_values: Optional[Dict[str, Any]] = None,
) -> CostAnalysis:
    """Analyze one operation after ordinary GraphQL validation."""
    if _coerced_variable_values is not None:
        coerced_values = _coerced_variable_values
    else:
        coerced = get_variable_values(
            schema, operation.variable_definitions or (), variable_values or {}
        )
        if isinstance(coerced, list):
            raise coerced[0]
        coerced_values = coerced

    root_type = schema.get_root_type(operation.operation)
    if root_type is None:
        raise GraphQLError(
            "Schema is not configured to execute "
            f"{operation.operation.value} operation.",
            operation,
        )

    state = _evaluate_selection_set(
        schema, fragments or {}, coerced_values, root_type, operation.selection_set
    )
    max_path = max(
        state.path_costs,
        key=lambda path: (state.path_costs[path], tuple(reversed(path))),
        default=(),
    )
    return CostAnalysis(
        state.total, dict(state.path_costs), dict(state.subtree_costs), max_path
    )


get_operation_cost = analyze_operation_cost


def assert_cost_within_budget(
    schema: GraphQLSchema,
    document: DocumentNode,
    budget: int,
    variable_values: Optional[Dict[str, Any]] = None,
    operation_name: Optional[str] = None,
) -> CostAnalysis:
    """Analyze an operation and raise if its static cost exceeds the budget."""
    analysis = analyze_cost(schema, document, variable_values, operation_name)
    if analysis.cost > budget:
        raise CostBudgetExceededError(budget, analysis)
    return analysis


def _evaluate_selection_set(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    object_type: GraphQLObjectType,
    selection_set: SelectionSetNode,
    ancestor_path: ResponsePath = (),
) -> _CostState:
    fields = collect_fields(
        schema, fragments, variable_values, object_type, selection_set
    )
    return _evaluate_fields(
        schema, fragments, variable_values, object_type, fields, ancestor_path
    )


def _evaluate_fields(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    object_type: GraphQLObjectType,
    fields: Dict[str, List[FieldNode]],
    ancestor_path: ResponsePath,
) -> _CostState:
    state = _CostState()
    for response_name, field_nodes in fields.items():
        path = ancestor_path + (response_name,)
        field_def = _get_field_def(schema, object_type, field_nodes[0])
        if field_def is None:
            continue
        child_state = _evaluate_field(
            schema, fragments, variable_values, field_def, field_nodes, path
        )
        state.add(path, _field_weight(field_def, field_nodes[0]), child_state)
    return state


def _evaluate_field(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    field_def: GraphQLField,
    field_nodes: List[FieldNode],
    path: ResponsePath,
) -> _CostState:
    factors = _list_multipliers(
        field_def, field_nodes[0], variable_values, field_def.type
    )
    named_type = get_named_type(field_def.type)

    if is_abstract_type(named_type):
        state = _evaluate_abstract_field(
            schema,
            fragments,
            variable_values,
            cast(GraphQLAbstractType, named_type),
            field_nodes,
            path,
        )
    elif is_object_type(named_type):
        state = _evaluate_object_field(
            schema,
            fragments,
            variable_values,
            cast(GraphQLObjectType, named_type),
            field_nodes,
            path,
        )
    else:
        state = _CostState()

    for factor in factors:
        state = state.scaled(factor)
    return state


def _evaluate_object_field(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    object_type: GraphQLObjectType,
    field_nodes: List[FieldNode],
    path: ResponsePath,
) -> _CostState:
    fields = collect_sub_fields(
        schema, fragments, variable_values, object_type, field_nodes
    )
    return _evaluate_fields(
        schema, fragments, variable_values, object_type, fields, path
    )


def _evaluate_abstract_field(
    schema: GraphQLSchema,
    fragments: Dict[str, FragmentDefinitionNode],
    variable_values: Dict[str, Any],
    abstract_type: GraphQLAbstractType,
    field_nodes: List[FieldNode],
    path: ResponsePath,
) -> _CostState:
    candidates = []
    for possible_type in schema.get_possible_types(abstract_type):
        fields = collect_sub_fields(
            schema, fragments, variable_values, possible_type, field_nodes
        )
        candidates.append(
            _evaluate_fields(
                schema,
                fragments,
                variable_values,
                possible_type,
                fields,
                path,
            )
        )
    if not candidates:
        return _CostState()
    state = max(candidates, key=lambda candidate: candidate.total)
    result = _CostState(
        state.total, dict(state.path_costs), dict(state.subtree_costs)
    )
    for candidate in candidates:
        result.include_max_branch(candidate)
    return result


def _get_field_def(
    schema: GraphQLSchema, parent_type: GraphQLObjectType, node: FieldNode
) -> Optional[GraphQLField]:
    field_name = node.name.value
    if field_name == "__schema" and schema.query_type == parent_type:
        return SchemaMetaFieldDef
    if field_name == "__type" and schema.query_type == parent_type:
        return TypeMetaFieldDef
    if field_name == "__typename":
        return TypeNameMetaFieldDef
    return parent_type.fields.get(field_name)


def _field_weight(field_def: GraphQLField, node: FieldNode) -> int:
    weight = _cost_extension(field_def.extensions, WEIGHT_KEYS, _MISSING)
    if weight is _MISSING:
        return 1
    if isinstance(weight, bool) or not isinstance(weight, Integral) or weight < 0:
        raise _field_error(field_def, node, "weights must be non-negative integers.")
    return int(weight)


def _list_multipliers(
    field_def: GraphQLField,
    node: FieldNode,
    variable_values: Dict[str, Any],
    return_type: Any,
) -> Tuple[int, ...]:
    list_depth = _list_depth(return_type)
    configured = _cost_extension(field_def.extensions, MULTIPLIER_KEYS, _MISSING)
    if list_depth == 0:
        if configured is not _MISSING:
            raise _field_error(
                field_def, node, "list multipliers are only valid on list fields."
            )
        return ()
    if configured is _MISSING:
        raise _field_error(field_def, node, "list fields require a list multiplier.")

    if isinstance(configured, Mapping):
        values = []
        for index in range(list_depth):
            value = configured.get(index, configured.get(str(index)))
            if value is None:
                raise _field_error(
                    field_def,
                    node,
                    f"missing list multiplier for list level {index}.",
                )
            values.append(value)
    elif isinstance(configured, (list, tuple)):
        values = list(configured)
        if len(values) != list_depth:
            raise _field_error(
                field_def,
                node,
                f"expected {list_depth} list multipliers, got {len(values)}.",
            )
    else:
        values = [configured] * list_depth

    argument_nodes = {argument.name.value: argument for argument in node.arguments}
    factors = []
    for value in values:
        if isinstance(value, str):
            arg_def = field_def.args.get(value)
            if arg_def is None:
                raise _field_error(
                    field_def,
                    node,
                    f"list multiplier references unknown argument '{value}'.",
                )
            argument_node = argument_nodes.get(value)
            if argument_node is None:
                if arg_def.default_value is Undefined:
                    raise _field_error(
                        field_def,
                        node,
                        f"list multiplier argument '{value}' is missing.",
                    )
                value = arg_def.default_value
            else:
                value_node = argument_node.value
                if isinstance(value_node, VariableNode):
                    variable_name = value_node.name.value
                    if variable_name not in variable_values:
                        if arg_def.default_value is Undefined:
                            raise _field_error(
                                field_def,
                                node,
                                f"list multiplier argument '{value}' is missing.",
                            )
                        value = arg_def.default_value
                    else:
                        value = variable_values[variable_name]
                else:
                    value = get_argument_values(
                        field_def, node, variable_values
                    ).get(arg_def.out_name or value)
        if (
            value is None
            or isinstance(value, bool)
            or not isinstance(value, Integral)
            or value < 0
        ):
            raise _field_error(
                field_def, node, "list multipliers must be non-negative integers."
            )
        factors.append(int(value))
    return tuple(factors)


def _list_depth(return_type: Any) -> int:
    depth = 0
    while is_non_null_type(return_type) or is_list_type(return_type):
        if is_list_type(return_type):
            depth += 1
        return_type = return_type.of_type
    return depth


def _field_error(field_def: GraphQLField, node: FieldNode, detail: str) -> GraphQLError:
    return GraphQLError(
        f"Invalid cost configuration for field '{node.name.value}': {detail}",
        node,
    )
