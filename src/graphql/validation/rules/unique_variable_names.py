from operator import attrgetter
from typing import Any, Tuple

from ...error import GraphQLError
from ...language import (
    FragmentDefinitionNode,
    OperationDefinitionNode,
    VariableDefinitionNode,
)
from ...pyutils import group_by
from . import ASTValidationRule

__all__ = ["UniqueVariableNamesRule"]


class UniqueVariableNamesRule(ASTValidationRule):
    """Unique variable names

    A GraphQL operation is only valid if all its variables are uniquely named.
    """

    def enter_operation_definition(
        self, node: OperationDefinitionNode, *_args: Any
    ) -> None:
        self.check_variable_uniqueness(node.variable_definitions)

    def enter_fragment_definition(
        self, node: FragmentDefinitionNode, *_args: Any
    ) -> None:
        # Experimental: fragment variable definitions (fragment arguments)
        self.check_variable_uniqueness(node.variable_definitions)

    def check_variable_uniqueness(
        self, variable_definitions: Tuple[VariableDefinitionNode, ...]
    ) -> None:
        if not variable_definitions:
            return
        seen_variable_definitions = group_by(
            variable_definitions, attrgetter("variable.name.value")
        )

        for variable_name, variable_nodes in seen_variable_definitions.items():
            if len(variable_nodes) > 1:
                self.report_error(
                    GraphQLError(
                        f"There can be only one variable named '${variable_name}'.",
                        [node.variable.name for node in variable_nodes],
                    )
                )
