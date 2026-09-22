from enum import Enum
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from ..language import DirectiveNode, print_ast
from ..pyutils import Undefined, inspect
from ..type import (
    GraphQLDirective,
    GraphQLSchema,
    GraphQLType,
    is_enum_type,
    is_input_object_type,
    is_interface_type,
    is_object_type,
)
from ..type.validate import assert_valid_schema
from ..utilities.sort_value_node import sort_value_node
from .ast_from_value import ast_from_value
from .value_from_ast import value_from_ast
from .value_from_ast_untyped import value_from_ast_untyped

__all__ = [
    "AppliedDirectiveChange",
    "AppliedDirectiveChangeType",
    "AppliedDirectiveChanges",
    "find_applied_directive_changes",
]


class AppliedDirectiveChangeType(Enum):
    APPLIED_DIRECTIVE_ADDED = 10
    APPLIED_DIRECTIVE_REMOVED = 20
    APPLIED_DIRECTIVE_ARGUMENTS_CHANGED = 30


class AppliedDirectiveChange(NamedTuple):
    """A structured record of a single applied-directive change.

    The ``coordinate`` locates the schema element the directive is applied to,
    using schema coordinates (``Type``, ``Type.field``, ``Type.field(arg:)``,
    ``Enum.VALUE``, ``Input.field``); the schema itself uses ``schema``.

    ``occurrence`` is the zero-based index of the directive occurrence at this
    location. It is always 0 for non-repeatable directives, while repeatable
    directives keep their count and order of appearance.

    ``old_arguments`` and ``new_arguments`` hold the directive arguments after
    coercion, with defaults from the directive definition filled in. The side
    on which the directive occurrence does not exist is ``None``.

    ``severity`` is whatever the caller-provided classification callback
    returned for this change (``None`` when no callback was given). The
    library itself never assigns severities.
    """

    type: AppliedDirectiveChangeType
    coordinate: str
    directive_name: str
    occurrence: int
    old_arguments: Optional[Dict[str, Any]]
    new_arguments: Optional[Dict[str, Any]]
    description: str
    severity: Any = None


class AppliedDirectiveChanges(NamedTuple):
    """Result of comparing applied directives between two schemas.

    ``is_complete`` is False when at least one of the schemas carries no
    applied-directive information at all (for instance a schema built from an
    introspection result). In that case no comparison is performed and
    ``changes`` is empty, instead of reporting every directive as removed.
    """

    changes: List[AppliedDirectiveChange]
    is_complete: bool


ClassifyAppliedDirectiveChange = Callable[[AppliedDirectiveChange], Any]


def find_applied_directive_changes(
    old_schema: GraphQLSchema,
    new_schema: GraphQLSchema,
    classify_change: Optional[ClassifyAppliedDirectiveChange] = None,
) -> AppliedDirectiveChanges:
    """Find changes in applied directives between two schemas.

    Compares the usages of directives (including their arguments) on the
    schema, types, fields, arguments, input fields and enum values of two
    schemas, including usages contributed by schema and type extensions.

    Both schemas must be valid; invalid directive definitions surface as the
    usual schema validation errors. Locations that only exist in one of the
    schemas (removed or added types, fields, ...) are not compared; those
    changes are covered by ``find_breaking_changes``.

    The optional ``classify_change`` callback is invoked with each detected
    change and its return value is stored as the change's ``severity``,
    allowing callers to apply their own business-specific classification.
    """
    assert_valid_schema(old_schema)
    assert_valid_schema(new_schema)

    if not _has_applied_directive_info(old_schema) or not _has_applied_directive_info(
        new_schema
    ):
        return AppliedDirectiveChanges([], False)

    old_usages = _collect_applied_directives(old_schema)
    new_usages = _collect_applied_directives(new_schema)

    changes: List[AppliedDirectiveChange] = []
    for coordinate, old_nodes in old_usages.items():
        new_nodes = new_usages.get(coordinate)
        if new_nodes is None:
            # The location itself was removed; this is reported as a breaking
            # change of the corresponding type, field or argument already.
            continue
        _diff_directive_usages(
            old_schema, new_schema, coordinate, old_nodes, new_nodes, changes
        )

    if classify_change is not None:
        changes = [
            change._replace(severity=classify_change(change)) for change in changes
        ]

    return AppliedDirectiveChanges(changes, True)


def _has_applied_directive_info(schema: GraphQLSchema) -> bool:
    """Check whether a schema carries applied-directive information.

    Schemas built from SDL keep the AST nodes of their definitions and
    extensions, while schemas built from introspection results do not.
    """
    if schema.ast_node is not None or schema.extension_ast_nodes:
        return True
    return any(
        type_.ast_node is not None or type_.extension_ast_nodes
        for type_ in schema.type_map.values()
    )


