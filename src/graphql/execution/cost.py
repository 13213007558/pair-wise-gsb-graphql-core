"""Explainable operation cost analysis."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from ..error import GraphQLError
from ..language import FieldNode, FragmentDefinitionNode, OperationDefinitionNode
from ..type import (
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    get_named_type,
    is_abstract_type,
    is_composite_type,
    is_list_type,
    is_non_null_type,
    is_object_type,
)
from .collect_fields import (
    FieldDetails,
    FragmentDetails,
    collect_fields,
    collect_subfields,
)
from .get_variable_signature import GraphQLVariableSignature, get_variable_signature
from .values import VariableValues, get_argument_values, get_variable_values

if TYPE_CHECKING:
    from ..language import DocumentNode
    from ..type import GraphQLOutputType
    from ..validation import ASTValidationRule

__all__ = [
    "CostAnalysisError",
    "CostBudgetExceededError",
    "CostFieldContext",
    "CostOptions",
    "FieldCost",
    "ObjectCost",
    "OperationCost",
    "TypeCost",
    "assert_within_cost_budget",
    "get_operation_cost",
]

_DEFAULT_MAX_COST = 2**31 - 1
_DEFAULT_MAX_MULTIPLIER = 2**31 - 1

FieldWeightResolver: TypeAlias = Callable[["CostFieldContext"], int]
ListMultiplierResolver: TypeAlias = Callable[["CostFieldContext"], int | float | None]


@dataclass(frozen=True)
class CostFieldContext:
    """Information available while pricing a field."""

    schema: GraphQLSchema
    parent_type: GraphQLObjectType
    field: GraphQLField
    node: FieldNode
    args: dict[str, Any]
    variable_values: VariableValues


@dataclass(frozen=True)
class CostOptions:
    """Configuration for operation cost analysis."""

    field_weight: FieldWeightResolver | None = None
    list_multiplier: ListMultiplierResolver | None = None
    max_cost: int = _DEFAULT_MAX_COST
    max_multiplier: int = _DEFAULT_MAX_MULTIPLIER
    validation_rules: Collection[type[ASTValidationRule]] | None = None
    hide_suggestions: bool = False


@dataclass(frozen=True)
class FieldCost:
    """Cost of one collected response key."""

    response_key: str
    field_name: str
    own_cost: int
    child_cost: int
    multiplied_child_cost: int
    total_cost: int
    list_multiplier: int
    type_costs: tuple[TypeCost, ...] = ()


@dataclass(frozen=True)
class TypeCost:
    """Cost of a field selection on one possible runtime type."""

    object_type: str
    total_cost: int
    object_cost: ObjectCost


@dataclass(frozen=True)
class ObjectCost:
    """Cost of the fields collected for one concrete object type."""

    fields: tuple[FieldCost, ...]
    total_cost: int


@dataclass(frozen=True)
class OperationCost:
    """Total and explainable cost of one operation."""

    operation: str | None
    operation_type: str
    total_cost: int
    object_cost: ObjectCost


class CostAnalysisError(GraphQLError):
    """Raised when an operation cannot be safely costed."""


class CostBudgetExceededError(CostAnalysisError):
    """Raised when an operation exceeds an admission budget."""

    def __init__(self, cost: OperationCost, budget: int) -> None:
        self.cost = cost
        self.budget = budget
        super().__init__(
            f"Operation cost {cost.total_cost} exceeds budget {budget}.",
            extensions={
                "cost": cost.total_cost,
                "budget": budget,
                "code": "COST_BUDGET_EXCEEDED",
            },
        )


def default_field_weight(context: CostFieldContext) -> int:
    """Get a field's weight from its ``cost`` extension, defaulting to one."""
    cost = context.field.extensions.get("cost")
    return cost if isinstance(cost, int) and not isinstance(cost, bool) else 1


def default_list_multiplier(context: CostFieldContext) -> int | None:
    """Get a list field's multiplier from arguments or extensions."""
    multiplier = context.args.get("listMultiplier")
    if multiplier is None:
        multiplier = context.field.extensions.get("listMultiplier")
    return (
        multiplier
        if isinstance(multiplier, int) and not isinstance(multiplier, bool)
        else None
    )


