from graphql.error import GraphQLError
from graphql.language import (
    FragmentDefinitionNode,
    FragmentSpreadNode,
    OperationDefinitionNode,
    parse,
    print_ast,
)

import pytest


def parse_with_fragment_arguments(source: str):
    return parse(source, allow_fragment_arguments=True)


def describe_parse_fragment_arguments():
    def parses_fragment_definition_with_parameters():
        doc = parse_with_fragment_arguments(
            "fragment Card($size: Int = 3) on User { friends(first: $size) { id } }"
        )
        fragment = doc.definitions[0]
        assert isinstance(fragment, FragmentDefinitionNode)
        assert fragment.name.value == "Card"
        var_defs = fragment.variable_definitions
        assert len(var_defs) == 1
        var_def = var_defs[0]
        assert var_def.variable.name.value == "size"
        assert var_def.default_value is not None
        assert var_def.default_value.value == "3"

    def parses_fragment_spread_with_arguments():
        doc = parse_with_fragment_arguments(
            "query { user { ...Card(size: 5, flag: $flag) } }"
        )
        operation = doc.definitions[0]
        assert isinstance(operation, OperationDefinitionNode)
        user_field = operation.selection_set.selections[0]
        spread = user_field.selection_set.selections[0]
        assert isinstance(spread, FragmentSpreadNode)
        assert spread.name.value == "Card"
        assert [arg.name.value for arg in spread.arguments] == ["size", "flag"]
        assert spread.arguments[0].value.value == "5"
        assert spread.arguments[1].value.name.value == "flag"

    def parses_fragment_spread_without_arguments():
        doc = parse_with_fragment_arguments("query { user { ...Card } }")
        operation = doc.definitions[0]
        spread = operation.selection_set.selections[0].selection_set.selections[0]
        assert isinstance(spread, FragmentSpreadNode)
        assert not spread.arguments

    def rejects_fragment_parameters_without_the_extension():
        with pytest.raises(GraphQLError) as exc_info:
            parse("fragment Card($size: Int = 3) on User { id }")
        assert "Syntax Error" in str(exc_info.value)

    def rejects_fragment_spread_arguments_without_the_extension():
        with pytest.raises(GraphQLError) as exc_info:
            parse("query { user { ...Card(size: 5) } }")
        assert "Syntax Error" in str(exc_info.value)

    def rejects_spread_arguments_with_only_legacy_fragment_variables():
        with pytest.raises(GraphQLError) as exc_info:
            parse(
                "query { user { ...Card(size: 5) } }",
                allow_legacy_fragment_variables=True,
            )
        assert "Syntax Error" in str(exc_info.value)


def describe_print_fragment_arguments():
    def prints_fragment_definition_with_parameters():
        doc = parse_with_fragment_arguments(
            "fragment Card($size: Int = 3) on User { friends(first: $size) { id } }"
        )
        assert print_ast(doc) == (
            "fragment Card($size: Int = 3) on User {\n"
            "  friends(first: $size) {\n"
            "    id\n"
            "  }\n"
            "}"
        )

    def prints_fragment_spread_with_arguments():
        doc = parse_with_fragment_arguments(
            "query { user { ...Card(size: 5) } }"
        )
        assert print_ast(doc) == (
            "{\n"
            "  user {\n"
            "    ...Card(size: 5)\n"
            "  }\n"
            "}"
        )

    def print_parse_roundtrip():
        source = (
            "fragment Card($size: Int = 3, $ids: [ID!]) on User {\n"
            "  friends(first: $size) {\n"
            "    id\n"
            "  }\n"
            "}\n"
            "\n"
            "query ($flag: Boolean!) {\n"
            "  user {\n"
            "    ...Card(size: 5, ids: [\"1\", \"2\"])\n"
            "    ...Card(size: $size, ids: $ids)\n"
            "  }\n"
            "}"
        )
        doc = parse_with_fragment_arguments(source)
        printed = print_ast(doc)
        assert parse_with_fragment_arguments(printed) == doc
        assert print_ast(parse_with_fragment_arguments(printed)) == printed
