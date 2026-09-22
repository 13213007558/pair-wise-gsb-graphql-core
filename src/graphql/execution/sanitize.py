from typing import Any, Callable, Dict, List, Optional, Set, Union

from ..error import GraphQLError
from ..language import (
    BooleanValueNode,
    DocumentNode,
    ListValueNode,
    NullValueNode,
    ObjectValueNode,
    OperationDefinitionNode,
    StringValueNode,
    ValueNode,
    VariableNode,
    Visitor,
    print_ast,
    visit,
)
from ..pyutils import inspect, is_iterable
from ..type import (
    GraphQLArgument,
    GraphQLInputField,
    GraphQLInputType,
    GraphQLSchema,
    get_named_type,
    is_input_object_type,
    is_input_type,
    is_list_type,
    is_non_null_type,
)
from ..utilities import TypeInfo, TypeInfoVisitor, type_from_ast

__all__ = ["ErrorSanitizer"]


OnOriginalErrorCB = Callable[[GraphQLError], None]

SensitiveDefinition = Union[GraphQLArgument, GraphQLInputField]


class ErrorSanitizer:
    """Policy for hiding sensitive input values in public error output.

    Input fields and arguments can be marked as sensitive by setting
    ``extensions={"sensitive": True}`` on their definitions (a custom
    extension key can be chosen via ``sensitive_extension_key``). When an
    ``ErrorSanitizer`` is passed to ``graphql``, ``graphql_sync``,
    ``execute`` or ``execute_sync``, the values of these fields and
    arguments are redacted from the messages and extensions of all errors
    returned in the execution result, whether the values were provided as
    variables or as literals in the query document. This covers nested input
    objects, lists and custom scalars, including previews of parent values
    and messages or extensions raised by custom scalar parsing.

    The redaction only affects the errors returned to the caller. The actual
    coercion of input values is left unchanged and no errors are swallowed.
    Field paths, source locations and error categories (such as extension
    codes) are preserved; only the sensitive values are replaced with a
    fixed marker.

    For internal diagnostics, the original unsanitized errors can be obtained
    via the ``on_original_error`` callback, which is disabled by default
    and only receives the original errors when explicitly configured.

    The sanitizer is immutable and stateless; each request builds its own
    redaction data, so concurrent requests using different sanitizers (or
    none at all) do not affect each other.
    """

    replacement: str
    """The fixed marker that replaces sensitive values in public errors."""

    on_original_error: Optional[OnOriginalErrorCB]
    """Callback receiving the original errors for internal diagnostics."""

    sensitive_extension_key: str
    """The extensions key marking arguments and input fields as sensitive."""

    def __init__(
        self,
        replacement: str = "[hidden]",
        on_original_error: Optional[OnOriginalErrorCB] = None,
        sensitive_extension_key: str = "sensitive",
    ) -> None:
        if not isinstance(replacement, str) or not replacement:
            raise TypeError("Sanitizer replacement must be a non-empty string.")
        if on_original_error is not None and not callable(on_original_error):
            raise TypeError("Sanitizer on_original_error must be callable.")
        self.replacement = replacement
        self.on_original_error = on_original_error
        self.sensitive_extension_key = sensitive_extension_key

    def is_sensitive(self, definition: SensitiveDefinition) -> bool:
        """Check whether an argument or input field is marked as sensitive."""
        extensions = getattr(definition, "extensions", None)
        return bool(extensions and extensions.get(self.sensitive_extension_key))

    def for_request(
        self,
        schema: GraphQLSchema,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
    ) -> "_RequestErrorSanitizer":
        """Create a request-local sanitizer bound to the given request data."""
        return _RequestErrorSanitizer(self, schema, document, variable_values)

    def sanitize_errors(
        self,
        errors: List[GraphQLError],
        schema: GraphQLSchema,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
    ) -> List[GraphQLError]:
        """Sanitize a list of errors for one request in a single step."""
        return self.for_request(schema, document, variable_values).sanitize(errors)


