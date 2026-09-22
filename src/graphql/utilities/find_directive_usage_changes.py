from enum import Enum
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from ..language import DirectiveNode
from ..pyutils import Undefined
from ..type import (
    GraphQLDirective,
    GraphQLNamedType,
    GraphQLSchema,
    is_enum_type,
    is_input_object_type,
    is_interface_type,
    is_introspection_type,
    is_object_type,
)
from ..type.validate import assert_valid_schema
from .value_from_ast import value_from_ast
from .value_from_ast_untyped import value_from_ast_untyped

__all__ = [
    "DirectiveUsageChange",
    "DirectiveUsageChangeType",
    "DirectiveUsageChanges",
    "find_directive_usage_changes",
]

SCHEMA_COORDINATE = "Schema"


class DirectiveUsageChangeType(Enum):
    """Kind of difference between two applied directive usages."""

    ADDED = 1
    REMOVED = 2
    ARGUMENTS_CHANGED = 3


class DirectiveUsageChange(NamedTuple):
    """A single difference between two applied directive usages.

    Coordinates use schema coordinates (see the GraphQL specification),
    except for directives applied to the schema itself which are located at
    the special coordinate "Schema".

    old_index and new_index give the zero-based position of the directive
    usage within the repeatable directive's occurrence list on the respective
    side. old_arguments and new_arguments contain the coerced argument
    values, including the default values taken from the directive definition.

    The library does not assign a severity to a change; callers decide how to
    classify the returned records themselves.
    """

    type: DirectiveUsageChangeType
    coordinate: str
    directive_name: str
    old_arguments: Optional[Dict[str, Any]] = None
    new_arguments: Optional[Dict[str, Any]] = None
    old_index: Optional[int] = None
    new_index: Optional[int] = None


class DirectiveUsageChanges(NamedTuple):
    """Result of comparing the applied directives of two schemas.

    is_incomplete is True when at least one of the schemas does not carry
    full applied-directive information, for example because it was built from
    an introspection result. In that case changes is always empty: the missing
    directives cannot be distinguished from removed ones.
    """

    is_incomplete: bool
    changes: List[DirectiveUsageChange]


def find_directive_usage_changes(
    old_schema: GraphQLSchema, new_schema: GraphQLSchema
) -> DirectiveUsageChanges:
    """Find differences between applied directives.

    Given two schemas, compares the directives applied to the schema, its
    types, fields, arguments, input fields and enum values, including
    directives added through schema and type extensions.

    Applied directives are located by schema coordinate. Repeatable directive
    occurrences keep both their multiplicity and their relative order. The
    arguments of each usage are compared after the default values of the
    directive definition have been filled in; input object arguments are
    compared independent of their field order, while list order is
    significant.

    The records do not carry a severity such as "breaking" or "dangerous".
    The caller is responsible for classifying them according to its own
    rules, so the library makes no assumptions about the meaning of specific
    directives.

    Invalid schema definitions raise the same validation errors as
    assert_valid_schema.

    If either schema was built from an introspection result or in another way
    that does not retain applied directives, the result is marked incomplete
    and contains no changes.
    """
    # Invalid directive definitions (unknown directives, wrong locations and
    # similar problems) surface through the normal schema validation errors.
    assert_valid_schema(old_schema)
    assert_valid_schema(new_schema)

    if not (
        _has_applied_directive_info(old_schema)
        and _has_applied_directive_info(new_schema)
    ):
        return DirectiveUsageChanges(True, [])

    old_directives = {directive.name: directive for directive in old_schema.directives}
    new_directives = {directive.name: directive for directive in new_schema.directives}

    changes: List[DirectiveUsageChange] = []

    old_locations = _collect_directive_locations(old_schema)
    new_locations = _collect_directive_locations(new_schema)

    for coordinate in sorted(set(old_locations) | set(new_locations)):
        old_usages = _normalize_directives(
            old_locations.get(coordinate, ()), old_directives
        )
        new_usages = _normalize_directives(
            new_locations.get(coordinate, ()), new_directives
        )
        changes.extend(_diff_usages(coordinate, old_usages, new_usages))

    return DirectiveUsageChanges(False, changes)


def _node_directives(node: Any) -> Tuple[DirectiveNode, ...]:
    return node.directives if node and node.directives else ()


def _schema_directive_nodes(schema: GraphQLSchema) -> Tuple[DirectiveNode, ...]:
    return (
        *_node_directives(schema.ast_node),
        *(
            directive
            for extension_node in schema.extension_ast_nodes or ()
            for directive in _node_directives(extension_node)
        ),
    )


