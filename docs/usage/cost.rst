Operation Cost and Budgets
--------------------------

.. currentmodule:: graphql.execution

Use :func:`get_operation_cost` before an expensive query reaches a resolver. The
analysis follows GraphQL field collection: compatible fields with the same response
key are merged, aliases are charged separately, repeated fragment spreads count once,
and ``@skip``/``@include`` use the supplied variables and operation default values.
Abstract type selections are charged for the most expensive concrete type that a single
object may match.

Field weights default to one and can be declared with the field extension ``cost``.
List multipliers may be supplied with a callback. A multiplier is only applied to the
sub-selection inside the list; nested list dimensions multiply.

.. code-block:: python

    from graphql import GraphQLField, GraphQLList, GraphQLString, parse
    from graphql.execution import (
        CostFieldContext,
        CostOptions,
        execute,
        get_operation_cost,
    )

    def list_multiplier(context: CostFieldContext) -> int | None:
        first = context.args.get("first")
        if isinstance(first, int) and not isinstance(first, bool):
            return first
        return context.field.extensions.get("listMultiplier")

    options = CostOptions(list_multiplier=list_multiplier)
    document = parse(
        """
        query Users($first: Int = 2) {
          users(first: $first) {
            name
          }
        }
        """
    )
    cost = get_operation_cost(schema, document, options=options)
    if cost.total_cost <= 100:
        result = execute(schema, document, variable_values={"first": 2})

For request handlers, :func:`execute_with_cost_budget` performs the admission check
before calling :func:`execute`. A rejected request raises
:class:`CostBudgetExceededError` and no resolver runs.

.. code-block:: python

    from graphql.execution import execute_with_cost_budget

    cost, result = execute_with_cost_budget(
        schema=schema,
        document=document,
        variable_values={"first": 2},
        budget=100,
        cost_options=options,
    )

Invalid documents, invalid variables, cyclic fragments, illegal multiplier values and
costs larger than the configured safety limits raise :class:`CostAnalysisError`
instead of returning a misleading low cost.
