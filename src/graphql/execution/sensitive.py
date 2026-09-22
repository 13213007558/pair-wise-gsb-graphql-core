"""Redaction of sensitive values from GraphQL errors.

This module implements an opt-in mechanism that keeps the values of sensitive
arguments and input object fields (such as passwords or tokens) out of the
errors that are returned to clients. Arguments and input fields are marked as
sensitive by setting the ``"sensitive"`` key in their extensions::

    GraphQLArgument(GraphQLString, extensions={"sensitive": True})
    GraphQLInputField(GraphQLString, extensions={"sensitive": True})

The redaction itself is enabled per request by passing a
:class:`SensitiveErrorRedaction` instance as the ``error_redaction`` argument
to :func:`~graphql.graphql`, :func:`~graphql.graphql_sync`,
:func:`~graphql.execution.execute` or :func:`~graphql.execution.execute_sync`.
The same schema can therefore serve redacted public responses and unredacted
internal calls at the same time. Redaction only changes the errors that are
returned to the caller; it never changes coercion results and never swallows
errors. Syntax (parse) errors are not redacted, since no type information is
available at that stage.
"""

from typing import Any, Callable, List, Optional, Set, cast

from ..error import GraphQLError, GraphQLErrorExtensions
from ..language import (
    DocumentNode,
    FloatValueNode,
    IntValueNode,
    ListValueNode,
    NullValueNode,
    ObjectValueNode,
    StringValueNode,
    ValueNode,
    VariableNode,
    Visitor,
    visit,
)
from ..pyutils import Undefined, is_iterable
from ..type import (
    GraphQLInputObjectType,
    GraphQLInputType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLSchema,
    is_input_object_type,
    is_list_type,
    is_non_null_type,
)
from ..utilities.type_info import TypeInfo, TypeInfoVisitor

__all__ = [
    "DEFAULT_REDACTION_MARKER",
    "SENSITIVE_EXTENSION",
    "SensitiveErrorRedaction",
    "SensitiveErrorRedactor",
    "is_sensitive_definition",
]

SENSITIVE_EXTENSION = "sensitive"
"""Name of the extension key used to mark arguments and input fields."""

DEFAULT_REDACTION_MARKER = "<redacted>"
"""Default text that replaces sensitive values in redacted errors."""


def is_sensitive_definition(definition: Any) -> bool:
    """Check whether an argument or input field is marked as sensitive.

    Returns ``True`` if the definition (a
    :class:`~graphql.type.GraphQLArgument` or
    :class:`~graphql.type.GraphQLInputField`) carries the
    :data:`SENSITIVE_EXTENSION` key with a truthy value in its extensions.
    """
    extensions = getattr(definition, "extensions", None)
    return bool(extensions) and bool(extensions.get(SENSITIVE_EXTENSION))


class SensitiveErrorRedaction:
    """Configuration for redacting sensitive values from public errors.

    An instance of this class is an immutable, shareable configuration object.
    It can safely be reused for any number of (also concurrent) requests, since
    all per-request state is kept in a dedicated
    :class:`SensitiveErrorRedactor` that is created internally for each
    request. Options of concurrent calls therefore never influence each other.

    :arg marker: the fixed text that replaces sensitive values in redacted
      error messages and extensions. Defaults to
      :data:`DEFAULT_REDACTION_MARKER`.
    :arg on_original_error: an optional callback that receives each original,
      unredacted error for internal diagnostic purposes. It is only called
      when redaction is enabled for a request, and defaults to ``None``
      (switched off), so original errors never leave the executor unless the
      caller explicitly configures this callback.
    """

    __slots__ = "marker", "on_original_error"

    marker: str
    on_original_error: Optional[Callable[[GraphQLError], None]]

    def __init__(
        self,
        marker: str = DEFAULT_REDACTION_MARKER,
        on_original_error: Optional[Callable[[GraphQLError], None]] = None,
    ) -> None:
        if not isinstance(marker, str) or not marker:
            raise TypeError("The redaction marker must be a non-empty string.")
        if on_original_error is not None and not callable(on_original_error):
            raise TypeError("The on_original_error callback must be callable.")
        self.marker = marker
        self.on_original_error = on_original_error


