from typing import Collection, Dict, List, Optional, Set, Union, cast

from ..error import GraphQLError
from ..pyutils import inspect, merge_kwargs
from ..type import (
    GraphQLArgument,
    GraphQLDirective,
    GraphQLEnumType,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLUnionType,
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
    is_union_type,
    validate_schema,
)

__all__ = ["project_schema", "SchemaProjectionError"]


class SchemaProjectionError(GraphQLError):
    """Raised when a schema cannot be projected to the given coordinates."""


def project_schema(
    schema: GraphQLSchema, allowed_coordinates: Collection[str]
) -> GraphQLSchema:
    """Project a GraphQLSchema down to the given schema coordinates.

    This function returns a new, independent and executable GraphQLSchema that
    only contains the parts of the original schema that are reachable from the
    given schema coordinates. The original schema is left untouched.

    Each coordinate addresses a field of an object, interface or input object
    type (like 'Type.field') or the value of an enum type (like 'Enum.VALUE').
    Types that are referenced by retained fields, arguments or input fields are
    retained as well, while types that end up without any members are removed
    together with everything that references them. Scalar types (including
    custom scalars) are retained by identity whenever they are referenced.

    A projection is rejected with a SchemaProjectionError when it would
    silently loosen an existing contract, that is when a retained field has a
    required argument or a retained input field is required and the type of
    that argument or input field is not part of the projection. It is also
    rejected when a retained object or interface type would no longer provide
    a retained field of an interface it implements, or when the query root
    type would not retain any field.

    All behavior attached to the retained parts of the schema (resolvers,
    'subscribe', 'is_type_of', 'resolve_type' and custom scalar serialization)
    is preserved, since the retained members are rebuilt as new type objects
    that share the original behavior functions.
    """

    # Parse and validate the given schema coordinates.
    field_coordinates: Dict[str, Set[str]] = {}
    value_coordinates: Dict[str, Set[str]] = {}
    for coordinate in allowed_coordinates:
        type_name, dot, member_name = coordinate.partition(".")
        if not type_name or not dot or not member_name or "." in member_name:
            raise SchemaProjectionError(
                f"Invalid schema coordinate: {coordinate!r}. Coordinates must"
                " have the form 'Type.field' or 'Enum.VALUE'."
            )
        type_ = schema.type_map.get(type_name)
        if type_ is None or is_introspection_type(type_):
            raise SchemaProjectionError(
                f"Unknown type '{type_name}' in schema coordinate: {coordinate!r}."
            )
        if is_object_type(type_) or is_interface_type(type_) or is_input_object_type(
            type_
        ):
            type_ = cast(
                Union[GraphQLObjectType, GraphQLInterfaceType, GraphQLInputObjectType],
                type_,
            )
            if member_name not in type_.fields:
                raise SchemaProjectionError(
                    f"Unknown field in schema coordinate: {coordinate!r}."
                )
            field_coordinates.setdefault(type_name, set()).add(member_name)
        elif is_enum_type(type_):
            type_ = cast(GraphQLEnumType, type_)
            if member_name not in type_.values:
                raise SchemaProjectionError(
                    f"Unknown enum value in schema coordinate: {coordinate!r}."
                )
            value_coordinates.setdefault(type_name, set()).add(member_name)
        else:
            raise SchemaProjectionError(
                f"Invalid schema coordinate: {coordinate!r}. Coordinates can only"
                " address fields of object, interface and input object types or"
                " values of enum types."
            )

    # Determine the viable types with a fixpoint computation. Scalar and
    # introspection types are always viable. Every other type is viable if it
    # retains at least one viable member. Removing non-viable types can make
    # other types lose their last member, so we iterate until nothing changes.
    viable: Set[str] = set()
    for type_ in schema.type_map.values():
        if is_scalar_type(type_) or is_introspection_type(type_):
            viable.add(type_.name)

    def is_viable_reference(type_: GraphQLNamedType) -> bool:
        return get_named_type(type_).name in viable

    def is_viable_field(field: GraphQLField) -> bool:
        return is_viable_reference(field.type)  # type: ignore[arg-type]

    def is_viable_input_field(field: GraphQLInputField) -> bool:
        return is_viable_reference(field.type)  # type: ignore[arg-type]

    changed = True
    while changed:
        changed = False
        for type_ in schema.type_map.values():
            name = type_.name
            if name in viable:
                continue
            if is_enum_type(type_):
                survives = bool(value_coordinates.get(name))
            elif is_object_type(type_) or is_interface_type(type_):
                composite = cast(Union[GraphQLObjectType, GraphQLInterfaceType], type_)
                survives = any(
                    is_viable_field(composite.fields[field_name])
                    for field_name in field_coordinates.get(name, ())
                )
            elif is_input_object_type(type_):
                input_object = cast(GraphQLInputObjectType, type_)
                survives = any(
                    is_viable_input_field(input_object.fields[field_name])
                    for field_name in field_coordinates.get(name, ())
                )
            elif is_union_type(type_):
                union = cast(GraphQLUnionType, type_)
                survives = any(member.name in viable for member in union.types)
            else:
                survives = False
            if survives:
                viable.add(name)
                changed = True

    # Report all dependencies that block the projection.
    problems: List[str] = []

    query_type = schema.query_type
    if query_type is None or query_type.name not in viable:
        problems.append(
            "the query root type"
            + (f" '{query_type.name}'" if query_type else "")
            + " does not retain any field"
        )

    for type_ in schema.type_map.values():
        if type_.name not in viable:
            continue
        if is_object_type(type_) or is_interface_type(type_):
            composite = cast(Union[GraphQLObjectType, GraphQLInterfaceType], type_)
            for field_name in field_coordinates.get(composite.name, ()):
                field = composite.fields[field_name]
                if not is_viable_field(field):
                    continue
                for arg_name, arg in field.args.items():
                    if is_required_argument(arg) and not is_viable_reference(
                        arg.type  # type: ignore[arg-type]
                    ):
                        problems.append(
                            f"field '{composite.name}.{field_name}' requires argument"
                            f" '{arg_name}' of type"
                            f" '{get_named_type(arg.type).name}'"
                            " which is not part of the projection"
                        )
            for interface in composite.interfaces:
                if interface.name not in viable:
                    continue
                for field_name in field_coordinates.get(interface.name, ()):
                    if not is_viable_field(interface.fields[field_name]):
                        continue
                    own_field = composite.fields.get(field_name)
                    if (
                        field_name not in field_coordinates.get(composite.name, ())
                        or own_field is None
                        or not is_viable_field(own_field)
                    ):
                        problems.append(
                            f"type '{composite.name}' must provide field"
                            f" '{interface.name}.{field_name}' of the interface"
                            " it implements"
                        )
        elif is_input_object_type(type_):
            input_object = cast(GraphQLInputObjectType, type_)
            for field_name in field_coordinates.get(input_object.name, ()):
                input_field = input_object.fields[field_name]
                if is_required_input_field(input_field) and not is_viable_input_field(
                    input_field
                ):
                    problems.append(
                        f"input field '{input_object.name}.{field_name}' is required"
                        f" and depends on type"
                        f" '{get_named_type(input_field.type).name}'"
                        " which is not part of the projection"
                    )

    if problems:
        raise SchemaProjectionError(
            "Cannot project schema to the given coordinates:\n- "
            + "\n- ".join(problems)
        )

    # Collect the types that form the projection: all viable composite types
    # plus the scalar types that are actually referenced by retained members
    # or directives. Unreferenced scalars (including custom scalars) are not
    # carried over into the projection.
    included = {
        name
        for name, type_ in schema.type_map.items()
        if name in viable
        and not is_scalar_type(type_)
        and not is_introspection_type(type_)
    }
    referenced_scalars: Set[str] = set()

    def collect_scalar(type_: GraphQLNamedType) -> None:
        named_type = get_named_type(type_)
        if is_scalar_type(named_type):
            referenced_scalars.add(named_type.name)

    for name in included:
        type_ = schema.type_map[name]
        if is_object_type(type_) or is_interface_type(type_):
            composite = cast(Union[GraphQLObjectType, GraphQLInterfaceType], type_)
            for field_name in field_coordinates.get(name, ()):
                field = composite.fields[field_name]
                if not is_viable_field(field):
                    continue
                collect_scalar(field.type)  # type: ignore[arg-type]
                for arg in field.args.values():
                    if is_viable_reference(arg.type):  # type: ignore[arg-type]
                        collect_scalar(arg.type)  # type: ignore[arg-type]
        elif is_input_object_type(type_):
            input_object = cast(GraphQLInputObjectType, type_)
            for field_name in field_coordinates.get(name, ()):
                input_field = input_object.fields[field_name]
                if is_viable_input_field(input_field):
                    collect_scalar(input_field.type)
    for directive in schema.directives:
        for arg in directive.args.values():
            if is_viable_reference(arg.type):  # type: ignore[arg-type]
                collect_scalar(arg.type)  # type: ignore[arg-type]

    # Rebuild the retained parts of the schema as new type objects.
    type_map: Dict[str, GraphQLNamedType] = {}

    def replace_type(
        type_: Union[GraphQLList, GraphQLNonNull, GraphQLNamedType],
    ) -> Union[GraphQLList, GraphQLNonNull, GraphQLNamedType]:
        if is_list_type(type_):
            return GraphQLList(replace_type(cast(GraphQLList, type_).of_type))
        if is_non_null_type(type_):
            return GraphQLNonNull(replace_type(cast(GraphQLNonNull, type_).of_type))
        return type_map[cast(GraphQLNamedType, type_).name]

    def project_args(
        args_map: Dict[str, GraphQLArgument],
    ) -> Dict[str, GraphQLArgument]:
        args = {}
        for name, arg in args_map.items():
            if not is_viable_reference(arg.type):  # type: ignore[arg-type]
                continue  # optional argument with a removed type
            args[name] = GraphQLArgument(
                **merge_kwargs(
                    arg.to_kwargs(),
                    type_=replace_type(arg.type),  # type: ignore[arg-type]
                )
            )
        return args

    def project_fields(
        type_: Union[GraphQLObjectType, GraphQLInterfaceType],
    ) -> Dict[str, GraphQLField]:
        fields = {}
        for name, field in type_.fields.items():
            if name not in field_coordinates.get(type_.name, ()):
                continue
            if not is_viable_field(field):
                continue
            fields[name] = GraphQLField(
                **merge_kwargs(
                    field.to_kwargs(),
                    type_=replace_type(field.type),  # type: ignore[arg-type]
                    args=project_args(field.args),
                )
            )
        return fields

    def project_input_fields(
        type_: GraphQLInputObjectType,
    ) -> Dict[str, GraphQLInputField]:
        fields = {}
        for name, field in type_.fields.items():
            if name not in field_coordinates.get(type_.name, ()):
                continue
            if not is_viable_input_field(field):
                continue  # optional input field with a removed type
            fields[name] = GraphQLInputField(
                **merge_kwargs(
                    field.to_kwargs(),
                    type_=replace_type(field.type),  # type: ignore[arg-type]
                )
            )
        return fields

    def project_interfaces(
        type_: Union[GraphQLObjectType, GraphQLInterfaceType],
    ) -> List[GraphQLInterfaceType]:
        return [
            cast(GraphQLInterfaceType, replace_type(interface))
            for interface in type_.interfaces
            if interface.name in viable
        ]

    def project_named_type(type_: GraphQLNamedType) -> GraphQLNamedType:
        if is_scalar_type(type_) or is_introspection_type(type_):
            return type_
        if is_object_type(type_):
            object_type = cast(GraphQLObjectType, type_)
            return GraphQLObjectType(
                **merge_kwargs(
                    object_type.to_kwargs(),
                    interfaces=lambda: project_interfaces(object_type),
                    fields=lambda: project_fields(object_type),
                )
            )
        if is_interface_type(type_):
            interface_type = cast(GraphQLInterfaceType, type_)
            return GraphQLInterfaceType(
                **merge_kwargs(
                    interface_type.to_kwargs(),
                    interfaces=lambda: project_interfaces(interface_type),
                    fields=lambda: project_fields(interface_type),
                )
            )
        if is_union_type(type_):
            union_type = cast(GraphQLUnionType, type_)
            return GraphQLUnionType(
                **merge_kwargs(
                    union_type.to_kwargs(),
                    types=lambda: [
                        cast(GraphQLObjectType, replace_type(member))
                        for member in union_type.types
                        if member.name in viable
                    ],
                )
            )
        if is_enum_type(type_):
            enum_type = cast(GraphQLEnumType, type_)
            kept_values = value_coordinates.get(enum_type.name, ())
            return GraphQLEnumType(
                **merge_kwargs(
                    enum_type.to_kwargs(),
                    values={
                        name: GraphQLEnumValue(**value.to_kwargs())
                        for name, value in enum_type.values.items()
                        if name in kept_values
                    },
                )
            )
        if is_input_object_type(type_):
            input_object_type = cast(GraphQLInputObjectType, type_)
            return GraphQLInputObjectType(
                **merge_kwargs(
                    input_object_type.to_kwargs(),
                    fields=lambda: project_input_fields(input_object_type),
                )
            )

        # Not reachable. All possible types have been considered.
        raise TypeError(f"Unexpected type: {inspect(type_)}.")

    for name, type_ in schema.type_map.items():
        if name in included or name in referenced_scalars:
            type_map[name] = project_named_type(type_)

    def project_directive(directive: GraphQLDirective) -> Optional[GraphQLDirective]:
        args = {}
        for name, arg in directive.args.items():
            if is_viable_reference(arg.type):  # type: ignore[arg-type]
                args[name] = GraphQLArgument(
                    **merge_kwargs(
                        arg.to_kwargs(),
                        type_=replace_type(arg.type),  # type: ignore[arg-type]
                    )
                )
            elif is_required_argument(arg):
                return None  # cannot loosen the contract of the directive
        return GraphQLDirective(
            **merge_kwargs(directive.to_kwargs(), args=args),
        )

    def replace_root_type(
        type_: Optional[GraphQLObjectType],
    ) -> Optional[GraphQLObjectType]:
        if type_ is None or type_.name not in type_map:
            return None
        return cast(GraphQLObjectType, type_map[type_.name])

    projected_schema = GraphQLSchema(
        query=replace_root_type(schema.query_type),
        mutation=replace_root_type(schema.mutation_type),
        subscription=replace_root_type(schema.subscription_type),
        types=tuple(type_map.values()),
        directives=[
            projected
            for directive in schema.directives
            if (projected := project_directive(directive)) is not None
        ],
        description=schema.description,
        extensions=schema.extensions,
        ast_node=schema.ast_node,
        extension_ast_nodes=schema.extension_ast_nodes,
    )

    # Safety net: the projection must be a valid schema of its own.
    validation_errors = validate_schema(projected_schema)
    if validation_errors:
        raise SchemaProjectionError(
            "Schema projection produced an invalid schema:\n- "
            + "\n- ".join(error.message for error in validation_errors)
        )

    return projected_schema
