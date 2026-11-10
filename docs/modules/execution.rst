Execution
=========

.. currentmodule:: graphql.execution

.. automodule:: graphql.execution
   :no-members:
   :no-inherited-members:

.. autofunction:: execute

.. autofunction:: execute_root_selection_set

.. autofunction:: experimental_execute_incrementally

.. autofunction:: execute_sync

.. autofunction:: default_field_resolver

.. autofunction:: default_type_resolver

.. autoclass:: Executor

.. autoclass:: ExecutionHooks

.. autoclass:: AsyncWorkFinishedInfo

.. autoexception:: AbortedGraphQLExecutionError

.. autoclass:: ExecutionResult

.. autoclass:: FormattedExecutionResult
   :no-inherited-members:

.. autoclass:: ExperimentalIncrementalExecutionResults

.. autoclass:: InitialIncrementalExecutionResult

.. autoclass:: FormattedInitialIncrementalExecutionResult
   :no-inherited-members:

.. autoclass:: SubsequentIncrementalExecutionResult

.. autoclass:: FormattedSubsequentIncrementalExecutionResult
   :no-inherited-members:

.. autoclass:: IncrementalDeferResult

.. autoclass:: FormattedIncrementalDeferResult
   :no-inherited-members:

.. autoclass:: IncrementalStreamResult

.. autoclass:: FormattedIncrementalStreamResult
   :no-inherited-members:

.. autoclass:: IncrementalResult

.. autoclass:: FormattedIncrementalResult
   :no-inherited-members:

.. autoclass:: PendingResult

.. autoclass:: FormattedPendingResult
   :no-inherited-members:

.. autoclass:: CompletedResult

.. autofunction:: subscribe

.. autofunction:: execute_subscription_event

.. autofunction:: create_source_event_stream

.. autofunction:: map_source_to_response_event

.. autofunction:: map_async_iterable

.. autoclass:: RootSelectionSetExecutor

.. autoclass:: Middleware

.. autoclass:: MiddlewareManager

.. autofunction:: get_argument_values

.. autofunction:: get_directive_values

.. autofunction:: get_variable_values

.. autoclass:: VariableValues

Pre-execution cost analysis
---------------------------

.. autoclass:: CostLimit

.. autoclass:: FieldCost

.. autoclass:: FieldCostInfo

.. autoclass:: CostCallback

.. autoclass:: OperationCost

.. autoclass:: CostBreakdownEntry

.. autodata:: COST_EXTENSION_KEY

.. autofunction:: get_operation_cost

.. autofunction:: check_cost_limit

.. autofunction:: default_cost_callback