class _RequestErrorSanitizer:
    """Sanitizes the errors of a single request according to a policy.

    The redaction fragments are collected lazily from the request's document
    and variable values, so requests without errors incur no overhead. All
    state is local to the request, which keeps concurrent requests isolated
    from each other.
    """

    def __init__(
        self,
        policy: ErrorSanitizer,
        schema: GraphQLSchema,
        document: DocumentNode,
        variable_values: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.policy = policy
        self.schema = schema
        self.document = document
        self.variable_values = variable_values or {}
        self._fragments: Optional[List[str]] = None

    @property
    def fragments(self) -> List[str]:
        fragments = self._fragments
        if fragments is None:
            collected = _collect_fragments(
                self.policy, self.schema, self.document, self.variable_values
            )
            # Replace longer fragments first so that nested representations
            # (like quoted strings) are redacted before their inner parts.
            self._fragments = fragments = sorted(collected, key=len, reverse=True)
        return fragments

    def sanitize(self, errors: List[GraphQLError]) -> List[GraphQLError]:
        """Return sanitized copies of the given errors.

        The original errors are passed to the ``on_original_error``
        callback of the policy if one has been configured.
        """
        if not errors:
            return errors
        policy = self.policy
        on_original_error = policy.on_original_error
        fragments = self.fragments
        replacement = policy.replacement
        sanitized_errors = []
        for error in errors:
            if on_original_error is not None:
                on_original_error(error)
            sanitized_errors.append(_sanitize_error(error, fragments, replacement))
        return sanitized_errors


def _sanitize_error(
    error: GraphQLError, fragments: List[str], replacement: str
) -> GraphQLError:
    """Create a public copy of an error with sensitive values redacted."""
    message = _scrub_text(error.message, fragments, replacement)
    extensions = (
        _scrub_extensions(error.extensions, fragments, replacement)
        if error.extensions
        else error.extensions
    )
    # The original error is deliberately not carried over, since its message,
    # extensions and traceback may contain the sensitive values. Locations,
    # path and extensions (including the error category) are preserved.
    return GraphQLError(
        message,
        nodes=error.nodes,
        source=error.source,
        positions=error.positions,
        path=error.path,
        extensions=extensions,
    )


def _scrub_text(text: str, fragments: List[str], replacement: str) -> str:
    for fragment in fragments:
        if fragment in text:
            text = text.replace(fragment, replacement)
    return text


def _scrub_extensions(value: Any, fragments: List[str], replacement: str) -> Any:
    if isinstance(value, str):
        return _scrub_text(value, fragments, replacement)
    if isinstance(value, dict):
        return {
            _scrub_extensions(key, fragments, replacement): _scrub_extensions(
                item, fragments, replacement
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(
            _scrub_extensions(item, fragments, replacement) for item in value
        )
    return value


def _collect_fragments(
    policy: ErrorSanitizer,
    schema: GraphQLSchema,
    document: DocumentNode,
    variable_values: Dict[str, Any],
) -> Set[str]:
    """Collect all string representations of sensitive values for a request."""
    fragments: Set[str] = set()
    sensitive_variables: Set[str] = set()
    type_info = TypeInfo(schema)

    class SensitiveValueVisitor(Visitor):
        """Find sensitive arguments and input fields in the document."""

        def enter_argument(self, node: Any, *_args: Any) -> None:
            argument = type_info.get_argument()
            if argument is not None and policy.is_sensitive(argument):
                _collect_literal(node.value, fragments, sensitive_variables)

        def enter_object_field(self, node: Any, *_args: Any) -> None:
            parent_type = get_named_type(type_info.get_parent_input_type())
            if is_input_object_type(parent_type):
                field = parent_type.fields.get(node.name.value)
                if field is not None and policy.is_sensitive(field):
                    _collect_literal(node.value, fragments, sensitive_variables)

    visit(document, TypeInfoVisitor(type_info, SensitiveValueVisitor()))

    # Collect the runtime values of variables that are either used at a
    # sensitive position or have a type containing sensitive input fields.
    for definition in document.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue
        for var_def_node in definition.variable_definitions or ():
            var_name = var_def_node.variable.name.value
            if var_name not in variable_values:
                continue
            var_type = type_from_ast(schema, var_def_node.type)
            if var_type is None or not is_input_type(var_type):
                continue
            _collect_variable_value(
                variable_values[var_name],
                var_type,
                var_name in sensitive_variables,
                policy,
                fragments,
            )

    return fragments


def _collect_literal(
    node: ValueNode, fragments: Set[str], sensitive_variables: Set[str]
) -> None:
    """Collect the printed and raw forms of a sensitive literal value."""
    if isinstance(node, VariableNode):
        sensitive_variables.add(node.name.value)
    elif isinstance(node, StringValueNode):
        if node.value:
            fragments.add(node.value)
            fragments.add(print_ast(node))
    elif isinstance(node, ListValueNode):
        for item_node in node.values:
            _collect_literal(item_node, fragments, sensitive_variables)
    elif isinstance(node, ObjectValueNode):
        for field_node in node.fields:
            _collect_literal(field_node.value, fragments, sensitive_variables)
    elif not isinstance(node, (NullValueNode, BooleanValueNode)):
        # Numbers and enum values are redacted as printed; null and boolean
        # literals are skipped since they carry no sensitive information.
        fragments.add(print_ast(node))


def _collect_variable_value(
    value: Any,
    type_: GraphQLInputType,
    wholly_sensitive: bool,
    policy: ErrorSanitizer,
    fragments: Set[str],
) -> None:
    """Collect the representations of sensitive parts of a variable value."""
    if wholly_sensitive:
        _record_value_deep(value, fragments)
        return
    if is_non_null_type(type_):
        _collect_variable_value(value, type_.of_type, False, policy, fragments)
        return
    if value is None:
        return
    if is_list_type(type_):
        item_type = type_.of_type
        if is_iterable(value):
            for item in value:
                _collect_variable_value(item, item_type, False, policy, fragments)
        else:
            # Lists accept a non-list value as a list of one.
            _collect_variable_value(value, item_type, False, policy, fragments)
        return
    if is_input_object_type(type_):
        if isinstance(value, dict):
            for field_name, field in type_.fields.items():
                if field_name not in value:
                    continue
                field_value = value[field_name]
                if policy.is_sensitive(field):
                    _record_value_deep(field_value, fragments)
                else:
                    _collect_variable_value(
                        field_value, field.type, False, policy, fragments
                    )


def _record_value_deep(value: Any, fragments: Set[str]) -> None:
    """Record all string representations under which a value may appear."""
    if isinstance(value, str):
        if value:
            # Both the raw form (as embedded by custom scalars) and the
            # inspected form (as used in coercion error messages) can appear.
            fragments.add(value)
            fragments.add(inspect(value))
    elif isinstance(value, (bytes, bytearray, memoryview)):
        fragments.add(inspect(value))
    elif isinstance(value, bool) or value is None:
        # Booleans and None carry no sensitive information and redacting
        # their representations would mangle unrelated parts of messages.
        return
    elif isinstance(value, (int, float, complex)):
        fragments.add(inspect(value))
    elif isinstance(value, dict):
        for item in value.values():
            _record_value_deep(item, fragments)
    elif is_iterable(value):
        for item in value:
            _record_value_deep(item, fragments)
    else:
        fragments.add(inspect(value))
