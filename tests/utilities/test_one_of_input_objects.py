from pytest import raises

from graphql import (
    GraphQLID,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLField,
    GraphQLString,
    build_schema,
    build_client_schema,
    extend_schema,
    graphql_sync,
    introspection_from_schema,
    parse,
    print_schema,
    validate,
    validate_schema,
)
from graphql.language import parse_value
from graphql.pyutils import Undefined
from graphql.utilities import coerce_input_value, value_from_ast


def lookup_type(**fields):
    return GraphQLInputObjectType("Lookup", fields, is_one_of=True)


def describe_one_of_input_objects():
    def supports_python_construction():
        input_type = lookup_type(
            id=GraphQLInputField(GraphQLID),
            email=GraphQLInputField(GraphQLString),
        )
        assert input_type.is_one_of is True

        schema = GraphQLSchema(
            query=GraphQLObjectType(
                "Query",
                {"lookup": GraphQLField(GraphQLString, {"input": input_type})},
            )
        )
        assert validate_schema(schema) == []

    def rejects_non_nullable_members():
        input_type = lookup_type(id=GraphQLNonNull(GraphQLID))
        schema = GraphQLSchema(
            query=GraphQLObjectType(
                "Query",
                {"lookup": GraphQLField(GraphQLString, {"input": input_type})},
            )
        )
        errors = validate_schema(schema)
        assert len(errors) == 1
        assert errors[0].message == "OneOf input field Lookup.id must be nullable."

    def rejects_member_default_values():
        input_type = lookup_type(
            id=GraphQLInputField(GraphQLID, default_value="7")
        )
        schema = GraphQLSchema(
            query=GraphQLObjectType(
                "Query",
                {"lookup": GraphQLField(GraphQLString, {"input": input_type})},
            )
        )
        errors = validate_schema(schema)
        assert len(errors) == 1
        assert (
            errors[0].message
            == "OneOf input field Lookup.id must not have a default value."
        )

    def validates_sdl_member_constraints():
        schema = build_schema(
            "input Lookup @oneOf { id: ID! email: String = null }"
            " type Query { lookup(input: Lookup): String }"
        )
        errors = validate_schema(schema)
        assert [error.message for error in errors] == [
            "OneOf input field Lookup.id must be nullable.",
            "OneOf input field Lookup.email must not have a default value.",
        ]

    def builds_and_extends_sdl():
        schema = build_schema(
            "input Lookup @oneOf { id: ID email: String }"
            " type Query { lookup(input: Lookup): String }"
        )
        assert schema.get_type("Lookup").is_one_of is True
        assert "input Lookup @oneOf" in print_schema(schema)

        extended = extend_schema(
            schema,
            parse("extend input Lookup @oneOf { phone: String }"),
        )
        assert extended.get_type("Lookup").is_one_of is True
        assert "phone: String" in print_schema(extended)

    def survives_introspection_roundtrip():
        schema = build_schema(
            "input Lookup @oneOf { id: ID email: String }"
            " type Query { lookup(input: Lookup): String }"
        )
        introspection = introspection_from_schema(schema)
        lookup = next(
            type_
            for type_ in introspection["__schema"]["types"]
            if type_["name"] == "Lookup"
        )
        assert lookup["isOneOf"] is True

        client_schema = build_client_schema(introspection)
        assert client_schema.get_type("Lookup").is_one_of is True
        assert "input Lookup @oneOf" in print_schema(client_schema)

        second_introspection = introspection_from_schema(client_schema)
        second_lookup = next(
            type_
            for type_ in second_introspection["__schema"]["types"]
            if type_["name"] == "Lookup"
        )
        assert second_lookup["isOneOf"] is True

        introspection["__schema"] = dict(introspection["__schema"])
        introspection["__schema"]["types"] = [
            dict(type_) if type_.get("name") == "Lookup" else type_
            for type_ in introspection["__schema"]["types"]
        ]
        legacy_lookup = next(
            type_
            for type_ in introspection["__schema"]["types"]
            if type_["name"] == "Lookup"
        )
        legacy_lookup["isOneOf"] = None
        legacy_client_schema = build_client_schema(introspection, assume_valid=True)
        assert legacy_client_schema.get_type("Lookup").is_one_of is False

    def validates_literals_with_locations():
        schema = build_schema(
            "input Lookup @oneOf { id: ID email: String }"
            " type Query { lookup(input: Lookup!): String }"
        )

        assert validate(schema, parse('{ lookup(input: {id: "7"}) }')) == []

        null_errors = validate(schema, parse("{ lookup(input: {email: null}) }"))
        assert len(null_errors) == 1
        assert null_errors[0].locations[0].column == 25
        assert "non-null" in null_errors[0].message

        multiple_errors = validate(
            schema,
            parse(
                '{ lookup(input: {id: "7", email: "a@example.com"}) }'
            ),
        )
        assert len(multiple_errors) == 1
        assert multiple_errors[0].locations[0].column == 18
        assert multiple_errors[0].locations[1].column == 27

        missing_variable_errors = validate(
            schema, parse("query { lookup(input: {id: $id}) }")
        )
        assert any(
            "Variable '$id' is not defined." in error.message
            for error in missing_variable_errors
        )

    def requires_non_null_member_variables():
        schema = build_schema(
            "input Lookup @oneOf { id: ID email: String }"
            " type Query { lookup(input: Lookup!): String }"
        )

        valid = validate(
            schema,
            parse("query($id: ID!) { lookup(input: {id: $id}) }"),
        )
        assert valid == []

        invalid = validate(
            schema,
            parse('query($id: ID = "7") { lookup(input: {id: $id}) }'),
        )
        assert len(invalid) == 1
        assert "used in position expecting type 'ID'" in invalid[0].message

    def distinguishes_missing_from_explicit_null_variables():
        schema = build_schema(
            "input Lookup @oneOf { id: ID email: String }"
            " type Query { lookup(input: Lookup): String }"
        )

        omitted_variable = graphql_sync(
            schema,
            "query($input: Lookup) { lookup(input: $input) }",
            variable_values={},
        )
        assert omitted_variable.errors is None
        assert omitted_variable.data == {"lookup": None}

        explicit_null = graphql_sync(
            schema,
            "query($input: Lookup) { lookup(input: $input) }",
            variable_values={"input": None},
        )
        assert explicit_null.data == {"lookup": None}

        missing_member_variable = graphql_sync(
            schema,
            "query($id: ID!) { lookup(input: {id: $id}) }",
            variable_values={},
        )
        assert missing_member_variable.data is None
        assert "was not provided" in missing_member_variable.errors[0].message

    def coerces_literal_and_python_values():
        input_type = lookup_type(
            id=GraphQLInputField(GraphQLID),
            email=GraphQLInputField(GraphQLString),
        )

        assert value_from_ast(parse_value('{id: "7"}'), input_type) == {"id": "7"}
        assert value_from_ast(parse_value("{email: null}"), input_type) is Undefined
        assert (
            value_from_ast(
                parse_value('{id: "7", email: "a@example.com"}'), input_type
            )
            is Undefined
        )
        assert (
            value_from_ast(parse_value("{id: $id}"), input_type, {})
            is Undefined
        )
        assert (
            value_from_ast(
                parse_value("{id: $id}"), input_type, {"id": None}
            )
            is Undefined
        )

        assert coerce_input_value({"id": "7"}, input_type) == {"id": "7"}
        with raises(Exception):
            coerce_input_value({"email": None}, input_type)
        with raises(Exception):
            coerce_input_value(
                {"id": "7", "email": "a@example.com"}, input_type
            )
        with raises(Exception):
            coerce_input_value({}, input_type)

    def applies_rules_inside_nested_objects_and_lists():
        inner = lookup_type(
            id=GraphQLInputField(GraphQLID),
            email=GraphQLInputField(GraphQLString),
        )
        outer = GraphQLInputObjectType(
            "Outer",
            {
                "items": GraphQLInputField(GraphQLList(inner)),
                "selected": GraphQLInputField(inner),
            },
        )

        assert coerce_input_value(
            {
                "items": [
                    {"id": "7"},
                    {"email": "a@example.com"},
                ]
            },
            outer,
        ) == {
            "items": [{"id": "7"}, {"email": "a@example.com"}]
        }
        with raises(Exception):
            coerce_input_value({"items": [{"email": None}]}, outer)
        with raises(Exception):
            coerce_input_value(
                {"selected": {"id": "7", "email": "a@example.com"}},
                outer,
            )

        assert validate_schema(
            GraphQLSchema(
                query=GraphQLObjectType(
                    "Query",
                    {
                        "outer": GraphQLField(
                            GraphQLString,
                            {"input": GraphQLList(inner)},
                        )
                    },
                )
            )
        ) == []

    def keeps_regular_input_object_rules():
        regular = GraphQLInputObjectType(
            "Regular",
            {
                "id": GraphQLInputField(
                    GraphQLNonNull(GraphQLID), default_value="7"
                )
            },
        )
        assert regular.is_one_of is False
        assert coerce_input_value({}, regular) == {"id": "7"}

    def preserves_positional_out_type_argument():
        class LookupValue:
            def __init__(self, values):
                self.values = values

        input_type = GraphQLInputObjectType(
            "Lookup",
            {
                "id": GraphQLInputField(GraphQLID),
                "email": GraphQLInputField(GraphQLString),
            },
            None,
            LookupValue,
            True,
        )
        result = coerce_input_value({"id": "7"}, input_type)
        assert isinstance(result, LookupValue)
        assert result.values == {"id": "7"}
