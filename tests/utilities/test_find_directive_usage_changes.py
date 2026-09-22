from pytest import raises

from graphql import (
    build_client_schema,
    build_schema,
    DirectiveLocation,
    extend_schema,
    GraphQLDirective,
    GraphQLSchema,
    parse,
)
from graphql.utilities import (
    DirectiveUsageChange,
    DirectiveUsageChanges,
    DirectiveUsageChangeType,
    find_directive_usage_changes,
    introspection_from_schema,
)


def describe_find_directive_usage_changes():
    def returns_no_changes_for_identical_schemas():
        schema = build_schema(
            """
            directive @auth(role: Role! = USER)
              on FIELD_DEFINITION | OBJECT | SCHEMA
            enum Role { ADMIN USER }
            schema @auth(role: ADMIN) { query: Query }
            type Query @auth(role: ADMIN) {
              field: String @auth(role: ADMIN)
            }
            """
        )

        assert find_directive_usage_changes(schema, schema) == (False, [])

    def detects_a_directive_added_or_removed_on_every_location():
        sdl = """
          directive @d on SCHEMA | OBJECT | FIELD_DEFINITION
            | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
            | ENUM | ENUM_VALUE | INTERFACE | SCALAR | UNION
          type Query {
            field(arg: String): String
          }
          input I { x: Int }
          enum E { A }
          interface N { n: String }
          scalar S
          union U = Query
        """
        old_schema = build_schema(sdl)
        new_schema = build_schema(
            """
          directive @d on SCHEMA | OBJECT | FIELD_DEFINITION
            | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
            | ENUM | ENUM_VALUE | INTERFACE | SCALAR | UNION
          schema @d { query: Query }
          type Query @d {
            field(arg: String @d): String @d
          }
          input I @d { x: Int @d }
          enum E @d { A @d }
          interface N @d { n: String @d }
          scalar S @d
          union U @d = Query
        """
        )

        result = find_directive_usage_changes(old_schema, new_schema)

        assert result.is_incomplete is False
        added_coordinates = sorted(
            change.coordinate
            for change in result.changes
            if change.type is DirectiveUsageChangeType.ADDED
        )
        assert added_coordinates == [
            "E",
            "E.A",
            "I",
            "I.x",
            "N",
            "N.n",
            "Query",
            "Query.field",
            "Query.field(arg)",
            "S",
            "Schema",
            "U",
        ]

        reversed_result = find_directive_usage_changes(new_schema, old_schema)
        removed_coordinates = sorted(
            change.coordinate
            for change in reversed_result.changes
            if change.type is DirectiveUsageChangeType.REMOVED
        )
        assert removed_coordinates == added_coordinates

    def detects_argument_changes_using_schema_coordinates():
        def build(role):
            return build_schema(
                f"""
                directive @auth(role: Role! = USER)
                  on FIELD_DEFINITION | ARGUMENT_DEFINITION
                enum Role {{ ADMIN USER }}
                type Query {{
                  field(limit: Int @auth(role: {role})): String
                    @auth(role: {role})
                }}
                """
            )

        old_schema = build("ADMIN")
        new_schema = build("USER")

        assert find_directive_usage_changes(old_schema, new_schema).changes == [
            DirectiveUsageChange(
                DirectiveUsageChangeType.ARGUMENTS_CHANGED,
                "Query.field",
                "auth",
                {"role": "ADMIN"},
                {"role": "USER"},
                0,
                0,
            ),
            DirectiveUsageChange(
                DirectiveUsageChangeType.ARGUMENTS_CHANGED,
                "Query.field(limit)",
                "auth",
                {"role": "ADMIN"},
                {"role": "USER"},
                0,
                0,
            ),
        ]

    def fills_in_default_values_from_the_directive_definition():
        def build(usage):
            return build_schema(
                f"""
                directive @auth(role: Role! = USER) on FIELD_DEFINITION
                enum Role {{ ADMIN USER }}
                type Query {{
                  field: String {usage}
                }}
                """
            )

        old_schema = build("@auth")
        new_schema = build("@auth(role: USER)")

        assert find_directive_usage_changes(old_schema, new_schema) == (False, [])

        changed_schema = build("@auth(role: ADMIN)")
        changes = find_directive_usage_changes(old_schema, changed_schema).changes
        assert len(changes) == 1
        assert changes[0].type is DirectiveUsageChangeType.ARGUMENTS_CHANGED
        assert changes[0].old_arguments == {"role": "USER"}
        assert changes[0].new_arguments == {"role": "ADMIN"}

    def fills_in_nested_input_object_default_values():
        def build(usage):
            return build_schema(
                f"""
                input Policy {{ role: Role = USER, retries: Int = 3 }}
                directive @auth(policy: Policy) on FIELD_DEFINITION
                enum Role {{ ADMIN USER }}
                type Query {{
                  field: String {usage}
                }}
                """
            )

        old_schema = build("@auth(policy: {})")
        new_schema = build("@auth(policy: {retries: 3})")

        assert find_directive_usage_changes(old_schema, new_schema) == (False, [])

        changed_schema = build("@auth(policy: {role: ADMIN, retries: 3})")
        changes = find_directive_usage_changes(old_schema, changed_schema).changes
        assert len(changes) == 1
        assert changes[0].old_arguments == {"policy": {"role": "USER", "retries": 3}}
        assert changes[0].new_arguments == {
            "policy": {"role": "ADMIN", "retries": 3}
        }

    def ignores_input_object_field_order_but_respects_list_order():
        def build(roles):
            return build_schema(
                f"""
                directive @auth(roles: [Role!]!) on FIELD_DEFINITION
                enum Role {{ ADMIN USER GUEST }}
                type Query {{
                  field: String @auth(roles: {roles})
                }}
                """
            )

        reordered = build("[ADMIN, USER]")
        same = build("[ADMIN, USER]")
        reversed_list = build("[USER, ADMIN]")

        assert find_directive_usage_changes(reordered, same) == (False, [])
        changes = find_directive_usage_changes(reordered, reversed_list).changes
        assert len(changes) == 1
        assert changes[0].type is DirectiveUsageChangeType.ARGUMENTS_CHANGED
        assert changes[0].old_arguments == {"roles": ["ADMIN", "USER"]}
        assert changes[0].new_arguments == {"roles": ["USER", "ADMIN"]}

    def keeps_multiplicity_and_order_of_repeatable_directives():
        def build(usages):
            return build_schema(
                f"""
                directive @auth(role: Role!) repeatable on FIELD_DEFINITION
                enum Role {{ ADMIN USER GUEST }}
                type Query {{
                  field: String {usages}
                }}
                """
            )

        old_schema = build(
            "@auth(role: ADMIN) @auth(role: USER) @auth(role: GUEST)"
        )
        new_schema = build(
            "@auth(role: ADMIN) @auth(role: GUEST) @auth(role: GUEST)"
        )

        changes = find_directive_usage_changes(old_schema, new_schema).changes

        assert [change.type for change in changes] == [
            DirectiveUsageChangeType.ARGUMENTS_CHANGED,
        ]
        assert changes[0].old_arguments == {"role": "USER"}
        assert changes[0].new_arguments == {"role": "GUEST"}
        assert changes[0].old_index == 1
        assert changes[0].new_index == 1

    def detects_added_repeatable_occurrences():
        def build(usages):
            return build_schema(
                f"""
                directive @auth(role: Role!) repeatable on FIELD_DEFINITION
                enum Role {{ ADMIN USER }}
                type Query {{
                  field: String {usages}
                }}
                """
            )

        old_schema = build("@auth(role: ADMIN)")
        new_schema = build("@auth(role: ADMIN) @auth(role: USER)")

        changes = find_directive_usage_changes(old_schema, new_schema).changes
        assert changes == [
            DirectiveUsageChange(
                DirectiveUsageChangeType.ADDED,
                "Query.field",
                "auth",
                None,
                {"role": "USER"},
                None,
                1,
            )
        ]

    def detects_removed_repeatable_occurrences():
        def build(usages):
            return build_schema(
                f"""
                directive @auth(role: Role!) repeatable on FIELD_DEFINITION
                enum Role {{ ADMIN USER }}
                type Query {{
                  field: String {usages}
                }}
                """
            )

        old_schema = build("@auth(role: ADMIN) @auth(role: USER)")
        new_schema = build("@auth(role: ADMIN)")

        changes = find_directive_usage_changes(old_schema, new_schema).changes
        assert changes == [
            DirectiveUsageChange(
                DirectiveUsageChangeType.REMOVED,
                "Query.field",
                "auth",
                {"role": "USER"},
                None,
                1,
                None,
            )
        ]

    def includes_directives_added_through_extend_schema():
        sdl = """
          directive @auth(role: Role! = USER) repeatable
            on OBJECT | FIELD_DEFINITION | ENUM | ENUM_VALUE | SCHEMA
          enum Role { ADMIN USER }
          type Query {
            field: String @auth(role: ADMIN)
          }
          enum E { A }
        """
        old_schema = extend_schema(
            build_schema(sdl),
            parse(
                """
                extend schema @auth(role: ADMIN)
                extend type Query @auth(role: ADMIN) {
                  addedField: String @auth(role: ADMIN)
                }
                extend enum E @auth(role: ADMIN) { B @auth(role: ADMIN) }
                """
            ),
        )
        new_schema = extend_schema(
            build_schema(sdl),
            parse(
                """
                extend schema @auth(role: USER)
                extend type Query @auth(role: USER) {
                  addedField: String @auth(role: USER)
                }
                extend enum E @auth(role: USER) { B @auth(role: USER) }
                """
            ),
        )

        changes = find_directive_usage_changes(old_schema, new_schema).changes

        assert [
            (change.coordinate, change.new_arguments) for change in changes
        ] == [
            ("E", {"role": "USER"}),
            ("E.B", {"role": "USER"}),
            ("Query", {"role": "USER"}),
            ("Query.addedField", {"role": "USER"}),
            ("Schema", {"role": "USER"}),
        ]
        assert all(
            change.type is DirectiveUsageChangeType.ARGUMENTS_CHANGED
            for change in changes
        )

    def is_stable_over_roundtrips_and_repeated_comparisons():
        sdl = """
          directive @auth(role: Role! = USER) repeatable
            on FIELD_DEFINITION | OBJECT | SCHEMA | INPUT_FIELD_DEFINITION
            | ENUM_VALUE | ARGUMENT_DEFINITION
          directive @tag(name: String!) on FIELD_DEFINITION
          enum Role { ADMIN USER }
          schema @auth(role: ADMIN) { query: Query }
          type Query @auth(role: ADMIN) {
            field(limit: Int @auth): String
              @auth(role: ADMIN) @tag(name: "a") @auth(role: USER)
          }
          input I { x: Int @auth }
          enum E { A @auth }
        """
        extension = """
          extend type Query { other: String @auth(role: ADMIN) }
        """
        old_schema = extend_schema(build_schema(sdl), parse(extension))
        new_schema = extend_schema(
            build_schema(sdl.replace('"a"', '"b"')),
            parse(extension),
        )

        first = find_directive_usage_changes(old_schema, new_schema)
        second = find_directive_usage_changes(old_schema, new_schema)
        third = find_directive_usage_changes(
            extend_schema(build_schema(sdl), parse(extension)),
            extend_schema(build_schema(sdl.replace('"a"', '"b"')), parse(extension)),
        )

        assert first == second == third
        assert find_directive_usage_changes(old_schema, old_schema) == (False, [])

    def returns_incomplete_result_for_introspection_schemas():
        sdl = """
          directive @auth(role: Role = USER) on FIELD_DEFINITION
          enum Role { ADMIN USER }
          type Query {
            field: String @auth(role: ADMIN)
          }
        """
        sdl_schema = build_schema(sdl)
        client_schema = build_client_schema(introspection_from_schema(sdl_schema))

        result = find_directive_usage_changes(sdl_schema, client_schema)
        assert result == DirectiveUsageChanges(True, [])

        reversed_result = find_directive_usage_changes(client_schema, sdl_schema)
        assert reversed_result == DirectiveUsageChanges(True, [])

        assert find_directive_usage_changes(client_schema, client_schema) == (
            True,
            [],
        )

    def reports_schema_validation_errors_for_invalid_definitions():
        valid_schema = build_schema("type Query { field: String }")
        bad_directive = GraphQLDirective(
            name="__bad",
            locations=(DirectiveLocation.FIELD_DEFINITION,),
        )
        invalid_schema = GraphQLSchema(
            query=valid_schema.query_type, directives=[bad_directive]
        )

        with raises(TypeError, match="__bad"):
            find_directive_usage_changes(invalid_schema, valid_schema)

        with raises(TypeError, match="__bad"):
            find_directive_usage_changes(valid_schema, invalid_schema)
