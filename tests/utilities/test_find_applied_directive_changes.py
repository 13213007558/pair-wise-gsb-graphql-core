from pytest import raises

from graphql.utilities import (
    AppliedDirectiveChange,
    AppliedDirectiveChangeType,
    build_client_schema,
    build_schema,
    extend_schema,
    find_applied_directive_changes,
    introspection_from_schema,
)
from graphql import parse


ADDED = AppliedDirectiveChangeType.APPLIED_DIRECTIVE_ADDED
REMOVED = AppliedDirectiveChangeType.APPLIED_DIRECTIVE_REMOVED
CHANGED = AppliedDirectiveChangeType.APPLIED_DIRECTIVE_ARGUMENTS_CHANGED


def change_tuples(result):
    return [
        (change.type, change.coordinate, change.directive_name, change.occurrence)
        for change in result.changes
    ]


def describe_find_applied_directive_changes():
    def detects_no_changes_for_identical_schemas():
        sdl = """
            directive @auth(role: String) on FIELD_DEFINITION | OBJECT

            type Query @auth(role: "ADMIN") {
              user: String @auth(role: "USER")
            }
            """
        old_schema = build_schema(sdl)
        new_schema = build_schema(sdl)

        result = find_applied_directive_changes(old_schema, new_schema)
        assert result.is_complete is True
        assert result.changes == []

    def is_stable_for_repeated_and_reversed_comparisons():
        old_sdl = """
            directive @tag(name: String) repeatable on OBJECT

            type Query @tag(name: "a") @tag(name: "b") {
              user: String
            }
            """
        new_sdl = """
            directive @tag(name: String) repeatable on OBJECT

            type Query @tag(name: "a") @tag(name: "c") {
              user: String
            }
            """
        old_schema = build_schema(old_sdl)
        new_schema = build_schema(new_sdl)

        first = find_applied_directive_changes(old_schema, new_schema)
        second = find_applied_directive_changes(old_schema, new_schema)
        assert first == second

        reversed_result = find_applied_directive_changes(new_schema, old_schema)
        assert change_tuples(reversed_result) == [
            (CHANGED, "Query", "tag", 1),
        ]
        assert reversed_result.changes[0].old_arguments == {"name": "c"}
        assert reversed_result.changes[0].new_arguments == {"name": "b"}

    def detects_argument_changes_on_fields():
        old_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "ADMIN")
            }
            """
        )
        new_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "USER")
            }
            """
        )

        result = find_applied_directive_changes(old_schema, new_schema)
        assert result.is_complete is True
        assert change_tuples(result) == [(CHANGED, "Query.user", "auth", 0)]
        change = result.changes[0]
        assert change.old_arguments == {"role": "ADMIN"}
        assert change.new_arguments == {"role": "USER"}

    def detects_added_and_removed_directives_on_all_locations():
        old_schema = build_schema(
            """
            directive @a on SCHEMA | OBJECT | FIELD_DEFINITION
              | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
              | ENUM | ENUM_VALUE
            directive @b on SCHEMA | OBJECT | FIELD_DEFINITION
              | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
              | ENUM | ENUM_VALUE

            schema @a {
              query: Query
            }

            type Query @a {
              user(id: ID @a): String @a
            }

            input UserFilter @a {
              name: String @a
            }

            enum Role @a {
              ADMIN @a
            }
            """
        )
        new_schema = build_schema(
            """
            directive @a on SCHEMA | OBJECT | FIELD_DEFINITION
              | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
              | ENUM | ENUM_VALUE
            directive @b on SCHEMA | OBJECT | FIELD_DEFINITION
              | ARGUMENT_DEFINITION | INPUT_OBJECT | INPUT_FIELD_DEFINITION
              | ENUM | ENUM_VALUE

            schema @b {
              query: Query
            }

            type Query @b {
              user(id: ID @b): String @b
            }

            input UserFilter @b {
              name: String @b
            }

            enum Role @b {
              ADMIN @b
            }
            """
        )

        result = find_applied_directive_changes(old_schema, new_schema)
        assert change_tuples(result) == [
            (REMOVED, "schema", "a", 0),
            (ADDED, "schema", "b", 0),
            (REMOVED, "Query", "a", 0),
            (ADDED, "Query", "b", 0),
            (REMOVED, "Query.user", "a", 0),
            (ADDED, "Query.user", "b", 0),
            (REMOVED, "Query.user(id:)", "a", 0),
            (ADDED, "Query.user(id:)", "b", 0),
            (REMOVED, "UserFilter", "a", 0),
            (ADDED, "UserFilter", "b", 0),
            (REMOVED, "UserFilter.name", "a", 0),
            (ADDED, "UserFilter.name", "b", 0),
            (REMOVED, "Role", "a", 0),
            (ADDED, "Role", "b", 0),
            (REMOVED, "Role.ADMIN", "a", 0),
            (ADDED, "Role.ADMIN", "b", 0),
        ]

    def includes_usages_added_by_schema_and_type_extensions():
        old_schema = build_schema(
            """
            directive @tag(name: String) on OBJECT | SCHEMA

            schema {
              query: Query
            }

            type Query {
              user: String
            }
            """
        )
        new_schema = extend_schema(
            old_schema,
            parse(
                """
                extend schema @tag(name: "root")

                extend type Query @tag(name: "extended")
                """
            ),
        )

        result = find_applied_directive_changes(old_schema, new_schema)
        assert change_tuples(result) == [
            (ADDED, "schema", "tag", 0),
            (ADDED, "Query", "tag", 0),
        ]

    def fills_in_defaults_from_the_directive_definition():
        old_schema = build_schema(
            """
            directive @auth(role: String = "ADMIN") on FIELD_DEFINITION

            type Query {
              user: String @auth
            }
            """
        )
        new_schema = build_schema(
            """
            directive @auth(role: String = "ADMIN") on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "ADMIN")
            }
            """
        )

        assert find_applied_directive_changes(old_schema, new_schema).changes == []

        changed_schema = build_schema(
            """
            directive @auth(role: String = "USER") on FIELD_DEFINITION

            type Query {
              user: String @auth
            }
            """
        )
        result = find_applied_directive_changes(old_schema, changed_schema)
        assert change_tuples(result) == [(CHANGED, "Query.user", "auth", 0)]
        change = result.changes[0]
        assert change.old_arguments == {"role": "ADMIN"}
        assert change.new_arguments == {"role": "USER"}

    def ignores_input_object_field_order_but_respects_list_order():
        old_schema = build_schema(
            """
            input Rule {
              path: String
              methods: [String!]
            }

            directive @policy(rule: Rule) on FIELD_DEFINITION

            type Query {
              user: String @policy(rule: {path: "/u", methods: ["GET", "POST"]})
            }
            """
        )
        reordered_fields = build_schema(
            """
            input Rule {
              path: String
              methods: [String!]
            }

            directive @policy(rule: Rule) on FIELD_DEFINITION

            type Query {
              user: String @policy(rule: {methods: ["GET", "POST"], path: "/u"})
            }
            """
        )
        reordered_list = build_schema(
            """
            input Rule {
              path: String
              methods: [String!]
            }

            directive @policy(rule: Rule) on FIELD_DEFINITION

            type Query {
              user: String @policy(rule: {path: "/u", methods: ["POST", "GET"]})
            }
            """
        )

        assert (
            find_applied_directive_changes(old_schema, reordered_fields).changes == []
        )
        assert change_tuples(
            find_applied_directive_changes(old_schema, reordered_list)
        ) == [(CHANGED, "Query.user", "policy", 0)]

    def keeps_count_and_order_of_repeatable_directives():
        old_schema = build_schema(
            """
            directive @tag(name: String) repeatable on OBJECT

            type Query @tag(name: "a") @tag(name: "b") {
              user: String
            }
            """
        )
        reordered = build_schema(
            """
            directive @tag(name: String) repeatable on OBJECT

            type Query @tag(name: "b") @tag(name: "a") {
              user: String
            }
            """
        )
        extended = build_schema(
            """
            directive @tag(name: String) repeatable on OBJECT

            type Query @tag(name: "a") @tag(name: "b") @tag(name: "c") {
              user: String
            }
            """
        )

        assert change_tuples(find_applied_directive_changes(old_schema, reordered)) == [
            (CHANGED, "Query", "tag", 0),
            (CHANGED, "Query", "tag", 1),
        ]
        assert change_tuples(find_applied_directive_changes(old_schema, extended)) == [
            (ADDED, "Query", "tag", 2),
        ]
        assert change_tuples(find_applied_directive_changes(extended, old_schema)) == [
            (REMOVED, "Query", "tag", 2),
        ]

    def leaves_severity_classification_to_the_caller():
        old_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "ADMIN")
            }
            """
        )
        new_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "USER")
            }
            """
        )

        def classify(change: AppliedDirectiveChange) -> str:
            if change.directive_name == "auth":
                return "BREAKING"
            return "COSMETIC"

        result = find_applied_directive_changes(
            old_schema, new_schema, classify_change=classify
        )
        assert [change.severity for change in result.changes] == ["BREAKING"]

        unclassified = find_applied_directive_changes(old_schema, new_schema)
        assert [change.severity for change in unclassified.changes] == [None]

    def marks_results_from_introspection_schemas_as_incomplete():
        sdl_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "ADMIN")
            }
            """
        )
        introspection_result = introspection_from_schema(sdl_schema)
        client_schema = build_client_schema(introspection_result)

        result = find_applied_directive_changes(sdl_schema, client_schema)
        assert result.is_complete is False
        assert result.changes == []

        result = find_applied_directive_changes(client_schema, sdl_schema)
        assert result.is_complete is False
        assert result.changes == []

        result = find_applied_directive_changes(client_schema, client_schema)
        assert result.is_complete is False
        assert result.changes == []

    def raises_schema_validation_errors_for_invalid_directive_definitions():
        old_schema = build_schema(
            """
            directive @auth(role: String) on FIELD_DEFINITION

            type Query {
              user: String @auth(role: "ADMIN")
            }
            """
        )
        invalid_schema = build_schema(
            """
            directive @auth(role: String! @deprecated(reason: "no"))
              on FIELD_DEFINITION

            type Query {
              user: String
            }
            """
        )

        with raises(TypeError, match="cannot be deprecated"):
            find_applied_directive_changes(old_schema, invalid_schema)
