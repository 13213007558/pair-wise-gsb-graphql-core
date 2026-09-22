from typing import cast, Any, Dict

from ...error import GraphQLError
from ...language import (
    BooleanValueNode,
    EnumValueNode,
    FloatValueNode,
    IntValueNode,
    NonNullTypeNode,
    NullValueNode,
    ListValueNode,
    ObjectFieldNode,
    ObjectValueNode,
    StringValueNode,
    ValueNode,
    VariableDefinitionNode,
    VariableNode,
    VisitorAction,
    SKIP,
    print_ast,
)
from ...pyutils import did_you_mean, suggestion_list, Undefined
from ...type import (
    GraphQLInputObjectType,
    GraphQLScalarType,
    get_named_type,
    get_nullable_type,
    is_input_object_type,
    is_leaf_type,
    is_list_type,
    is_non_null_type,
    is_required_input_field,
)
from . import ValidationContext, ValidationRule

__all__ = ["ValuesOfCorrectTypeRule"]


class ValuesOfCorrectTypeRule(ValidationRule):
    """Value literals of correct type

    A GraphQL document is only valid if all value literals are of the type expected at
    their position.

    See https://spec.graphql.org/draft/#sec-Values-of-Correct-Type
    """

    variable_definitions: Dict[str, VariableDefinitionNode]

    def __init__(self, context: ValidationContext) -> None:
        super().__init__(context)
        self.variable_definitions = {}

    def enter_operation_definition(self, *_args: Any) -> None:
        self.variable_definitions.clear()

    def enter_variable_definition(
        self, node: VariableDefinitionNode, *_args: Any
    ) -> None:
        self.variable_definitions[node.variable.name.value] = node

    def enter_list_value(self, node: ListValueNode, *_args: Any) -> VisitorAction:
        # Note: TypeInfo will traverse into a list's item type, so look to the parent
        # input type to check if it is a list.
        type_ = get_nullable_type(self.context.get_parent_input_type())  # type: ignore
        if not is_list_type(type_):
            self.is_valid_value_node(node)
            return SKIP  # Don't traverse further.
        return None

    def enter_object_value(self, node: ObjectValueNode, *_args: Any) -> VisitorAction:
        type_ = get_named_type(self.context.get_input_type())
        if not is_input_object_type(type_):
            self.is_valid_value_node(node)
            return SKIP  # Don't traverse further.
        type_ = cast(GraphQLInputObjectType, type_)
        # Ensure every required field exists.
        field_node_map = {field.name.value: field for field in node.fields}
        for field_name, field_def in type_.fields.items():
            field_node = field_node_map.get(field_name)
            if not field_node and is_required_input_field(field_def):
                field_type = field_def.type
                self.report_error(
                    GraphQLError(
                        f"Field '{type_.name}.{field_name}' of required type"
                        f" '{field_type}' was not provided.",
                        node,
                    )
                )
        if type_.is_one_of:
            self.validate_one_of_input_object(type_, node, field_node_map)
        return None

    def validate_one_of_input_object(
        self,
        type_: GraphQLInputObjectType,
        node: ObjectValueNode,
        field_node_map: Dict[str, ObjectFieldNode],
    ) -> None:
        keys = list(field_node_map)
        if len(keys) != 1:
            self.report_error(
                GraphQLError(
                    f"OneOf Input Object '{type_.name}'"
                    " must specify exactly one key.",
                    node,
                )
            )
            return

        value = node.fields[0].value

        if isinstance(value, NullValueNode):
            self.report_error(
                GraphQLError(
                    f"Field '{type_.name}.{keys[0]}' must be non-null.", value
                )
            )
            return

        if isinstance(value, VariableNode):
            variable_name = value.name.value
            definition = self.variable_definitions.get(variable_name)
            if definition is None:
                return
            is_nullable_variable = not isinstance(definition.type, NonNullTypeNode)
            contains_default = definition.default_value is not None
            if not is_nullable_variable or contains_default:
                self.report_error(
                    GraphQLError(
                        f"Variable '${variable_name}' must be nullable"
                        " and must not have a default value.",
                        value,
                    )
                )

    def enter_object_field(self, node: ObjectFieldNode, *_args: Any) -> None:
        parent_type = get_named_type(self.context.get_parent_input_type())
        field_type = self.context.get_input_type()
        if not field_type and is_input_object_type(parent_type):
            parent_type = cast(GraphQLInputObjectType, parent_type)
            suggestions = suggestion_list(node.name.value, list(parent_type.fields))
            self.report_error(
                GraphQLError(
                    f"Field '{node.name.value}'"
                    f" is not defined by type '{parent_type.name}'."
                    + did_you_mean(suggestions),
                    node,
                )
            )

    def enter_null_value(self, node: NullValueNode, *_args: Any) -> None:
        type_ = self.context.get_input_type()
        if is_non_null_type(type_):
            self.report_error(
                GraphQLError(
                    f"Expected value of type '{type_}', found {print_ast(node)}.", node
                )
            )

    def enter_enum_value(self, node: EnumValueNode, *_args: Any) -> None:
        self.is_valid_value_node(node)

    def enter_int_value(self, node: IntValueNode, *_args: Any) -> None:
        self.is_valid_value_node(node)

    def enter_float_value(self, node: FloatValueNode, *_args: Any) -> None:
        self.is_valid_value_node(node)

    def enter_string_value(self, node: StringValueNode, *_args: Any) -> None:
        self.is_valid_value_node(node)

    def enter_boolean_value(self, node: BooleanValueNode, *_args: Any) -> None:
        self.is_valid_value_node(node)

    def is_valid_value_node(self, node: ValueNode) -> None:
        """Check whether this is a valid value node.

        Any value literal may be a valid representation of a Scalar, depending on that
        scalar type.
        """
        # Report any error at the full type expected by the location.
        location_type = self.context.get_input_type()
        if not location_type:
            return

        type_ = get_named_type(location_type)

        if not is_leaf_type(type_):
            self.report_error(
                GraphQLError(
                    f"Expected value of type '{location_type}',"
                    f" found {print_ast(node)}.",
                    node,
                )
            )
            return

        # Scalars determine if a literal value is valid via `parse_literal()` which may
        # throw or return an invalid value to indicate failure.
        type_ = cast(GraphQLScalarType, type_)
        try:
            parse_result = type_.parse_literal(node)
            if parse_result is Undefined:
                self.report_error(
                    GraphQLError(
                        f"Expected value of type '{location_type}',"
                        f" found {print_ast(node)}.",
                        node,
                    )
                )
        except GraphQLError as error:
            self.report_error(error)
        except Exception as error:
            self.report_error(
                GraphQLError(
                    f"Expected value of type '{location_type}',"
                    f" found {print_ast(node)}; {error}",
                    node,
                    # Ensure a reference to the original error is maintained.
                    original_error=error,
                )
            )

        return
