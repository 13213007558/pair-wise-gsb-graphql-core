"""Pre-execution cost analysis for GraphQL operations.

This module provides an optional static cost check that runs after variable and
argument coercion but before any field resolver is invoked. It allows applications
to assign a base cost to each field and a list multiplier to fields returning list
types, and to reject operations whose estimated cost exceeds a configured limit.

Cost semantics
--------------

- Every selected field contributes its configured base cost (weight), multiplied
  by the product of the list multipliers of all list fields that contain it. A
  field's own list multiplier applies to its sub-selections, not to the field
  itself (one resolver call happens regardless of the returned list length).
- Field merging follows the execution rules in :mod:`graphql.execution.collect_fields`:
  selections merged into the same response key are billed once as one field group,
  while the same field under different aliases is billed separately.
- Fragments are expanded exactly like execution does, including ``@skip`` and
  ``@include`` evaluated against the coerced variable values. A fragment reused in
  several positions is billed in each position.
- For interface and union fields, the runtime object type cannot be known before
  execution, so the cost of every possible concrete type branch is computed and the
  maximum is billed (a conservative upper bound).
- When a list multiplier cannot be determined safely (the configuration provides
  neither a value nor a callback result), the configured conservative default
  multiplier is used rather than silently assuming a multiplier of one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple, TypeAlias

from ..error import GraphQLError
from ..type import (
    GraphQLAbstractType,
    GraphQLField,
    GraphQLObjectType,
    GraphQLOutputType,
    GraphQLSchema,
    get_named_type,
    is_abstract_type,
    is_list_type,
    is_non_null_type,
    is_object_type,
)
from .collect_fields import (
    FieldDetailsList,
    FragmentDetails,
    collect_fields,
    collect_subfields,
)
from .values import VariableValues, get_argument_values

if TYPE_CHECKING:
    from ..language import FieldNode, OperationDefinitionNode

__all__ = [
    "COST_EXTENSION_KEY",
    "CostBreakdownEntry",
    "CostCallback",
    "CostLimit",
    "FieldCost",
    "FieldCostInfo",
    "OperationCost",
    "check_cost_limit",
    "default_cost_callback",
    "get_operation_cost",
]

#: Key under which per-field cost configuration may be stored in
#: ``GraphQLField.extensions``. The value must be a mapping with optional
#: ``weight`` and ``list_multiplier`` entries.
COST_EXTENSION_KEY = "cost"

#: Weight assigned to a field when neither the extensions nor the callback
#: provide one.
DEFAULT_FIELD_WEIGHT = 1

#: Conservative upper bound used for a list multiplier when the schema does not
#: configure one and the callback does not return one. It is deliberately not
#: ``1``, which would amount to pretending the list always contains one item.
DEFAULT_UNKNOWN_LIST_MULTIPLIER = 100


class FieldCostInfo(NamedTuple):
    """Information passed to a :data:`CostCallback` for one field group.

    The callback only receives validated schema metadata and the already coerced
    argument values of the field. It has no access to resolvers, source values,
    context values or any external service.
    """

    field: GraphQLField
    """The schema definition of the field."""

    parent_type: GraphQLObjectType
    """The object type on which the field is selected."""

    field_name: str
    """The actual field name (not an alias) in the schema."""

    response_name: str
    """The response key, which differs from the field name when aliased."""

    args: dict[str, object]
    """The coerced argument values, identical to those passed to the resolver."""

    schema: GraphQLSchema
    """The schema used for the operation."""

    is_list: bool
    """Whether the (unwrapped) return type of the field is a list."""

    default_list_multiplier: int
    """Conservative multiplier that will be used if the callback returns none."""


class FieldCost(NamedTuple):
    """Cost configuration for a field, as returned by a :data:`CostCallback`.

    ``list_multiplier`` of ``None`` means that no safe upper bound is known; the
    analysis then falls back to the conservative default of the cost limit. A
    multiplier of ``1`` is an explicit statement that the list never fans out and
    is therefore not treated as unknown.
    """

    weight: float = DEFAULT_FIELD_WEIGHT
    """Base cost added once per selected field group."""

    list_multiplier: float | None = None
    """Upper bound applied to sub-selections for each list dimension, or ``None``
    when no safe bound is known and the conservative default should be used."""


CostCallback: TypeAlias = Callable[[FieldCostInfo], FieldCost | None]
"""Callback returning the cost configuration of a field, or ``None`` to fall back
to the configuration stored in the field extensions (and then to the defaults)."""


class CostBreakdownEntry(NamedTuple):
    """One billed field group in the cost breakdown."""

    path: str
    """Response path of the field group, using aliases where present."""

    field_name: str
    """Actual schema field name."""

    weight: float
    """Configured base cost of the field."""

    multiplier: float
    """Product of the list multipliers applied to this field."""

    cost: float
    """Contribution to the total cost (``weight * multiplier``)."""

    fallback_used: bool
    """Whether a conservative default list multiplier was applied on an ancestor
    list field."""


class OperationCost(NamedTuple):
    """Result of analyzing an operation."""

    total: float
    """Sum of all breakdown entry costs."""

    breakdown: list[CostBreakdownEntry]
    """One entry per billed field group, in traversal order."""

    fallback_paths: list[str]
    """Paths of list fields for which a conservative default multiplier was used."""


class CostLimit(NamedTuple):
    """Configuration for a pre-execution cost limit.

    :param max_cost: Maximum allowed total cost. An operation is rejected as soon
        as its estimated cost exceeds this value.
    :param default_list_multiplier: Conservative upper bound applied to list
        fields whose multiplier cannot be determined safely. Never silently
        defaults to ``1``.
    :param cost_callback: Optional callback computing the cost configuration of a
        field from validated schema metadata and coerced arguments. When it
        returns ``None``, the field extensions under the
        :data:`COST_EXTENSION_KEY` key are used.
    """

    max_cost: float
    """Maximum allowed total estimated cost."""

    default_list_multiplier: int = DEFAULT_UNKNOWN_LIST_MULTIPLIER
    """Conservative upper bound for list fields with no known multiplier."""

    cost_callback: CostCallback | None = None
    """Optional callback resolving the cost configuration of each field."""


def default_cost_callback(info: FieldCostInfo) -> FieldCost | None:
    """Read the cost configuration from the field extensions.

    This is the callback used when no custom callback is supplied. It looks up a
    ``cost`` mapping on ``info.field.extensions``, supporting optional ``weight``
    and ``list_multiplier`` entries, and returns ``None`` (so that the built-in
    defaults apply) when no such mapping exists.

    :param info: Validated field metadata and coerced arguments for one field.
    :returns: The configured :class:`FieldCost`, or ``None`` when the field has
        no cost extension.

    >>> from graphql import build_schema
    >>> from graphql.execution.cost_analysis import (
    ...     FieldCostInfo, default_cost_callback,
    ... )
    >>> schema = build_schema('type Query { users: [String] }')
    >>> field = schema.query_type.fields['users']
    >>> info = FieldCostInfo(
    ...     field=field, parent_type=schema.query_type, field_name='users',
    ...     response_name='users', args={}, schema=schema, is_list=True,
    ...     default_list_multiplier=100,
    ... )
    >>> default_cost_callback(info) is None
    True
    """
    config = info.field.extensions.get(COST_EXTENSION_KEY)
    if not config:
        return None
    weight = config.get("weight", DEFAULT_FIELD_WEIGHT)
    list_multiplier = config.get("list_multiplier")
    return FieldCost(weight=weight, list_multiplier=list_multiplier)


def get_operation_cost(
    schema: GraphQLSchema,
    fragments: dict[str, FragmentDetails],
    operation: OperationDefinitionNode,
    variable_values: VariableValues,
    cost_limit: CostLimit,
    hide_suggestions: bool = False,
) -> OperationCost:
    """Compute the estimated cost of a selected operation.

    Only the given operation is analyzed; other operations in the document are
    ignored. Fragment spreads, inline fragments and ``@skip``/``@include``
    directives are resolved exactly as during execution, using the coerced
    variable values.

    Argument values passed to the cost callback have completed normal input
    coercion. No resolver, type resolver or any other external code is invoked.

    :param schema: The schema used for the operation.
    :param fragments: Fragment details as prepared by the executor.
    :param operation: The selected operation definition node.
    :param variable_values: The coerced operation variable values.
    :param cost_limit: Cost limit configuration, including the callback and the
        conservative default list multiplier.
    :param hide_suggestions: Whether suggestion text should be omitted from
        coercion errors.
    :returns: The total cost together with a detailed breakdown.
    :raises GraphQLError: If an argument needed for billing cannot be coerced.

    >>> from graphql import COST_EXTENSION_KEY, CostLimit, Executor, build_schema
    >>> from graphql import parse
    >>> schema = build_schema('type Query { users: [String] }')
    >>> schema.query_type.fields['users'].extensions[COST_EXTENSION_KEY] = {
    ...     'weight': 2, 'list_multiplier': 5,
    ... }
    >>> executor = Executor.build(schema, parse('{ users }'))
    >>> cost = get_operation_cost(
    ...     schema, executor.fragments, executor.operation,
    ...     executor.variable_values, CostLimit(max_cost=100),
    ... )
    >>> cost.total
    2
    """
    analyzer = _CostAnalyzer(
        schema, fragments, operation, variable_values, cost_limit,
        hide_suggestions,
    )
    return analyzer.analyze_operation()


def check_cost_limit(
    schema: GraphQLSchema,
    fragments: dict[str, FragmentDetails],
    operation: OperationDefinitionNode,
    variable_values: VariableValues,
    cost_limit: CostLimit,
    hide_suggestions: bool = False,
) -> GraphQLError | None:
    """Check an operation against a cost limit.

    Returns a located :class:`~graphql.error.GraphQLError` describing the main
    cost source when the estimated cost exceeds the limit, and ``None`` when the
    operation is within the limit. The error message always states the estimated
    and maximum cost and points to the most expensive field group.

    :param schema: The schema used for the operation.
    :param fragments: Fragment details as prepared by the executor.
    :param operation: The selected operation definition node.
    :param variable_values: The coerced operation variable values.
    :param cost_limit: The cost limit configuration to check against.
    :param hide_suggestions: Whether suggestion text should be omitted from
        coercion errors.
    :returns: A located error when the limit is exceeded, otherwise ``None``.
    :raises GraphQLError: If an argument needed for billing cannot be coerced.

    >>> from graphql import CostLimit, Executor, build_schema, parse
    >>> schema = build_schema('type Query { cheap: String }')
    >>> executor = Executor.build(schema, parse('{ cheap }'))
    >>> check_cost_limit(
    ...     schema, executor.fragments, executor.operation,
    ...     executor.variable_values, CostLimit(max_cost=1),
    ... ) is None
    True
    """
    analyzer = _CostAnalyzer(
        schema, fragments, operation, variable_values, cost_limit,
        hide_suggestions,
    )
    cost = analyzer.analyze_operation()
    if cost.total <= cost_limit.max_cost:
        return None

    main = max(cost.breakdown, default=None, key=lambda entry: entry.cost)
    if main is None:
        location_text = ""
        node = None
    else:
        location_text = (
            f" Main cost source: field '{main.path}' ({main.cost:g})."
        )
        node = analyzer.nodes.get(main.path)
    fallback_text = (
        " A conservative default list multiplier was used for:"
        f" {', '.join(cost.fallback_paths)}."
        if cost.fallback_paths
        else ""
    )
    message = (
        f"Operation cost {cost.total:g} exceeds the maximum allowed cost"
        f" {cost_limit.max_cost:g}.{location_text}{fallback_text}"
    )
    return GraphQLError(message, node)


def _unwrap_non_null(type_: GraphQLOutputType) -> GraphQLOutputType:
    while is_non_null_type(type_):
        type_ = type_.of_type
    return type_


def _list_dimensions(return_type: GraphQLOutputType) -> int:
    """Count the list dimensions of a return type, ignoring non-null wrappers."""
    dimensions = 0
    while True:
        return_type = _unwrap_non_null(return_type)
        if is_list_type(return_type):
            dimensions += 1
            return_type = return_type.of_type
        else:
            return dimensions


class _CollectedSubtree(NamedTuple):
    cost: float
    entries: list[CostBreakdownEntry]
    fallback_paths: list[str]


class _CostAnalyzer:
    """Traverses the selected field sets and accumulates the estimated cost."""

    def __init__(
        self,
        schema: GraphQLSchema,
        fragments: dict[str, FragmentDetails],
        operation: OperationDefinitionNode,
        variable_values: VariableValues,
        cost_limit: CostLimit,
        hide_suggestions: bool,
    ) -> None:
        self.schema = schema
        self.fragments = fragments
        self.operation = operation
        self.variable_values = variable_values
        self.cost_limit = cost_limit
        self.hide_suggestions = hide_suggestions
        self.callback: CostCallback = cost_limit.cost_callback or default_cost_callback
        # Maps a billed response path to a representative field node, used to
        # locate cost-limit errors.
        self.nodes: dict[str, FieldNode] = {}

    def analyze_operation(self) -> OperationCost:
        root_type = self.schema.get_root_type(self.operation.operation)
        if root_type is None:
            msg = (
                "Schema is not configured to execute"
                f" {self.operation.operation.value} operation."
            )
            raise GraphQLError(msg, self.operation)

        collected = collect_fields(
            self.schema,
            self.fragments,
            self.variable_values,
            root_type,
            self.operation,
            self.hide_suggestions,
        )
        subtree = self._analyze_fields(
            root_type, collected.grouped_field_set, "", 1, False
        )
        total = sum(entry.cost for entry in subtree.entries)
        return OperationCost(total, subtree.entries, subtree.fallback_paths)

    def _resolve_field_cost(self, info: FieldCostInfo) -> FieldCost:
        cost = self.callback(info)
        if cost is None:
            cost = default_cost_callback(info)
        if cost is None:
            cost = FieldCost()
        return cost

    def _analyze_fields(
        self,
        parent_type: GraphQLObjectType,
        grouped_field_set: dict[str, FieldDetailsList],
        path_prefix: str,
        multiplier: float,
        fallback_active: bool,
    ) -> _CollectedSubtree:
        cost_sum = 0.0
        entries: list[CostBreakdownEntry] = []
        fallback_paths: list[str] = []

        for response_name, field_details_list in grouped_field_set.items():
            first_details = field_details_list[0]
            first_node = first_details.node
            field_name = first_node.name.value
            path = (
                f"{path_prefix}.{response_name}" if path_prefix else response_name
            )
            self.nodes[path] = first_node

            field_def = self.schema.get_field(parent_type, field_name)
            if field_def is None:
                # Unknown fields are rejected by validation; bill a minimal
                # default anyway instead of dropping the selection silently.
                field_cost = DEFAULT_FIELD_WEIGHT * multiplier
                entries.append(
                    CostBreakdownEntry(
                        path, field_name, DEFAULT_FIELD_WEIGHT, multiplier,
                        field_cost, fallback_active,
                    )
                )
                cost_sum += field_cost
                continue

            # Use the same coercion path as the executor, so billing only ever
            # sees argument values that would also be handed to the resolver.
            args = (
                get_argument_values(
                    field_def,
                    first_node,
                    self.variable_values,
                    first_details.fragment_variable_values,
                    self.hide_suggestions,
                )
                if field_def.args or first_node.arguments
                else {}
            )
            nullable_type: GraphQLOutputType = _unwrap_non_null(field_def.type)
            info = FieldCostInfo(
                field=field_def,
                parent_type=parent_type,
                field_name=field_name,
                response_name=response_name,
                args=args,
                schema=self.schema,
                is_list=is_list_type(nullable_type),
                default_list_multiplier=self.cost_limit.default_list_multiplier,
            )
            field_cost_config = self._resolve_field_cost(info)
            entry_cost = field_cost_config.weight * multiplier
            entries.append(
                CostBreakdownEntry(
                    path,
                    field_name,
                    field_cost_config.weight,
                    multiplier,
                    entry_cost,
                    fallback_active,
                )
            )
            cost_sum += entry_cost

            subtree = self._analyze_subselections(
                field_def,
                field_details_list,
                path,
                multiplier,
                field_cost_config,
                fallback_active,
            )
            cost_sum += subtree.cost
            entries.extend(subtree.entries)
            fallback_paths.extend(subtree.fallback_paths)

        return _CollectedSubtree(cost_sum, entries, fallback_paths)

    def _analyze_subselections(
        self,
        field_def: GraphQLField,
        field_details_list: FieldDetailsList,
        path: str,
        multiplier: float,
        field_cost: FieldCost,
        fallback_active: bool,
    ) -> _CollectedSubtree:
        dimensions = _list_dimensions(field_def.type)
        if dimensions:
            if field_cost.list_multiplier is None:
                # No safe upper bound is known: apply the explicit conservative
                # default instead of silently assuming a multiplier of one.
                list_multiplier = float(self.cost_limit.default_list_multiplier)
                fallback_active = True
                fallback_paths = [path]
            else:
                list_multiplier = float(field_cost.list_multiplier)
                fallback_paths = []
            child_multiplier = multiplier * list_multiplier**dimensions
        else:
            child_multiplier = multiplier
            fallback_paths = []

        named_type: GraphQLOutputType = get_named_type(field_def.type)
        if is_object_type(named_type):
            subtree = self._analyze_object_subfields(
                named_type, field_details_list, path,
                child_multiplier, fallback_active,
            )
        elif is_abstract_type(named_type):
            subtree = self._analyze_abstract_subfields(
                named_type, field_details_list, path,
                child_multiplier, fallback_active,
            )
        else:
            # Leaf types have no sub-selections.
            return _CollectedSubtree(0, [], fallback_paths)

        return _CollectedSubtree(
            subtree.cost,
            subtree.entries,
            fallback_paths + subtree.fallback_paths,
        )

    def _analyze_object_subfields(
        self,
        object_type: GraphQLObjectType,
        field_details_list: FieldDetailsList,
        path_prefix: str,
        multiplier: float,
        fallback_active: bool,
    ) -> _CollectedSubtree:
        collected = collect_subfields(
            self.schema,
            self.fragments,
            self.variable_values,
            self.operation,
            object_type,
            field_details_list,
            self.hide_suggestions,
        )
        return self._analyze_fields(
            object_type,
            collected.grouped_field_set,
            path_prefix,
            multiplier,
            fallback_active,
        )

    def _analyze_abstract_subfields(
        self,
        abstract_type: GraphQLAbstractType,
        field_details_list: FieldDetailsList,
        path_prefix: str,
        multiplier: float,
        fallback_active: bool,
    ) -> _CollectedSubtree:
        # The runtime object type cannot be known before execution, so bill the
        # most expensive possible concrete branch rather than guessing one.
        branches = [
            self._analyze_object_subfields(
                possible_type,
                field_details_list,
                path_prefix,
                multiplier,
                fallback_active,
            )
            for possible_type in self.schema.get_possible_types(abstract_type)
        ]
        return max(branches, key=lambda subtree: subtree.cost,
                   default=_CollectedSubtree(0, [], []))