class SensitiveErrorRedactor:
    """Per-request engine that collects and redacts sensitive values.

    A redactor is created internally for each request that is executed with a
    :class:`SensitiveErrorRedaction` configuration. It collects the raw values
    of sensitive arguments and input fields (from variables as well as from
    literals in the query, including nested input objects, lists and custom
    scalars) and uses them to scrub public error messages and extensions.
    A redactor instance is stateful and must never be shared between
    concurrent requests.
    """

    __slots__ = "marker", "on_original_error", "secrets", "sensitive_variables"

    marker: str
    on_original_error: Optional[Callable[[GraphQLError], None]]
    secrets: Set[str]
    sensitive_variables: Set[str]

    def __init__(self, redaction: SensitiveErrorRedaction) -> None:
        self.marker = redaction.marker
        self.on_original_error = redaction.on_original_error
        self.secrets = set()
        self.sensitive_variables = set()

    # -- collection of sensitive values ------------------------------------

    def add_secret(self, value: str) -> None:
        """Register a raw string value that must not appear in public errors."""
        if not value:
            return
        forms = {value}
        # Values are often embedded via repr()/inspect(), which escapes some
        # characters, so register the escaped form as well.
        escaped = repr(value)[1:-1]
        if escaped != value:
            forms.add(escaped)
        for form in forms:
            self.secrets.add(form)
            # Long values are truncated by inspect(), keeping only the start
            # and the end; register these visible fragments as well.
            if len(form) > 237:
                self.secrets.add(form[:117])
                self.secrets.add(form[-118:])

    def collect_value(
        self, value: Any, type_: GraphQLInputType, sensitive: bool = False
    ) -> None:
        """Collect sensitive values within a raw (variable) input value.

        Traverses the given Python value along the given input type and
        registers all leaf values that are marked as sensitive, either
        directly or via an enclosing input object field.
        """
        if is_non_null_type(type_):
            self.collect_value(value, cast(GraphQLNonNull, type_).of_type, sensitive)
            return
        if value is None or value is Undefined:
            return
        if sensitive:
            self._collect_raw_value(value)
            return
        if is_list_type(type_):
            item_type = cast(GraphQLList, type_).of_type
            if is_iterable(value):
                for item in value:
                    self.collect_value(item, item_type)
            else:
                # Lists accept a non-list value as a list of one.
                self.collect_value(value, item_type)
            return
        if is_input_object_type(type_) and isinstance(value, dict):
            fields = cast(GraphQLInputObjectType, type_).fields
            for field_name, field in fields.items():
                if field_name in value:
                    self.collect_value(
                        value[field_name], field.type, is_sensitive_definition(field)
                    )

    def _collect_raw_value(self, value: Any) -> None:
        """Register all scalar leaf values within an arbitrary Python value."""
        if isinstance(value, str):
            self.add_secret(value)
        elif isinstance(value, bool) or value is None or value is Undefined:
            return
        elif isinstance(value, (int, float)):
            self.add_secret(str(value))
        elif isinstance(value, dict):
            for key, item in value.items():
                self._collect_raw_value(key)
                self._collect_raw_value(item)
        elif is_iterable(value):
            for item in value:
                self._collect_raw_value(item)

    def collect_literal(
        self, node: ValueNode, type_: GraphQLInputType, sensitive: bool = False
    ) -> None:
        """Collect sensitive values within a literal AST node.

        Traverses the given value node along the given input type and
        registers all literal leaf values that are marked as sensitive.
        Variables used at sensitive positions are registered as sensitive
        variables, so that their runtime values are redacted as well.
        """
        if is_non_null_type(type_):
            self.collect_literal(node, cast(GraphQLNonNull, type_).of_type, sensitive)
            return
        if isinstance(node, VariableNode):
            if sensitive:
                self.sensitive_variables.add(node.name.value)
            return
        if isinstance(node, NullValueNode):
            return
        if sensitive:
            self._collect_literal_value(node)
            return
        if is_list_type(type_):
            item_type = cast(GraphQLList, type_).of_type
            if isinstance(node, ListValueNode):
                for item in node.values:
                    self.collect_literal(item, item_type)
            else:
                # Lists accept a non-list value as a list of one.
                self.collect_literal(node, item_type)
            return
        if is_input_object_type(type_) and isinstance(node, ObjectValueNode):
            fields = cast(GraphQLInputObjectType, type_).fields
            for field_node in node.fields:
                field = fields.get(field_node.name.value)
                if field is not None:
                    self.collect_literal(
                        field_node.value, field.type, is_sensitive_definition(field)
                    )

    def _collect_literal_value(self, node: ValueNode) -> None:
        """Register all literal leaf values within an AST node."""
        if isinstance(node, (StringValueNode, IntValueNode, FloatValueNode)):
            self.add_secret(node.value)
        elif isinstance(node, VariableNode):
            self.sensitive_variables.add(node.name.value)
        elif isinstance(node, ListValueNode):
            for item in node.values:
                self._collect_literal_value(item)
        elif isinstance(node, ObjectValueNode):
            for field_node in node.fields:
                self._collect_literal_value(field_node.value)

    def collect_document(self, schema: GraphQLSchema, document: DocumentNode) -> None:
        """Collect sensitive literals and variable usages from a document.

        Walks the given document with type information and collects the values
        of all literals that are used at sensitive argument or input field
        positions, as well as the names of variables used at such positions.
        """
        redactor = self
        type_info = TypeInfo(schema)

        class SensitiveArgumentVisitor(Visitor):
            def enter_argument(self, node: Any, *_args: Any) -> None:
                arg_def = type_info.get_argument()
                if arg_def is not None:
                    redactor.collect_literal(
                        node.value, arg_def.type, is_sensitive_definition(arg_def)
                    )

        visit(document, TypeInfoVisitor(type_info, SensitiveArgumentVisitor()))

    # -- redaction of errors ------------------------------------------------

    def scrub_text(self, text: str) -> str:
        """Replace all known sensitive values in the given text."""
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, self.marker)
        return text

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, dict):
            return {
                self._scrub_value(key): self._scrub_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._scrub_value(item) for item in value]
        return value

    def redact_error(self, error: GraphQLError) -> GraphQLError:
        """Return a redacted copy of the given error if it contains secrets.

        The redacted error keeps the message (with sensitive values replaced
        by the marker), the locations, the path and the (scrubbed) extensions
        of the original error, and is marked with a fixed ``"redacted"``
        flag in its extensions. The original error is dropped, so internal
        details can only be retrieved via the ``on_original_error``
        callback. Errors that do not contain any sensitive values are
        returned unchanged.
        """
        message = self.scrub_text(error.message)
        extensions = error.extensions
        scrubbed_extensions = (
            cast(GraphQLErrorExtensions, self._scrub_value(dict(extensions)))
            if extensions
            else extensions
        )
        if message == error.message and scrubbed_extensions == extensions:
            return error
        redacted_extensions: GraphQLErrorExtensions = (
            dict(scrubbed_extensions) if scrubbed_extensions else {}
        )
        redacted_extensions["redacted"] = True
        return GraphQLError(
            message,
            nodes=error.nodes,
            source=error.source,
            positions=error.positions,
            path=error.path,
            extensions=redacted_extensions,
        )

    def redact_errors(self, errors: List[GraphQLError]) -> List[GraphQLError]:
        """Redact a list of errors, reporting originals to the callback.

        If an ``on_original_error`` callback has been configured, it is
        called with each original error before redaction. Returns the list of
        redacted errors; the list always has the same length as the input.
        """
        on_original_error = self.on_original_error
        if on_original_error is not None:
            for error in errors:
                on_original_error(error)
        return [self.redact_error(error) for error in errors]