def get_operation_cost(
    schema: GraphQLSchema,
    document: DocumentNode,
    variable_values: Mapping[str, Any] | None = None,
    operation_name: str | None = None,
    options: CostOptions | None = None,
) -> OperationCost:
    """Calculate the explainable static cost of an executable operation.

    Field collection uses GraphQL response keys, repeated fragment spreads are
    merged, aliases remain separate, and abstract selections are priced by the
    most expensive possible concrete object type.
    """
    if options is None:
        options = CostOptions()
    _validate_options(options)
    from ..validation.specified_rules import specified_rules
    from ..validation.validate import validate

    validation_errors = validate(
        schema,
        document,
        rules=options.validation_rules or specified_rules,
        hide_suggestions=options.hide_suggestions,
    )
    if validation_errors:
        error = validation_errors[0]
        msg = "Cannot calculate operation cost for an invalid operation."
        raise CostAnalysisError(
            msg,
            error.nodes,
            original_error=error,
            extensions={"code": "INVALID_OPERATION"},
        )

    operation = _get_operation(document, operation_name)
    fragments, signature_errors = _get_fragments(schema, document)
    if signature_errors:
        error = signature_errors[0]
        msg = "Cannot calculate operation cost for an invalid operation."
        raise CostAnalysisError(
            msg,
            error.nodes,
            original_error=error,
            extensions={"code": "INVALID_OPERATION"},
        )

    coerced_variables = get_variable_values(
        schema,
        operation.variable_definitions or (),
        dict(variable_values or {}),
        hide_suggestions=options.hide_suggestions,
    )
    if isinstance(coerced_variables, list):
        error = coerced_variables[0]
        msg = "Cannot calculate operation cost with invalid variables."
        raise CostAnalysisError(
            msg,
            error.nodes,
            original_error=error,
            extensions={"code": "INVALID_VARIABLES"},
        )

    root_type = schema.get_root_type(operation.operation)
    if root_type is None or not is_object_type(root_type):
        msg = f"Schema is not configured to execute {operation.operation.value}."
        raise CostAnalysisError(msg, operation)

    analyzer = _CostAnalyzer(schema, fragments, coerced_variables, options)
    root_cost = analyzer.object_cost(root_type, operation, None)
    return OperationCost(
        operation=operation.name.value if operation.name else None,
        operation_type=operation.operation.value,
        total_cost=root_cost.total_cost,
        object_cost=root_cost,
    )


def assert_within_cost_budget(cost: OperationCost, budget: int) -> None:
    """Raise unless ``budget`` admits the calculated cost."""
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        msg = "Cost budget must be a non-negative integer."
        raise ValueError(msg)
    if cost.total_cost > budget:
        raise CostBudgetExceededError(cost, budget)