def _collect_applied_directives(
    schema: GraphQLSchema,
) -> Dict[str, List[DirectiveNode]]:
    """Collect applied directive nodes per schema coordinate, in order."""
    usages: Dict[str, List[DirectiveNode]] = {}

    def add(coordinate: str, nodes: Tuple[Any, ...]) -> None:
        collected = usages.setdefault(coordinate, [])
        for node in nodes:
            if node is not None:
                collected.extend(node.directives or ())

    add("schema", (schema.ast_node, *schema.extension_ast_nodes))

    for type_name, type_ in schema.type_map.items():
        if type_name.startswith("__"):
            continue
        add(type_name, (type_.ast_node, *type_.extension_ast_nodes))
        if is_object_type(type_) or is_interface_type(type_):
            for field_name, field in type_.fields.items():
                field_coordinate = f"{type_name}.{field_name}"
                add(field_coordinate, (field.ast_node,))
                for arg_name, arg in field.args.items():
                    add(f"{field_coordinate}({arg_name}:)", (arg.ast_node,))
        elif is_input_object_type(type_):
            for field_name, field in type_.fields.items():
                add(f"{type_name}.{field_name}", (field.ast_node,))
        elif is_enum_type(type_):
            for value_name, value in type_.values.items():
                add(f"{type_name}.{value_name}", (value.ast_node,))

    return usages


def _diff_directive_usages(
    old_schema: GraphQLSchema,
    new_schema: GraphQLSchema,
    coordinate: str,
    old_nodes: List[DirectiveNode],
    new_nodes: List[DirectiveNode],
    changes: List[AppliedDirectiveChange],
) -> None:
    old_by_name = _group_by_name(old_nodes)
    new_by_name = _group_by_name(new_nodes)

    names = list(old_by_name) + [
        name for name in new_by_name if name not in old_by_name
    ]
    for name in names:
        old_occurrences = old_by_name.get(name, [])
        new_occurrences = new_by_name.get(name, [])
        old_directive = old_schema.get_directive(name)
        new_directive = new_schema.get_directive(name)

        for occurrence in range(max(len(old_occurrences), len(new_occurrences))):
            old_node = (
                old_occurrences[occurrence]
                if occurrence < len(old_occurrences)
                else None
            )
            new_node = (
                new_occurrences[occurrence]
                if occurrence < len(new_occurrences)
                else None
            )
            old_arguments = (
                _normalized_arguments(old_node, old_directive)
                if old_node is not None
                else None
            )
            new_arguments = (
                _normalized_arguments(new_node, new_directive)
                if new_node is not None
                else None
            )

            if old_node is None:
                change_type = AppliedDirectiveChangeType.APPLIED_DIRECTIVE_ADDED
                description = (
                    f"Directive @{name} was applied to {coordinate}."
                )
            elif new_node is None:
                change_type = AppliedDirectiveChangeType.APPLIED_DIRECTIVE_REMOVED
                description = (
                    f"Directive @{name} was removed from {coordinate}."
                )
            elif _arguments_key(old_arguments, old_directive) != _arguments_key(
                new_arguments, new_directive
            ):
                change_type = (
                    AppliedDirectiveChangeType.APPLIED_DIRECTIVE_ARGUMENTS_CHANGED
                )
                description = (
                    f"Arguments of directive @{name} on {coordinate} changed."
                )
            else:
                continue

            changes.append(
                AppliedDirectiveChange(
                    type=change_type,
                    coordinate=coordinate,
                    directive_name=name,
                    occurrence=occurrence,
                    old_arguments=old_arguments,
                    new_arguments=new_arguments,
                    description=description,
                )
            )


def _group_by_name(
    nodes: List[DirectiveNode],
) -> Dict[str, List[DirectiveNode]]:
    """Group directive nodes by name, preserving the order of appearance.

    Repeatable directives occur multiple times and must not be collapsed
    into a dictionary keyed by directive name alone.
    """
    grouped: Dict[str, List[DirectiveNode]] = {}
    for node in nodes:
        grouped.setdefault(node.name.value, []).append(node)
    return grouped


def _normalized_arguments(
    node: DirectiveNode, directive: Optional[GraphQLDirective]
) -> Dict[str, Any]:
    """Coerce directive arguments, filling in defaults from the definition."""
    provided = {
        arg.name.value: arg.value for arg in node.arguments or ()
    }
    if directive is None:
        # The directive definition is unknown; compare the provided
        # arguments as untyped values without applying any defaults.
        return {
            name: value_from_ast_untyped(value_node)
            for name, value_node in provided.items()
        }
    normalized: Dict[str, Any] = {}
    for arg_name, arg in directive.args.items():
        if arg_name in provided:
            normalized[arg_name] = value_from_ast(provided[arg_name], arg.type)
        elif arg.default_value is not Undefined:
            normalized[arg_name] = arg.default_value
    return normalized


def _arguments_key(
    arguments: Optional[Dict[str, Any]], directive: Optional[GraphQLDirective]
) -> Optional[Dict[str, str]]:
    """Build a canonical comparable representation of directive arguments.

    Input object fields are order-insensitive, while list order is
    significant. Values are compared via their printed AST so that enum
    values compare by name across the two schemas.
    """
    if arguments is None:
        return None
    return {
        arg_name: _argument_key(
            value,
            directive.args[arg_name].type
            if directive is not None and arg_name in directive.args
            else None,
        )
        for arg_name, value in arguments.items()
    }


def _argument_key(value: Any, type_: Optional[GraphQLType]) -> str:
    if value is Undefined:
        return "Undefined"
    value_node = ast_from_value(value, type_)
    if value_node is None:
        return inspect(value)
    return print_ast(sort_value_node(value_node))
