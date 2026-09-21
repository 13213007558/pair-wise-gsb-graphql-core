"""Static query cost estimation for admission control.

This module provides :func:`estimate_query_cost` which computes an
explainable, static cost for a GraphQL operation *before* it is executed, so
that a backend can admit or reject expensive operations up front, and
:func:`assert_cost_within_budget` which enforces a budget.

The estimation follows the field collection semantics of the GraphQL
specification (the same rules the executor uses):

* Fields with the same response key are merged and counted only once, while
  different aliases are counted separately.
* ``@skip`` and ``@include`` directives are evaluated with the coerced
  variable values of this request (including variable default values).
* Fragment spreads that are referenced multiple times are collected only once.
* For fields returning interface or union types, the mutually exclusive type
  branches are not added together; the cost of the most expensive possible
  object type is used, since only one of them can match at runtime.
* Configured list multipliers are applied per list layer, so nested lists
  multiply layer by layer.

Safety properties:

* Invalid operations (syntax errors, validation errors, unknown fields or
  fragments, cyclic fragments, unknown or ambiguous operation names, invalid
  variable values) raise a :class:`QueryCostError` instead of returning a
  deceptively low cost.
* Invalid or absurdly large list multipliers are rejected, and the accumulated
  cost as well as the evaluation effort are capped, so that pathological
  queries cannot cause unbounded computation.

Note that the experimental ``@defer`` directive is not supported; operations
using it are rejected with a clear error rather than under-estimated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..error import GraphQLError
from ..execution.collect_fields import (
    FragmentDetails,
    GroupedFieldSet,
    collect_fields,
    collect_subfields,
)
from ..execution.get_variable_signature import get_variable_signature
from ..execution.values import VariableValues, get_variable_values
from ..language import (
    DocumentNode,
    FragmentDefinitionNode,
    OperationDefinitionNode,
    Source,
    parse,
)
from ..type import (
    GraphQLField,
    GraphQLObjectType,
    GraphQLSchema,
    get_named_type,
    is_abstract_type,
    is_list_type,
    is_non_null_type,
    is_object_type,
)
from ..type.introspection import (
    SchemaMetaFieldDef,
    TypeMetaFieldDef,
    TypeNameMetaFieldDef,
)
from ..validation.validate import validate

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_COST_LIMIT",
    "DEFAULT_EFFORT_LIMIT",
    "MAX_LIST_MULTIPLIER",
    "QueryCostError",
    "QueryCostExceededError",
    "assert_cost_within_budget",
    "estimate_query_cost",
]

#: Default upper bound for the cost of a single operation.
DEFAULT_COST_LIMIT = 100_000

#: Default upper bound for the number of field evaluations during estimation.
DEFAULT_EFFORT_LIMIT = 100_000

#: Largest list multiplier accepted by default.
MAX_LIST_MULTIPLIER = 1_000


class QueryCostError(GraphQLError):
    """Raised when the cost of a GraphQL operation cannot be estimated."""


class QueryCostExceededError(QueryCostError):
    """Raised when the estimated cost of an operation exceeds a budget."""

    budget: int | None

    def __init__(self, message: str, budget: int | None = None) -> None:
        super().__init__(message)
        self.budget = budget


class _CostLimitExceededError(QueryCostError):
    """Internal error raised when the configured cost limit is exceeded."""


def _validate_int(
    name: str, value: Any, minimum: int, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{name} must be an integer, got: {value!r}."
        raise QueryCostError(msg)
    if value < minimum:
        msg = f"{name} must be at least {minimum}, got: {value}."
        raise QueryCostError(msg)
    if maximum is not None and value > maximum:
        msg = f"{name} must be at most {maximum}, got: {value}."
        raise QueryCostError(msg)
    return value


class _CostContext:
    """Internal state shared while estimating the cost of an operation."""

    __slots__ = (
        "cost_limit",
        "default_field_cost",
        "default_list_multiplier",
        "field_costs",
        "fragments",
        "list_multipliers",
        "max_list_multiplier",
        "operation",
        "remaining",
        "remaining_effort",
        "schema",
        "variable_values",
    )

    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: dict[str, FragmentDetails],
        variable_values: VariableValues,
        operation: OperationDefinitionNode,
        field_costs: Mapping[str, int] | None,
        list_multipliers: Mapping[str, int] | None,
        default_field_cost: int,
        default_list_multiplier: int,
        max_list_multiplier: int,
        cost_limit: int,
        effort_limit: int,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.variable_values = variable_values
        self.operation = operation
        self.field_costs = field_costs
        self.list_multipliers = list_multipliers
        self.default_field_cost = default_field_cost
        self.default_list_multiplier = default_list_multiplier
        self.max_list_multiplier = max_list_multiplier
        self.cost_limit = cost_limit
        self.remaining = [cost_limit]
        self.remaining_effort = [effort_limit]

    def field_cost(self, parent_type: GraphQLObjectType, field_name: str) -> int:
        key = f"{parent_type.name}.{field_name}"
        costs = self.field_costs
        value = costs.get(key) if costs else None
        if value is None:
            return self.default_field_cost
        return _validate_int(f"Cost of field '{key}'", value, 0, self.cost_limit)

    def list_multiplier(self, parent_type: GraphQLObjectType, field_name: str) -> int:
        key = f"{parent_type.name}.{field_name}"
        multipliers = self.list_multipliers
        value = multipliers.get(key) if multipliers else None
        if value is None:
            return self.default_list_multiplier
        return _validate_int(
            f"List multiplier of field '{key}'", value, 1, self.max_list_multiplier
        )

    def charge(self, cost: int) -> None:
        remaining = self.remaining[0] - cost
        self.remaining[0] = remaining
        if remaining < 0:
            msg = f"Query cost exceeds the configured cost limit of {self.cost_limit}."
            raise _CostLimitExceededError(msg)

    def step(self) -> None:
        remaining = self.remaining_effort[0] - 1
        self.remaining_effort[0] = remaining
        if remaining < 0:
            msg = (
                "Query is too complex to estimate:"
                " the evaluation effort limit was exceeded."
            )
            raise QueryCostError(msg)


def _get_field_def(
    schema: GraphQLSchema, parent_type: GraphQLObjectType, field_name: str
) -> GraphQLField | None:
    if field_name == "__schema" and schema.query_type == parent_type:
        return SchemaMetaFieldDef
    if field_name == "__type" and schema.query_type == parent_type:
        return TypeMetaFieldDef
    if field_name == "__typename":
        return TypeNameMetaFieldDef
    return parent_type.fields.get(field_name)


def _evaluate_grouped_fields(
    ctx: _CostContext,
    grouped_field_set: GroupedFieldSet,
    parent_type: GraphQLObjectType,
    multiplier: int,
) -> int:
    total = 0
    for field_details_list in grouped_field_set.values():
        total += _evaluate_field(ctx, field_details_list, parent_type, multiplier)
    return total


def _evaluate_field(
    ctx: _CostContext,
    field_details_list: list,
    parent_type: GraphQLObjectType,
    multiplier: int,
) -> int:
    ctx.step()
    node = field_details_list[0].node
    field_name = node.name.value
    field_def = _get_field_def(ctx.schema, parent_type, field_name)
    if field_def is None:
        msg = (
            f"Cannot estimate cost: unknown field '{field_name}'"
            f" on type '{parent_type.name}'."
        )
        raise QueryCostError(msg)

    local_multiplier = 1
    type_ = field_def.type
    while True:
        if is_non_null_type(type_):
            type_ = type_.of_type
        elif is_list_type(type_):
            local_multiplier *= ctx.list_multiplier(parent_type, field_name)
            type_ = type_.of_type
        else:
            break

    accumulated = multiplier * local_multiplier
    cost = ctx.field_cost(parent_type, field_name)
    ctx.charge(cost * accumulated)

    if not any(details.node.selection_set for details in field_details_list):
        return cost * local_multiplier

    named_type = get_named_type(field_def.type)
    if is_object_type(named_type):
        sub_cost = _evaluate_subfields(ctx, named_type, field_details_list, accumulated)
    elif is_abstract_type(named_type):
        # The possible object types are mutually exclusive at runtime, so the
        # most expensive branch determines the cost instead of their sum.
        sub_cost = 0
        for possible_type in ctx.schema.get_possible_types(named_type):
            saved = ctx.remaining[0]
            branch_cost = _evaluate_subfields(
                ctx, possible_type, field_details_list, accumulated
            )
            ctx.remaining[0] = saved
            sub_cost = max(sub_cost, branch_cost)
        ctx.charge(sub_cost * accumulated)
    else:
        msg = (
            f"Cannot estimate cost: field '{parent_type.name}.{field_name}'"
            " has a selection set but does not return a composite type."
        )
        raise QueryCostError(msg)
    return (cost + sub_cost) * local_multiplier


def _evaluate_subfields(
    ctx: _CostContext,
    return_type: GraphQLObjectType,
    field_details_list: list,
    multiplier: int,
) -> int:
    collected = collect_subfields(
        ctx.schema,
        ctx.fragments,
        ctx.variable_values,
        ctx.operation,
        return_type,
        field_details_list,
    )
    if collected.new_defer_usages:
        msg = "Cannot estimate cost: the @defer directive is not supported."
        raise QueryCostError(msg)
    return _evaluate_grouped_fields(
        ctx, collected.grouped_field_set, return_type, multiplier
    )


def estimate_query_cost(
    schema: GraphQLSchema,
    document: str | Source | DocumentNode,
    operation_name: str | None = None,
    variable_values: dict[str, Any] | None = None,
    *,
    field_costs: Mapping[str, int] | None = None,
    list_multipliers: Mapping[str, int] | None = None,
    default_field_cost: int = 1,
    default_list_multiplier: int = 1,
    max_list_multiplier: int = MAX_LIST_MULTIPLIER,
    cost_limit: int = DEFAULT_COST_LIMIT,
    effort_limit: int = DEFAULT_EFFORT_LIMIT,
) -> int:
    """Estimate the static cost of a GraphQL operation.

    The operation is parsed (if given as text), validated and its variables
    are coerced (applying default values) before the cost is computed following
    the field collection rules of the GraphQL specification. Any problem with
    the operation raises a :class:`QueryCostError` instead of returning a
    deceptively low cost.

    Field costs and list multipliers are configured with mappings keyed by
    ``"TypeName.fieldName"``; fields without an entry use
    ``default_field_cost`` and ``default_list_multiplier``. List
    multipliers apply per list layer, so nested lists multiply layer by layer,
    and they must be integers between 1 and ``max_list_multiplier``.

    The estimation itself is bounded: it raises a :class:`QueryCostError`
    when the cost exceeds ``cost_limit`` or the number of field evaluations
    exceeds ``effort_limit``.
    """
    _validate_int("Default field cost", default_field_cost, 0, cost_limit)
    _validate_int(
        "Default list multiplier", default_list_multiplier, 1, max_list_multiplier
    )
    _validate_int("Maximum list multiplier", max_list_multiplier, 1)
    _validate_int("Cost limit", cost_limit, 1)
    _validate_int("Effort limit", effort_limit, 1)

    if not isinstance(document, DocumentNode):
        document = parse(document)

    validation_errors = validate(schema, document)
    if validation_errors:
        raise QueryCostError(
            "Cannot estimate cost of an invalid operation: "
            + " ".join(error.message for error in validation_errors)
        )

    operation: OperationDefinitionNode | None = None
    fragments: dict[str, FragmentDetails] = {}
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            if operation_name is None:
                if operation is not None:
                    msg = (
                        "Must provide operation name"
                        " if query contains multiple operations."
                    )
                    raise QueryCostError(msg)
                operation = definition
            elif definition.name and definition.name.value == operation_name:
                operation = definition
        elif isinstance(definition, FragmentDefinitionNode):
            variable_signatures = None
            if definition.variable_definitions:
                variable_signatures = {}
                for var_def in definition.variable_definitions:
                    signature = get_variable_signature(schema, var_def)
                    if not isinstance(signature, GraphQLError):
                        variable_signatures[signature.name] = signature
            fragments[definition.name.value] = FragmentDetails(
                definition, variable_signatures
            )

    if operation is None:
        if operation_name is not None:
            msg = f"Unknown operation named '{operation_name}'."
            raise QueryCostError(msg)
        msg = "Must provide an operation."
        raise QueryCostError(msg)

    root_type = schema.get_root_type(operation.operation)
    if root_type is None:
        msg = (
            "Schema is not configured to execute"
            f" {operation.operation.value} operation."
        )
        raise QueryCostError(msg)

    coerced = get_variable_values(
        schema, operation.variable_definitions or [], variable_values or {}
    )
    if isinstance(coerced, list):
        raise QueryCostError(
            "Cannot estimate cost with invalid variable values: "
            + " ".join(error.message for error in coerced)
        )

    ctx = _CostContext(
        schema,
        fragments,
        coerced,
        operation,
        field_costs,
        list_multipliers,
        default_field_cost,
        default_list_multiplier,
        max_list_multiplier,
        cost_limit,
        effort_limit,
    )

    collected = collect_fields(schema, fragments, coerced, root_type, operation)
    if collected.new_defer_usages:
        msg = "Cannot estimate cost: the @defer directive is not supported."
        raise QueryCostError(msg)
    try:
        return _evaluate_grouped_fields(ctx, collected.grouped_field_set, root_type, 1)
    except RecursionError as error:
        msg = "Cannot estimate cost: the operation is nested too deeply."
        raise QueryCostError(msg) from error


def assert_cost_within_budget(
    schema: GraphQLSchema,
    document: str | Source | DocumentNode,
    budget: int,
    operation_name: str | None = None,
    variable_values: dict[str, Any] | None = None,
    **kwargs: Any,
) -> int:
    """Estimate the cost of an operation and enforce the given budget.

    Returns the estimated cost if it is less than or equal to ``budget``
    and raises a :class:`QueryCostExceededError` otherwise. Any keyword
    arguments are passed on to :func:`estimate_query_cost`.
    """
    _validate_int("Budget", budget, 0)
    kwargs["cost_limit"] = max(budget, 1)
    try:
        return estimate_query_cost(
            schema, document, operation_name, variable_values, **kwargs
        )
    except _CostLimitExceededError:
        msg = f"Query cost exceeds the budget of {budget}."
        raise QueryCostExceededError(msg, budget=budget) from None