def _validate_options(options: CostOptions) -> None:
    for name in ("max_cost", "max_multiplier"):
        value = getattr(options, name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            msg = f"{name} must be a positive integer."
            raise ValueError(msg)
    if options.field_weight is not None and not callable(options.field_weight):
        msg = "field_weight must be callable."
        raise TypeError(msg)
    if options.list_multiplier is not None and not callable(options.list_multiplier):
        msg = "list_multiplier must be callable."
        raise TypeError(msg)


def _get_operation(
    document: DocumentNode, operation_name: str | None
) -> OperationDefinitionNode:
    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    if operation_name is None:
        if not operations:
            msg = "Must provide an operation."
            raise CostAnalysisError(msg)
        if len(operations) > 1:
            msg = "Must provide operation name if query contains multiple operations."
            raise CostAnalysisError(msg)
        return operations[0]
    for operation in operations:
        if operation.name and operation.name.value == operation_name:
            return operation
    msg = f"Unknown operation named '{operation_name}'."
    raise CostAnalysisError(msg)


def _get_fragments(
    schema: GraphQLSchema, document: DocumentNode
) -> tuple[dict[str, FragmentDetails], list[GraphQLError]]:
    fragments: dict[str, FragmentDetails] = {}
    errors: list[GraphQLError] = []
    for definition in document.definitions:
        if not isinstance(definition, FragmentDefinitionNode):
            continue
        signatures: dict[str, GraphQLVariableSignature] | None = None
        if definition.variable_definitions:
            signatures = {}
            for variable_definition in definition.variable_definitions:
                signature = get_variable_signature(schema, variable_definition)
                if isinstance(signature, GraphQLError):
                    errors.append(signature)
                    continue
                signatures[signature.name] = signature
        fragments[definition.name.value] = FragmentDetails(definition, signatures)
    return fragments, errors


class _CostAnalyzer:
    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: dict[str, FragmentDetails],
        variable_values: VariableValues,
        options: CostOptions,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.variable_values = variable_values
        self.options = options
        self.field_weight = options.field_weight or default_field_weight
        self.list_multiplier = options.list_multiplier or default_list_multiplier
        self.object_cost_cache: dict[object, ObjectCost] = {}

    def object_cost(
        self,
        object_type: GraphQLObjectType,
        operation: OperationDefinitionNode,
        field_details_list: list[FieldDetails] | None = None,
    ) -> ObjectCost:
        cache_key: object = (
            object_type,
            None
            if field_details_list is None
            else tuple(
                (
                    id(details.node),
                    id(details.fragment_variable_values),
                )
                for details in field_details_list
            ),
        )
        cached = self.object_cost_cache.get(cache_key)
        if cached is not None:
            return cached

        if field_details_list is None:
            grouped_fields = collect_fields(
                self.schema,
                self.fragments,
                self.variable_values,
                object_type,
                operation,
                self.options.hide_suggestions,
            ).grouped_field_set
        else:
            grouped_fields = collect_subfields(
                self.schema,
                self.fragments,
                self.variable_values,
                operation,
                object_type,
                field_details_list,
                self.options.hide_suggestions,
            ).grouped_field_set
        fields = tuple(
            self.field_cost(object_type, response_key, details, operation)
            for response_key, details in grouped_fields.items()
        )
        result = ObjectCost(fields, self._add_all(field.total_cost for field in fields))
        self.object_cost_cache[cache_key] = result
        return result

    def field_cost(
        self,
        parent_type: GraphQLObjectType,
        response_key: str,
        field_details_list: list[FieldDetails],
        operation: OperationDefinitionNode,
    ) -> FieldCost:
        first_details = field_details_list[0]
        node = first_details.node
        field_name = node.name.value
        field = self.schema.get_field(parent_type, field_name)
        if field is None:
            msg = f"Cannot find field '{field_name}' on type '{parent_type.name}'."
            raise CostAnalysisError(msg, node)

        args = get_argument_values(
            field,
            node,
            self.variable_values,
            first_details.fragment_variable_values,
            self.options.hide_suggestions,
        )
        context = CostFieldContext(
            self.schema,
            parent_type,
            field,
            node,
            args,
            self.variable_values,
        )
        own_cost = self._weight(context)
        return_type = field.type
        list_multiplier = self._list_multiplier(context, return_type)
        child_cost, type_costs = self._selection_cost(
            return_type, field_details_list, operation
        )
        multiplied_child_cost = self._multiply(child_cost, list_multiplier, node)
        total_cost = self._add(own_cost, multiplied_child_cost, node)
        return FieldCost(
            response_key,
            field_name,
            own_cost,
            child_cost,
            multiplied_child_cost,
            total_cost,
            list_multiplier,
            type_costs,
        )

    def _selection_cost(
        self,
        return_type: GraphQLOutputType,
        field_details_list: list[FieldDetails],
        operation: OperationDefinitionNode,
    ) -> tuple[int, tuple[TypeCost, ...]]:
        named_type = get_named_type(return_type)
        node = field_details_list[0].node
        if (
            named_type is None
            or not is_composite_type(named_type)
            or not node.selection_set
        ):
            return 0, ()
        if is_object_type(named_type):
            object_cost = self.object_cost(named_type, operation, field_details_list)
            return object_cost.total_cost, (
                TypeCost(named_type.name, object_cost.total_cost, object_cost),
            )
        if is_abstract_type(named_type):
            type_costs = tuple(
                self._possible_type_cost(possible_type, field_details_list, operation)
                for possible_type in self.schema.get_possible_types(named_type)
            )
            if not type_costs:
                return 0, ()
            maximum = max(type_costs, key=lambda type_cost: type_cost.total_cost)
            return maximum.total_cost, type_costs
        return 0, ()

    def _possible_type_cost(
        self,
        object_type: GraphQLObjectType,
        field_details_list: list[FieldDetails],
        operation: OperationDefinitionNode,
    ) -> TypeCost:
        object_cost = self.object_cost(object_type, operation, field_details_list)
        return TypeCost(object_type.name, object_cost.total_cost, object_cost)

    def _weight(self, context: CostFieldContext) -> int:
        try:
            weight = self.field_weight(context)
        except CostAnalysisError:
            raise
        except Exception as error:
            msg = f"Field weight resolver raised: {error}"
            raise CostAnalysisError(
                msg,
                context.node,
                original_error=error,
            ) from error
        weight = self._integer(weight, "Field weight", context.node)
        return self._bounded(weight, self.options.max_cost, context.node, "cost")

    def _list_multiplier(
        self,
        context: CostFieldContext,
        return_type: GraphQLOutputType,
    ) -> int:
        current_type: Any = return_type
        multiplier = 1
        while True:
            if is_list_type(current_type):
                try:
                    value = self.list_multiplier(context)
                except CostAnalysisError:
                    raise
                except Exception as error:
                    msg = f"List multiplier resolver raised: {error}"
                    raise CostAnalysisError(
                        msg,
                        context.node,
                        original_error=error,
                    ) from error
                layer_multiplier = self._multiplier_integer(value, context.node)
                multiplier = self._multiply(
                    multiplier,
                    layer_multiplier,
                    context.node,
                    limit=self.options.max_multiplier,
                )
            if is_non_null_type(current_type) or is_list_type(current_type):
                current_type = current_type.of_type
                continue
            return multiplier

    def _multiplier_integer(self, value: Any, node: FieldNode) -> int:
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            msg = "List multiplier must be an integer."
            raise CostAnalysisError(msg, node)
        if isinstance(value, float):
            if not value.is_integer():
                msg = "List multiplier must be an integer."
                raise CostAnalysisError(msg, node)
            try:
                value = int(value)
            except OverflowError as error:
                msg = "List multiplier is too large."
                raise CostAnalysisError(msg, node) from error
        if value < 0:
            msg = "List multiplier must be non-negative."
            raise CostAnalysisError(msg, node)
        return self._bounded(
            value, self.options.max_multiplier, node, "list multiplier"
        )

    def _integer(self, value: Any, label: str, node: FieldNode) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            msg = f"{label} must be an integer."
            raise CostAnalysisError(msg, node)
        if value < 0:
            msg = f"{label} must be non-negative."
            raise CostAnalysisError(msg, node)
        return value

    def _bounded(self, value: int, limit: int, node: FieldNode, label: str) -> int:
        if value > limit:
            msg = f"{label.capitalize()} exceeds safe limit of {limit}."
            raise CostAnalysisError(msg, node)
        return value

    def _add_all(self, values: Any) -> int:
        total = 0
        for value in values:
            total = self._add(total, value)
        return total

    def _add(self, left: int, right: int, node: FieldNode | None = None) -> int:
        total = left + right
        if total > self.options.max_cost:
            msg = f"Operation cost exceeds safe limit of {self.options.max_cost}."
            raise CostAnalysisError(msg, node)
        return total

    def _multiply(
        self,
        left: int,
        right: int,
        node: FieldNode | None = None,
        limit: int | None = None,
    ) -> int:
        if right == 0:
            return 0
        effective_limit = limit or self.options.max_cost
        if left > effective_limit // right:
            label = "Multiplier" if limit is not None else "Operation cost"
            msg = f"{label} exceeds safe limit of {effective_limit}."
            raise CostAnalysisError(msg, node)
        return left * right
