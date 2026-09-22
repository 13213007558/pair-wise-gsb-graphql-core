import re
from typing import (
    Collection,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    cast,
)

from ..error import GraphQLError
from ..pyutils import merge_kwargs
from ..type import (
    GraphQLArgument,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLInputField,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLType,
    get_named_type,
    is_enum_type,
    is_input_object_type,
    is_interface_type,
    is_introspection_type,
    is_list_type,
    is_non_null_type,
    is_object_type,
    is_required_argument,
    is_required_input_field,
    is_scalar_type,
    is_specified_scalar_type,
    is_union_type,
    validate_schema,
)

__all__ = ["SchemaProjectionError", "project_schema"]


class SchemaProjectionError(GraphQLError):
    """Raised when a schema cannot be projected to the given coordinates."""


_coordinate_pattern = re.compile(
    r"^([_A-Za-z][_0-9A-Za-z]*)"
    r"(?:\.([_A-Za-z][_0-9A-Za-z]*)"
    r"(?:\(\s*([_A-Za-z][_0-9A-Za-z]*)\s*\))?"
    r")?$"
)


def project_schema(
    schema: GraphQLSchema, coordinates: Collection[str]
) -> GraphQLSchema:
    """Project a schema to an explicitly allowed set of schema coordinates.

    Returns a new, independent and executable ``GraphQLSchema`` that contains
    exactly the parts of the original schema that are allowed by the given
    schema coordinates. The original schema and its type and field objects
    are never modified.

    The following schema coordinates are supported:

    - ``TypeName`` keeps the type with all of its members.
    - ``TypeName.fieldName`` keeps a single field of an object or interface
      type, a single field of an input object type or a single value of an
      enum type.
    - ``TypeName.fieldName(argName)`` keeps a single argument of a field.
      The field itself is kept as well, with only the explicitly allowed
      arguments.

    Types are kept only when they are allowed by a coordinate. Scalar types
    are the exception: since they have no projectable members, they are kept
    whenever a kept field, argument or input field references them, together
    with their custom serialization behavior.

    Types that end up without any members (objects, interfaces and input
    objects without fields, enums without values, unions without possible
    types) are removed, and this removal cascades: fields, arguments and
    input fields that reference a removed type are dropped as well, unless
    they are required. A required (non-null without default) argument of a
    kept field or a required input field of a kept input object can never be
    dropped silently, since that would loosen the contract of the projected
    schema. Instead a :class:`SchemaProjectionError` is raised that reports
    the dependency which blocks the projection.

    Interface implementations are re-validated in the projected schema, and
    the projection fails if the query root type ends up without fields.

    Resolvers (``resolve``, ``subscribe``, ``is_type_of`` and
    ``resolve_type``) as well as custom scalar behavior are preserved in
    the projected schema.
    """
    if isinstance(coordinates, str):
        coordinates = (coordinates,)
    if not isinstance(coordinates, Collection):
        raise TypeError(
            "Schema coordinates must be provided as a collection of strings."
        )

    full_types: Set[str] = set()
    type_members: Dict[str, Set[str]] = {}
    field_args: Dict[Tuple[str, str], Set[str]] = {}

    type_map = {
        name: type_
        for name, type_ in schema.type_map.items()
        if not is_introspection_type(type_)
    }

    for coordinate in coordinates:
        if not isinstance(coordinate, str):
            raise TypeError(
                f"Schema coordinates must be strings, received: {coordinate!r}."
            )
        match = _coordinate_pattern.match(coordinate)
        if not match:
            raise SchemaProjectionError(
                f"Invalid schema coordinate: '{coordinate}'."
            )
        type_name, member_name, arg_name = match.groups()
        type_ = type_map.get(type_name)
        if type_ is None:
            raise SchemaProjectionError(
                f"Unknown schema coordinate: '{coordinate}'."
                f" Type '{type_name}' does not exist in the schema."
            )
        if member_name is None:
            full_types.add(type_name)
            continue
        if is_object_type(type_) or is_interface_type(type_):
            field = type_.fields.get(member_name)
            if field is None:
                raise SchemaProjectionError(
                    f"Unknown schema coordinate: '{coordinate}'."
                    f" Field '{type_name}.{member_name}'"
                    " does not exist in the schema."
                )
            type_members.setdefault(type_name, set()).add(member_name)
            if arg_name is not None:
                if arg_name not in field.args:
                    raise SchemaProjectionError(
                        f"Unknown schema coordinate: '{coordinate}'."
                        f" Argument '{type_name}.{member_name}({arg_name}:)'"
                        " does not exist in the schema."
                    )
                field_args.setdefault((type_name, member_name), set()).add(arg_name)
        elif is_input_object_type(type_):
            if arg_name is not None or member_name not in type_.fields:
                raise SchemaProjectionError(
                    f"Unknown schema coordinate: '{coordinate}'."
                    f" Input field '{type_name}.{member_name}'"
                    " does not exist in the schema."
                )
            type_members.setdefault(type_name, set()).add(member_name)
        elif is_enum_type(type_):
            if arg_name is not None or member_name not in type_.values:
                raise SchemaProjectionError(
                    f"Unknown schema coordinate: '{coordinate}'."
                    f" Enum value '{type_name}.{member_name}'"
                    " does not exist in the schema."
                )
            type_members.setdefault(type_name, set()).add(member_name)
        else:
            raise SchemaProjectionError(
                f"Invalid schema coordinate: '{coordinate}'."
                f" Type '{type_name}' has no addressable members."
            )

    def is_wanted(name: str, member_name: str) -> bool:
        return name in full_types or member_name in type_members.get(name, ())

    # Compute the fixpoint of retained types. Types without any retained
    # members are removed, which can in turn invalidate members of other
    # retained types that reference them.
    retained: Set[str] = {
        name
        for name in type_map
        if name in full_types or name in type_members
    }

    def is_available(type_: GraphQLType) -> bool:
        named_type = get_named_type(type_)
        return is_scalar_type(named_type) or named_type.name in retained

    def has_retained_members(name: str) -> bool:
        type_ = cast(GraphQLNamedType, type_map[name])
        if is_object_type(type_) or is_interface_type(type_):
            return any(
                is_wanted(name, field_name) and is_available(field.type)
                for field_name, field in type_.fields.items()
            )
        if is_input_object_type(type_):
            return any(
                is_wanted(name, field_name) and is_available(field.type)
                for field_name, field in type_.fields.items()
            )
        if is_enum_type(type_):
            return any(is_wanted(name, value_name) for value_name in type_.values)
        if is_union_type(type_):
            return any(member.name in retained for member in type_.types)
        return True  # scalar types are always kept when referenced

    while True:
        emptied = {name for name in retained if not has_retained_members(name)}
        if not emptied:
            break
        retained -= emptied

    # Check that no required dependency of a retained member is missing.
    # Required arguments and required input fields must not be dropped
    # silently, since that would loosen the contract of the projected schema.
    blocking_errors: List[str] = []
    for name in sorted(retained):
        type_ = cast(GraphQLNamedType, type_map[name])
        if is_object_type(type_) or is_interface_type(type_):
            for field_name, field in type_.fields.items():
                if not is_wanted(name, field_name) or not is_available(field.type):
                    continue
                arg_coordinates = field_args.get((name, field_name))
                for arg_name, arg in field.args.items():
                    arg_wanted = arg_coordinates is None or arg_name in arg_coordinates
                    if arg_wanted and is_available(arg.type):
                        continue
                    if is_required_argument(arg):
                        if arg_wanted:
                            blocking_errors.append(
                                f"Cannot project field '{name}.{field_name}':"
                                f" its required argument '{arg_name}' depends"
                                f" on type '{get_named_type(arg.type).name}'"
                                " which is not part of the projection."
                            )
                        else:
                            blocking_errors.append(
                                f"Cannot project field '{name}.{field_name}':"
                                f" its required argument '{arg_name}' is not"
                                " included in the projection coordinates."
                            )
        elif is_input_object_type(type_):
            for field_name, field in type_.fields.items():
                field_wanted = is_wanted(name, field_name)
                if field_wanted and is_available(field.type):
                    continue
                if is_required_input_field(field):
                    if field_wanted:
                        blocking_errors.append(
                            f"Cannot project input object '{name}':"
                            f" its required field '{field_name}' depends"
                            f" on type '{get_named_type(field.type).name}'"
                            " which is not part of the projection."
                        )
                    else:
                        blocking_errors.append(
                            f"Cannot project input object '{name}':"
                            f" its required field '{field_name}' is not"
                            " included in the projection coordinates."
                        )

    if blocking_errors:
        raise SchemaProjectionError(
            "Schema projection failed:\n"
            + "\n".join(f" - {error}" for error in blocking_errors)
        )

    query_type = schema.query_type
    if query_type is None:
        raise SchemaProjectionError(
            "Schema projection failed:"
            " the schema provides no query root type to project."
        )
    if query_type.name not in retained:
        raise SchemaProjectionError(
            "Schema projection failed:"
            f" the query root type '{query_type.name}' has no retained fields."
            " A projection must keep at least one field of the query root type."
        )

    # Rebuild the retained parts of the schema as new type objects, so that
    # the projection is fully independent of the original schema.
    new_type_map: Dict[str, GraphQLNamedType] = {}

    def rebuild_type(type_: GraphQLType) -> GraphQLType:
        if is_list_type(type_):
            return GraphQLList(rebuild_type(type_.of_type))
        if is_non_null_type(type_):
            return GraphQLNonNull(rebuild_type(type_.of_type))
        return rebuild_named_type(cast(GraphQLNamedType, type_))

    def rebuild_argument(arg: GraphQLArgument) -> GraphQLArgument:
        return GraphQLArgument(
            **merge_kwargs(arg.to_kwargs(), type_=rebuild_type(arg.type), ast_node=None)
        )

    def rebuild_fields(type_: GraphQLNamedType) -> Dict[str, GraphQLField]:
        fields = {}
        for field_name, field in type_.fields.items():
            if not is_wanted(type_.name, field_name) or not is_available(field.type):
                continue
            arg_coordinates = field_args.get((type_.name, field_name))
            args = {
                arg_name: rebuild_argument(arg)
                for arg_name, arg in field.args.items()
                if (arg_coordinates is None or arg_name in arg_coordinates)
                and is_available(arg.type)
            }
            fields[field_name] = GraphQLField(
                **merge_kwargs(
                    field.to_kwargs(),
                    type_=rebuild_type(field.type),
                    args=args or None,
                    ast_node=None,
                )
            )
        return fields

    def rebuild_input_fields(
        type_: GraphQLNamedType,
    ) -> Dict[str, GraphQLInputField]:
        return {
            field_name: GraphQLInputField(
                **merge_kwargs(
                    field.to_kwargs(), type_=rebuild_type(field.type), ast_node=None
                )
            )
            for field_name, field in type_.fields.items()
            if is_wanted(type_.name, field_name) and is_available(field.type)
        }

    def rebuild_interfaces(type_: GraphQLNamedType) -> Tuple:
        return tuple(
            rebuild_named_type(interface)
            for interface in type_.interfaces
            if interface.name in retained
        )

    def rebuild_named_type(type_: GraphQLNamedType) -> GraphQLNamedType:
        name = type_.name
        if name in new_type_map:
            return new_type_map[name]
        if is_specified_scalar_type(type_) or is_introspection_type(type_):
            # Specified scalar and introspection types are shared singletons
            # without references to other types and can be reused as they are.
            return type_
        kwargs = merge_kwargs(type_.to_kwargs(), ast_node=None)
        if is_scalar_type(type_):
            new_type = type_.__class__(**kwargs)
        elif is_object_type(type_) or is_interface_type(type_):
            new_type = type_.__class__(
                **merge_kwargs(
                    kwargs,
                    fields=lambda: rebuild_fields(type_),
                    interfaces=lambda: rebuild_interfaces(type_),
                )
            )
        elif is_union_type(type_):
            new_type = type_.__class__(
                **merge_kwargs(
                    kwargs,
                    types=lambda: tuple(
                        rebuild_named_type(member)
                        for member in type_.types
                        if member.name in retained
                    ),
                )
            )
        elif is_enum_type(type_):
            new_type = type_.__class__(
                **merge_kwargs(
                    kwargs,
                    values={
                        value_name: GraphQLEnumValue(
                            **merge_kwargs(value.to_kwargs(), ast_node=None)
                        )
                        for value_name, value in type_.values.items()
                        if is_wanted(name, value_name)
                    },
                )
            )
        elif is_input_object_type(type_):
            new_type = type_.__class__(
                **merge_kwargs(kwargs, fields=lambda: rebuild_input_fields(type_))
            )
        else:  # pragma: no cover
            raise TypeError(f"Unexpected type: {type_!r}.")
        new_type_map[name] = new_type
        return new_type

    def rebuild_root_type(
        type_: Optional[GraphQLObjectType],
    ) -> Optional[GraphQLObjectType]:
        if type_ is None or type_.name not in retained:
            return None
        return cast(GraphQLObjectType, rebuild_named_type(type_))

    directives = []
    for directive in schema.directives:
        if all(is_available(arg.type) for arg in directive.args.values()):
            directives.append(
                directive.__class__(
                    **merge_kwargs(
                        directive.to_kwargs(),
                        args={
                            arg_name: rebuild_argument(arg)
                            for arg_name, arg in directive.args.items()
                        },
                        ast_node=None,
                    )
                )
            )

    projected_schema = GraphQLSchema(
        query=rebuild_root_type(schema.query_type),
        mutation=rebuild_root_type(schema.mutation_type),
        subscription=rebuild_root_type(schema.subscription_type),
        types=[
            rebuild_named_type(cast(GraphQLNamedType, type_map[name]))
            for name in type_map
            if name in retained
        ],
        directives=directives,
        description=schema.description,
        extensions=schema.extensions,
    )

    # Re-validate the projected schema. This also re-validates the remaining
    # interface implementation relationships.
    validation_errors = validate_schema(projected_schema)
    if validation_errors:
        raise SchemaProjectionError(
            "Schema projection produced an invalid schema:\n"
            + "\n".join(f" - {error.message}" for error in validation_errors)
        )

    return projected_schema
