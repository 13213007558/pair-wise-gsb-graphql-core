from typing import List

from pytest import raises

from graphql import graphql_sync, validate_schema
from graphql.language import DirectiveLocation, parse
from graphql.validation import validate
from graphql.type import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLDirective,
    GraphQLEnumType,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLID,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNamedType,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    GraphQLUnionType,
    is_introspection_type,
    is_specified_scalar_type,
    is_union_type,
    is_input_object_type,
    is_interface_type,
    is_object_type,
    is_wrapping_type,
    specified_directives,
)
from graphql.utilities import (
    SchemaProjectionError,
    build_schema,
    get_introspection_query,
    print_schema,
    project_schema,
)


def build_test_schema() -> GraphQLSchema:
    date_time_type = GraphQLScalarType(
        "DateTime",
        serialize=lambda value: "dt:" + value,
        parse_value=lambda value: value[3:],
    )

    role_type = GraphQLEnumType(
        "Role",
        {
            "ADMIN": GraphQLEnumValue(),
            "USER": GraphQLEnumValue(),
            "INTERNAL": GraphQLEnumValue(),
        },
    )

    node_interface = GraphQLInterfaceType(
        "Node",
        lambda: {"id": GraphQLField(GraphQLNonNull(GraphQLID))},
        resolve_type=lambda obj, info, type_: obj["__typename"],
    )

    user_type = GraphQLObjectType(
        "User",
        lambda: {
            "id": GraphQLField(GraphQLNonNull(GraphQLID)),
            "name": GraphQLField(
                GraphQLString, resolve=lambda user, info: user["name"].upper()
            ),
            "email": GraphQLField(GraphQLString),
            "role": GraphQLField(role_type),
            "friends": GraphQLField(GraphQLList(user_type)),
            "createdAt": GraphQLField(date_time_type),
        },
        interfaces=[node_interface],
    )

    audit_log_type = GraphQLObjectType(
        "AuditLog",
        lambda: {
            "message": GraphQLField(GraphQLString),
            "internal": GraphQLField(GraphQLString),
        },
    )

    search_result_type = GraphQLUnionType(
        "SearchResult",
        [user_type, audit_log_type],
        resolve_type=lambda obj, info, type_: obj["__typename"],
    )

    user_filter_input = GraphQLInputObjectType(
        "UserFilter",
        lambda: {
            "term": GraphQLInputField(GraphQLString),
            "role": GraphQLInputField(role_type),
            "internalOnly": GraphQLInputField(GraphQLBoolean),
        },
    )

    create_user_input = GraphQLInputObjectType(
        "CreateUserInput",
        lambda: {
            "name": GraphQLInputField(GraphQLNonNull(GraphQLString)),
            "role": GraphQLInputField(GraphQLNonNull(role_type)),
            "note": GraphQLInputField(GraphQLString),
        },
    )

    users = [
        {"__typename": "User", "id": "1", "name": "alice", "role": "USER"},
        {"__typename": "User", "id": "2", "name": "bob", "role": "ADMIN"},
    ]

    query_type = GraphQLObjectType(
        "Query",
        lambda: {
            "node": GraphQLField(
                node_interface,
                args={"id": GraphQLArgument(GraphQLNonNull(GraphQLID))},
                resolve=lambda root, info, id: next(
                    item for item in users if item["id"] == id
                ),
            ),
            "user": GraphQLField(
                user_type,
                args={
                    "id": GraphQLArgument(GraphQLNonNull(GraphQLID)),
                    "filter": GraphQLArgument(user_filter_input),
                },
                resolve=lambda root, info, id, filter=None: users[0],
            ),
            "users": GraphQLField(
                GraphQLList(user_type), resolve=lambda root, info: users
            ),
            "search": GraphQLField(
                search_result_type,
                args={"text": GraphQLArgument(GraphQLString)},
                resolve=lambda root, info, text=None: {
                    "__typename": "AuditLog",
                    "message": "logged",
                },
            ),
            "now": GraphQLField(
                date_time_type, resolve=lambda root, info: "2026-09-22"
            ),
            "auditLog": GraphQLField(audit_log_type),
            "internalStats": GraphQLField(GraphQLString),
        },
    )

    mutation_type = GraphQLObjectType(
        "Mutation",
        lambda: {
            "createUser": GraphQLField(
                user_type,
                args={
                    "input": GraphQLArgument(GraphQLNonNull(create_user_input))
                },
                resolve=lambda root, info, input: {
                    "__typename": "User",
                    "id": "3",
                    **input,
                },
            ),
            "purge": GraphQLField(GraphQLBoolean),
        },
    )

    async def user_created_source(root, info):
        yield users[0]

    subscription_type = GraphQLObjectType(
        "Subscription",
        lambda: {
            "userCreated": GraphQLField(user_type, subscribe=user_created_source)
        },
    )

    tag_directive = GraphQLDirective(
        "tag",
        locations=[DirectiveLocation.FIELD_DEFINITION],
        args={"name": GraphQLArgument(GraphQLString)},
    )

    return GraphQLSchema(
        query_type,
        mutation_type,
        subscription_type,
        directives=specified_directives + (tag_directive,),
    )


