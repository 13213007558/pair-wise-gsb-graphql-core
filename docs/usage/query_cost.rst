Query Cost Estimation
---------------------

.. currentmodule:: graphql.utilities

Before executing an expensive operation, a backend can estimate its static cost
with :func:`estimate_query_cost` and use it for admission control, rejecting
operations that exceed a budget before any resolver runs.

The estimation follows the field collection rules of the GraphQL specification:
fields with the same response key are merged and counted only once, different
aliases are counted separately, ``@skip`` and ``@include`` are evaluated
with the coerced variable values of the current request (including variable
default values), and fragment spreads that are referenced multiple times are
collected only once. For fields returning interface or union types, the
mutually exclusive type branches are not added together; the cost of the most
expensive possible object type is used instead.

Field costs and list multipliers are configured with mappings keyed by
``"TypeName.fieldName"``. List multipliers apply per list layer, so nested
lists multiply layer by layer. Invalid operations raise a
:class:`QueryCostError` instead of returning a deceptively low cost, and the
estimation itself is bounded by configurable cost and effort limits.

Here is an example of a small admission control wrapper::

    from graphql import (
        assert_cost_within_budget,
        execute_sync,
        parse,
    )

    LIST_MULTIPLIERS = {
        "Query.users": 20,
        "User.friends": 10,
    }

    def execute_with_budget(schema, source, budget, variable_values=None):
        document = parse(source)
        assert_cost_within_budget(
            schema,
            document,
            budget,
            variable_values=variable_values,
            list_multipliers=LIST_MULTIPLIERS,
        )
        return execute_sync(schema, document, variable_values=variable_values)

When the estimated cost exceeds the budget, :func:`assert_cost_within_budget`
raises a :class:`QueryCostExceededError` and ``execute_sync`` is never
called, so no resolver runs for rejected operations.

