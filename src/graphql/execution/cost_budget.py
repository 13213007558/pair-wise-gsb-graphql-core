"""Execute operations admitted by a static cost budget."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeGuard

from .cost import (
    CostOptions,
    OperationCost,
    assert_within_cost_budget,
    get_operation_cost,
)
from .execute import execute

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..language import DocumentNode
    from ..type import GraphQLFieldResolver, GraphQLSchema, GraphQLTypeResolver
    from .execute import Middleware
    from .types import ExecutionResult

__all__ = ["execute_with_cost_budget"]


def execute_with_cost_budget(
    *,
    schema: GraphQLSchema,
    document: DocumentNode,
    budget: int,
    variable_values: dict[str, Any] | None = None,
    operation_name: str | None = None,
    root_value: Any = None,
    context_value: Any = None,
    field_resolver: GraphQLFieldResolver | None = None,
    type_resolver: GraphQLTypeResolver | None = None,
    middleware: Middleware | None = None,
    cost_options: CostOptions | None = None,
    is_awaitable: Callable[[Any], TypeGuard[Awaitable]] | None = None,
) -> tuple[OperationCost, ExecutionResult | Awaitable[ExecutionResult]]:
    """Validate the cost budget before any resolver can run.

    Returns the calculated cost together with the normal result of
    :func:`graphql.execution.execute`. The budget check completes before execution
    starts, so a rejected operation never invokes a resolver.
    """
    cost = get_operation_cost(
        schema,
        document,
        variable_values,
        operation_name,
        cost_options,
    )
    assert_within_cost_budget(cost, budget)
    result = execute(
        schema,
        document,
        root_value=root_value,
        context_value=context_value,
        variable_values=variable_values,
        operation_name=operation_name,
        field_resolver=field_resolver,
        type_resolver=type_resolver,
        middleware=middleware,
        is_awaitable=is_awaitable,
    )
    return cost, result