def _type_directive_nodes(type_: GraphQLNamedType) -> Tuple[DirectiveNode, ...]:
    return (
        *_node_directives(type_.ast_node),
        *(
            directive
            for extension_node in type_.extension_ast_nodes
            for directive in _node_directives(extension_node)
        ),
    )


def _collect_directive_locations(
    schema: GraphQLSchema,
) -> Dict[str, Tuple[DirectiveNode, ...]]:
    """Collect applied directives at every comparable schema coordinate."""
    locations: Dict[str, Tuple[DirectiveNode, ...]] = {
        SCHEMA_COORDINATE: _schema_directive_nodes(schema)
    }

    for type_name in sorted(schema.type_map):
        type_ = schema.type_map[type_name]
        if is_introspection_type(type_):
            continue

        locations[type_name] = _type_directive_nodes(type_)

        if is_object_type(type_) or is_interface_type(type_):
            for field_name in sorted(type_.fields):
                field = type_.fields[field_name]
                field_coordinate = f"{type_name}.{field_name}"
                locations[field_coordinate] = _node_directives(field.ast_node)

                for arg_name in sorted(field.args):
                    arg = field.args[arg_name]
                    locations[
                        f"{field_coordinate}({arg_name})"
                    ] = _node_directives(arg.ast_node)

        elif is_input_object_type(type_):
            for field_name in sorted(type_.fields):
                input_field = type_.fields[field_name]
                locations[
                    f"{type_name}.{field_name}"
                ] = _node_directives(input_field.ast_node)

        elif is_enum_type(type_):
            for value_name in sorted(type_.values):
                value = type_.values[value_name]
                locations[
                    f"{type_name}.{value_name}"
                ] = _node_directives(value.ast_node)

    return locations


def _has_applied_directive_info(schema: GraphQLSchema) -> bool:
    """Check whether a schema retains information about applied directives.

    Schemas parsed from SDL keep AST nodes on the schema and its members,
    while schemas built from introspection results (or constructed purely in
    Python without AST nodes) cannot carry applied-directive information.
    """
    if schema.ast_node is not None or schema.extension_ast_nodes:
        return True

    for type_ in schema.type_map.values():
        if is_introspection_type(type_):
            continue
        if type_.ast_node is not None or type_.extension_ast_nodes:
            return True

        if is_object_type(type_) or is_interface_type(type_):
            members: Tuple[Any, ...] = tuple(type_.fields.values())
        elif is_input_object_type(type_):
            members = tuple(type_.fields.values())
        elif is_enum_type(type_):
            members = tuple(type_.values.values())
        else:
            continue

        for member in members:
            if member.ast_node is not None:
                return True
            if hasattr(member, "args"):
                for arg in member.args.values():
                    if arg.ast_node is not None:
                        return True

    return False


DirectiveUsage = Tuple[str, Dict[str, Any]]


def _normalize_directives(
    directive_nodes: Tuple[DirectiveNode, ...],
    directive_definitions: Dict[str, GraphQLDirective],
) -> List[DirectiveUsage]:
    """Turn directive AST nodes into (name, coerced arguments) pairs.

    The pairs stay in source order and are not grouped into a mapping so that
    repeated usages of repeatable directives are preserved.
    """
    usages: List[DirectiveUsage] = []
    for directive_node in directive_nodes:
        directive_name = directive_node.name.value
        directive_definition = directive_definitions.get(directive_name)
        if directive_definition is None:
            # Unknown directives are already rejected while validating the
            # schema; this only keeps the comparison defensive.
            arguments = {
                argument.name.value: value_from_ast_untyped(argument.value)
                for argument in directive_node.arguments
            }
        else:
            arguments = _coerce_directive_arguments(
                directive_definition, directive_node
            )
        usages.append((directive_name, arguments))
    return usages


def _coerce_directive_arguments(
    directive_definition: GraphQLDirective, directive_node: DirectiveNode
) -> Dict[str, Any]:
    """Coerce directive usage arguments, filling in definition defaults.

    Only the arguments declared by the directive definition are considered,
    so an omitted argument with a default value compares equal to an explicit
    default value. value_from_ast also fills default values of nested input
    object fields.
    """
    argument_nodes = {
        argument.name.value: argument for argument in directive_node.arguments
    }
    arguments: Dict[str, Any] = {}
    for name, argument_definition in directive_definition.args.items():
        argument_node = argument_nodes.get(name)
        if argument_node is None:
            default_value = argument_definition.default_value
            if default_value is not Undefined:
                arguments[argument_definition.out_name or name] = default_value
        else:
            arguments[argument_definition.out_name or name] = value_from_ast(
                argument_node.value, argument_definition.type
            )
    return arguments


