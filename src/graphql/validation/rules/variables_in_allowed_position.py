from typing import Any, Dict, Optional, cast

from ...error import GraphQLError
from ...language import (
    FragmentDefinitionNode,
    NullValueNode,
    OperationDefinitionNode,
    ValueNode,
    VariableDefinitionNode,
    VariableNode,
)
from ...pyutils import Undefined
from ...type import (
    GraphQLInputType,
    GraphQLNonNull,
    GraphQLSchema,
    GraphQLType,
    is_non_null_type,
)
from ...utilities import type_from_ast, is_type_sub_type_of
from . import ValidationContext, ValidationRule

__all__ = ["VariablesInAllowedPositionRule"]


class VariablesInAllowedPositionRule(ValidationRule):
    """Variables in allowed position

    Variable usages must be compatible with the arguments they are passed to.

    See https://spec.graphql.org/draft/#sec-All-Variable-Usages-are-Allowed
    """

    def __init__(self, context: ValidationContext):
        super().__init__(context)
        self.var_def_map: Dict[str, VariableDefinitionNode] = {}
        self.in_operation = False

    def enter_operation_definition(self, *_args: Any) -> None:
        self.var_def_map.clear()
        self.in_operation = True

    def leave_operation_definition(
        self, operation: OperationDefinitionNode, *_args: Any
    ) -> None:
        self.in_operation = False
        var_def_map = self.var_def_map
        usages = self.context.get_recursive_variable_usages(operation)

        for usage in usages:
            node, type_ = usage.node, usage.type
            var_name = node.name.value
            var_def = var_def_map.get(var_name)
            if var_def and type_:
                self.check_variable_usage(var_def, node, type_, usage.default_value)

    def leave_fragment_definition(
        self, fragment: FragmentDefinitionNode, *_args: Any
    ) -> None:
        # Experimental: variables that are declared as fragment parameters
        # (fragment arguments feature) are checked against the variable
        # definitions of the fragment itself instead of the operation.
        var_def_nodes = fragment.variable_definitions
        if not var_def_nodes:
            return
        var_def_map = {
            var_def.variable.name.value: var_def for var_def in var_def_nodes
        }
        for usage in self.context.get_variable_usages(fragment):
            node, type_ = usage.node, usage.type
            var_def = var_def_map.get(node.name.value)
            if var_def and type_:
                self.check_variable_usage(var_def, node, type_, usage.default_value)

    def check_variable_usage(
        self,
        var_def: VariableDefinitionNode,
        node: VariableNode,
        type_: GraphQLInputType,
        default_value: Any,
    ) -> None:
        # A var type is allowed if it is the same or more strict (e.g. is a
        # subtype of) than the expected type. It can be more strict if the
        # variable type is non-null when the expected type is nullable. If both
        # are list types, the variable item type can be more strict than the
        # expected item type (contravariant).
        schema = self.context.schema
        var_name = var_def.variable.name.value
        var_type = type_from_ast(schema, var_def.type)
        if var_type and not allowed_variable_usage(
            schema, var_type, var_def.default_value, type_, default_value
        ):
            self.report_error(
                GraphQLError(
                    f"Variable '${var_name}' of type '{var_type}' used"
                    f" in position expecting type '{type_}'.",
                    [var_def, node],
                )
            )

    def enter_variable_definition(
        self, node: VariableDefinitionNode, *_args: Any
    ) -> None:
        if self.in_operation:
            self.var_def_map[node.variable.name.value] = node


def allowed_variable_usage(
    schema: GraphQLSchema,
    var_type: GraphQLType,
    var_default_value: Optional[ValueNode],
    location_type: GraphQLType,
    location_default_value: Any,
) -> bool:
    """Check for allowed variable usage.

    Returns True if the variable is allowed in the location it was found, which includes
    considering if default values exist for either the variable or the location at which
    it is located.
    """
    if is_non_null_type(location_type) and not is_non_null_type(var_type):
        has_non_null_variable_default_value = (
            var_default_value is not None
            and not isinstance(var_default_value, NullValueNode)
        )
        has_location_default_value = location_default_value is not Undefined
        if not has_non_null_variable_default_value and not has_location_default_value:
            return False
        location_type = cast(GraphQLNonNull, location_type)
        nullable_location_type = location_type.of_type
        return is_type_sub_type_of(schema, var_type, nullable_location_type)
    return is_type_sub_type_of(schema, var_type, location_type)