def named_type_objects(schema: GraphQLSchema) -> List[GraphQLNamedType]:
    """Collect all named type objects referenced by the given schema."""
    seen: List[GraphQLNamedType] = []
    seen_ids = set()

    def visit(type_):
        while is_wrapping_type(type_):
            type_ = type_.of_type
        if id(type_) in seen_ids:
            return
        seen_ids.add(id(type_))
        seen.append(type_)
        if is_object_type(type_) or is_interface_type(type_):
            for field in type_.fields.values():
                visit(field.type)
                for arg in field.args.values():
                    visit(arg.type)
            for interface in type_.interfaces:
                visit(interface)
        elif is_union_type(type_):
            for member in type_.types:
                visit(member)
        elif is_input_object_type(type_):
            for field in type_.fields.values():
                visit(field.type)

    for type_ in schema.type_map.values():
        visit(type_)
    for directive in schema.directives:
        for arg in directive.args.values():
            visit(arg.type)
    return seen


def custom_type_ids(schema: GraphQLSchema) -> set:
    return {
        id(type_)
        for type_ in named_type_objects(schema)
        if not is_specified_scalar_type(type_) and not is_introspection_type(type_)
    }

def describe_project_schema():
    def keeps_only_the_allowed_coordinates():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.users", "User.id", "User.name"])
        assert validate_schema(projected) == []
        assert print_schema(projected) == (
            "directive @tag(name: String) on FIELD_DEFINITION\n"
            "\n"
            "type Query {\n"
            "  users: [User]\n"
            "}\n"
            "\n"
            "type User {\n"
            "  id: ID!\n"
            "  name: String\n"
            "}"
        )

    def accepts_a_single_coordinate_string():
        schema = build_test_schema()
        projected = project_schema(schema, "Query.now")
        assert print_schema(projected) == (
            "directive @tag(name: String) on FIELD_DEFINITION\n"
            "\n"
            "type Query {\n  now: DateTime\n}\n\nscalar DateTime"
        )

    def removed_fields_cannot_be_executed():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.users", "User.id", "User.name"])
        result = graphql_sync(projected, "{ internalStats }")
        assert result.data is None
        assert [error.message for error in result.errors] == [
            "Cannot query field 'internalStats' on type 'Query'."
        ]
        result = graphql_sync(projected, "{ users { email } }")
        assert [error.message for error in result.errors] == [
            "Cannot query field 'email' on type 'User'."
        ]

    def removed_fields_do_not_leak_into_error_suggestions():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.users", "User.id", "User.name"])
        errors = validate(projected, parse("{ users { emai } }"))
        assert len(errors) == 1
        message = errors[0].message
        assert "Cannot query field 'emai'" in message
        assert "email" not in message
        errors = validate(projected, parse('{ user(id: "1") { id } }'))
        assert len(errors) == 1
        assert "internalStats" not in errors[0].message

    def removed_types_do_not_leak_into_introspection():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.users", "User.id", "User.name"])
        result = graphql_sync(projected, get_introspection_query())
        assert not result.errors
        type_names = {type_["name"] for type_ in result.data["__schema"]["types"]}
        assert type_names == {
            "Query",
            "User",
            "ID",
            "String",
            "Boolean",
            "__Schema",
            "__Type",
            "__TypeKind",
            "__Field",
            "__InputValue",
            "__EnumValue",
            "__Directive",
            "__DirectiveLocation",
        }

    def kept_resolvers_and_custom_scalars_still_work():
        schema = build_test_schema()
        projected = project_schema(
            schema, ["Query.users", "Query.now", "User.id", "User.name"]
        )
        result = graphql_sync(projected, "{ users { name } now }")
        assert not result.errors
        assert result.data == {
            "users": [{"name": "ALICE"}, {"name": "BOB"}],
            "now": "dt:2026-09-22",
        }

    def projects_enum_values():
        schema = build_test_schema()
        projected = project_schema(
            schema,
            ["Query.users", "User.id", "User.role", "Role.ADMIN", "Role.USER"],
        )
        assert "enum Role {\n  ADMIN\n  USER\n}" in print_schema(projected)
        result = graphql_sync(projected, "{ users { role } }")
        assert not result.errors
        assert result.data == {"users": [{"role": "USER"}, {"role": "ADMIN"}]}
        result = graphql_sync(
            projected,
            "query ($role: Role) { users { role } }",
            variable_values={"role": "INTERNAL"},
        )
        assert result.errors

    def emptying_a_union_cascades_to_its_fields():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.search", "SearchResult"])
        assert "query root type 'Query' has no retained fields" in str(exc_info.value)

    def unions_keep_only_retained_member_types():
        schema = build_test_schema()
        projected = project_schema(
            schema,
            ["Query.search", "Query.auditLog", "SearchResult", "AuditLog.message"],
        )
        assert "union SearchResult = AuditLog" in print_schema(projected)
        result = graphql_sync(
            projected, "{ search { __typename ... on AuditLog { message } } }"
        )
        assert not result.errors

    def drops_optional_arguments_with_missing_types():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.user", "User.id", "User.name"])
        user_field = projected.query_type.fields["user"]
        assert list(user_field.args) == ["id"]

    def reports_required_arguments_with_missing_types():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.users", "Mutation.createUser", "User.id"])
        message = str(exc_info.value)
        assert "Cannot project field 'Mutation.createUser'" in message
        assert "required argument 'input'" in message
        assert "'CreateUserInput'" in message

    def reports_required_arguments_missing_from_coordinates():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.user(filter)", "User.id"])
        message = str(exc_info.value)
        assert "Cannot project field 'Query.user'" in message
        assert "required argument 'id' is not included" in message

    def reports_required_input_fields_missing_from_projection():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(
                schema,
                [
                    "Query.users",
                    "Mutation.createUser",
                    "User.id",
                    "CreateUserInput.name",
                ],
            )
        message = str(exc_info.value)
        assert "Cannot project input object 'CreateUserInput'" in message
        assert "required field 'role' is not included" in message

    def reports_required_input_fields_with_missing_types():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(
                schema,
                ["Query.users", "Mutation.createUser", "User.id", "CreateUserInput"],
            )
        message = str(exc_info.value)
        assert "Cannot project input object 'CreateUserInput'" in message
        assert "required field 'role' depends on type 'Role'" in message

    def revalidates_interface_implementations():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.node", "Node", "User.name"])
        assert "Node.id" in str(exc_info.value)

    def keeps_satisfied_interface_implementations():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.node", "Node", "User"])
        assert validate_schema(projected) == []
        result = graphql_sync(
            projected, '{ node(id: "1") { id ... on User { name } } }'
        )
        assert not result.errors
        assert result.data == {"node": {"id": "1", "name": "ALICE"}}

    def fails_when_the_query_root_ends_up_empty():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["User.id", "User.name"])
        assert "query root type 'Query' has no retained fields" in str(exc_info.value)

    def rejects_unknown_coordinates():
        schema = build_test_schema()
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.unknown"])
        assert "Unknown schema coordinate: 'Query.unknown'" in str(exc_info.value)
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Unknown"])
        assert "Unknown schema coordinate: 'Unknown'" in str(exc_info.value)
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.user(unknown)"])
        assert "Unknown schema coordinate: 'Query.user(unknown)'" in str(
            exc_info.value
        )
        with raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["not a coordinate"])
        assert "Invalid schema coordinate" in str(exc_info.value)

    def preserves_subscribe_and_resolvers_by_reference():
        schema = build_test_schema()
        projected = project_schema(
            schema,
            ["Query.users", "Subscription.userCreated", "User.id", "User.name"],
        )
        original_field = schema.subscription_type.fields["userCreated"]
        projected_field = projected.subscription_type.fields["userCreated"]
        assert projected_field.subscribe is original_field.subscribe
        assert projected_field.resolve is original_field.resolve
        original_name_resolve = schema.get_type("User").fields["name"].resolve
        assert projected.get_type("User").fields["name"].resolve is (
            original_name_resolve
        )
        assert projected.get_type("User") is not schema.get_type("User")
        assert "Node" not in projected.type_map

    def keeps_custom_directives_with_available_arguments():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.now"])
        assert [directive.name for directive in projected.directives] == [
            directive.name for directive in schema.directives
        ]
        assert "tag" in [directive.name for directive in projected.directives]

    def does_not_modify_the_original_schema():
        schema = build_test_schema()
        original_sdl = print_schema(schema)
        original_user_fields = schema.get_type("User").fields
        projected = project_schema(schema, ["Query.users", "User.id", "User.name"])
        assert print_schema(schema) == original_sdl
        assert set(schema.get_type("User").fields) == set(original_user_fields)
        assert "email" in schema.get_type("User").fields
        projected_user_fields = projected.get_type("User").fields
        for name, field in projected_user_fields.items():
            assert field is not original_user_fields[name]
        result = graphql_sync(schema, "{ users { email } internalStats }")
        assert not result.errors

    def does_not_leak_removed_members_through_ast_nodes():
        schema = build_schema(
            """
            type Query {
              public: String
              secret: String
            }
            """
        )
        assert schema.query_type.ast_node is not None
        projected = project_schema(schema, ["Query.public"])
        assert projected.query_type.ast_node is None
        assert projected.query_type.fields["public"].ast_node is None
        assert "secret" not in print_schema(projected)

    def two_projections_of_the_same_schema_are_fully_independent():
        schema = build_test_schema()
        projection_a = project_schema(
            schema,
            [
                "Query.users",
                "User.id",
                "User.name",
                "User.role",
                "Role.ADMIN",
                "Role.USER",
            ],
        )
        projection_b = project_schema(
            schema,
            ["Query.users", "User.id", "User.email", "User.createdAt"],
        )

        # both projections are valid and executable
        assert validate_schema(projection_a) == []
        assert validate_schema(projection_b) == []
        result_a = graphql_sync(projection_a, "{ users { name role } }")
        assert not result_a.errors
        assert result_a.data == {
            "users": [
                {"name": "ALICE", "role": "USER"},
                {"name": "BOB", "role": "ADMIN"},
            ]
        }
        result_b = graphql_sync(projection_b, "{ users { email createdAt } }")
        assert not result_b.errors

        # both projections can be introspected independently
        introspection_a = graphql_sync(projection_a, get_introspection_query())
        introspection_b = graphql_sync(projection_b, get_introspection_query())
        assert not introspection_a.errors
        assert not introspection_b.errors
        user_a = next(
            type_
            for type_ in introspection_a.data["__schema"]["types"]
            if type_["name"] == "User"
        )
        user_b = next(
            type_
            for type_ in introspection_b.data["__schema"]["types"]
            if type_["name"] == "User"
        )
        assert {field["name"] for field in user_a["fields"]} == {
            "id",
            "name",
            "role",
        }
        assert {field["name"] for field in user_b["fields"]} == {
            "id",
            "email",
            "createdAt",
        }

        # every type referenced by a projection belongs to that projection
        for projected in (projection_a, projection_b):
            for type_ in named_type_objects(projected):
                if is_specified_scalar_type(type_) or is_introspection_type(type_):
                    continue
                assert projected.type_map[type_.name] is type_

        # no type object is shared between the two projections
        ids_a = custom_type_ids(projection_a)
        ids_b = custom_type_ids(projection_b)
        assert ids_a.isdisjoint(ids_b)

        # and none of them is shared with the original schema either
        original_ids = custom_type_ids(schema)
        assert ids_a.isdisjoint(original_ids)
        assert ids_b.isdisjoint(original_ids)

        # the projections really differ in behavior
        assert "role" in projection_a.get_type("User").fields
        assert "role" not in projection_b.get_type("User").fields
        assert "createdAt" in projection_b.get_type("User").fields
        assert "createdAt" not in projection_a.get_type("User").fields