def _diff_usages(
    coordinate: str,
    old_usages: List[DirectiveUsage],
    new_usages: List[DirectiveUsage],
) -> List[DirectiveUsageChange]:
    """Diff two ordered lists of directive usages at one coordinate.

    Usages are aligned by directive name with a longest common subsequence,
    so that the multiplicity and order of repeatable directive occurrences
    is taken into account without collapsing the lists into mappings.
    """
    paired, removed_indices, added_indices = _align_usages(old_usages, new_usages)

    changes: List[DirectiveUsageChange] = []
    for old_index, new_index in paired:
        old_name, old_arguments = old_usages[old_index]
        _, new_arguments = new_usages[new_index]
        if old_arguments != new_arguments:
            changes.append(
                DirectiveUsageChange(
                    DirectiveUsageChangeType.ARGUMENTS_CHANGED,
                    coordinate,
                    old_name,
                    old_arguments,
                    new_arguments,
                    old_index,
                    new_index,
                )
            )
    for old_index in removed_indices:
        old_name, old_arguments = old_usages[old_index]
        changes.append(
            DirectiveUsageChange(
                DirectiveUsageChangeType.REMOVED,
                coordinate,
                old_name,
                old_arguments=old_arguments,
                old_index=old_index,
            )
        )
    for new_index in added_indices:
        new_name, new_arguments = new_usages[new_index]
        changes.append(
            DirectiveUsageChange(
                DirectiveUsageChangeType.ADDED,
                coordinate,
                new_name,
                new_arguments=new_arguments,
                new_index=new_index,
            )
        )
    return changes


def _align_usages(
    old_usages: List[DirectiveUsage], new_usages: List[DirectiveUsage]
) -> Tuple[List[Tuple[int, int]], List[int], List[int]]:
    """Align two ordered directive usage sequences.

    Usages are aligned with a longest common subsequence over equal names.
    Exact matches (same name and arguments) receive a much larger weight, so
    unchanged occurrences stay paired whenever possible; the remaining
    same-named occurrences are paired as argument changes. Multiplicity and
    relative order of repeatable directives are thereby preserved, and
    leftovers are reported as removed or added.
    """
    old_length = len(old_usages)
    new_length = len(new_usages)
    # Exact matches outweigh any number of name-only matches.
    exact_weight = old_length + new_length + 1

    scores = [[0] * (new_length + 1) for _ in range(old_length + 1)]
    for old_index in range(old_length):
        for new_index in range(new_length):
            best = max(
                scores[old_index][new_index + 1],
                scores[old_index + 1][new_index],
            )
            if old_usages[old_index][0] == new_usages[new_index][0]:
                weight = (
                    exact_weight
                    if old_usages[old_index][1] == new_usages[new_index][1]
                    else 1
                )
                best = max(best, scores[old_index][new_index] + weight)
            scores[old_index + 1][new_index + 1] = best

    paired: List[Tuple[int, int]] = []
    old_index = old_length
    new_index = new_length
    while old_index > 0 and new_index > 0:
        can_go_up = (
            scores[old_index][new_index] == scores[old_index - 1][new_index]
        )
        can_go_left = (
            scores[old_index][new_index] == scores[old_index][new_index - 1]
        )
        current_name = old_usages[old_index - 1][0]
        next_name = new_usages[new_index - 1][0]
        weight = (
            exact_weight
            if current_name == next_name
            and old_usages[old_index - 1][1] == new_usages[new_index - 1][1]
            else 1 if current_name == next_name else 0
        )
        can_match = weight and scores[old_index][new_index] == (
            scores[old_index - 1][new_index - 1] + weight
        )
        if can_match and not (can_go_up or can_go_left):
            old_index -= 1
            new_index -= 1
            paired.append((old_index, new_index))
        elif can_go_left:
            new_index -= 1
        elif can_go_up:
            old_index -= 1
        else:
            old_index -= 1
            new_index -= 1
            paired.append((old_index, new_index))

    paired.reverse()

    changed_pairs: List[Tuple[int, int]] = []
    paired_old = set()
    paired_new = set()
    for old_index, new_index in paired:
        paired_old.add(old_index)
        paired_new.add(new_index)
        if old_usages[old_index][1] != new_usages[new_index][1]:
            changed_pairs.append((old_index, new_index))

    removed_indices = [
        old_index
        for old_index in range(old_length)
        if old_index not in paired_old
    ]
    added_indices = [
        new_index
        for new_index in range(new_length)
        if new_index not in paired_new
    ]

    return changed_pairs, removed_indices, added_indices
