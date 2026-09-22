from functools import partial

import pytest

from graphql.error import GraphQLError
from graphql.execution import execute_sync
from graphql.language import (
    ArgumentNode,
    BooleanValueNode,
    DirectiveNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    Location,
    NamedTypeNode,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionNode,
    SelectionSetNode,
    Source,
    Token,
    TokenKind,
    ValueNode,
    VariableDefinitionNode,
    VariableNode,
)
from graphql.utilities import get_introspection_query
from graphql.validation import MaxIntrospectionDepthRule, validate

from .harness import assert_validation_errors, test_schema

assert_errors = partial(assert_validation_errors, MaxIntrospectionDepthRule)

assert_valid = partial(assert_errors, errors=[])


def describe_validate_max_introspection_nodes_rule():
    def default_introspection_query():
        assert_valid(get_introspection_query())

    def all_ptions_introspection_query():
        assert_valid(
            get_introspection_query(
                descriptions=True,
                specified_by_url=True,
                directive_is_repeatable=True,
                schema_description=True,
                input_value_deprecation=True,
            )
        )

    def three_flat_fields_introspection_query():
        assert_valid(
            """
            {
              __type(name: "Query") {
                trueFields: fields(includeDeprecated: true) {
                  name
                }
                falseFields: fields(includeDeprecated: false) {
                  name
                }
                omittedFields: fields {
                  name
                }
              }
            }
            """
        )

    def three_fields_deep_introspection_query_from_schema():
        assert_errors(
            """
            {
              __schema {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_interfaces_deep_introspection_query_from_schema():
        assert_errors(
            """
            {
              __schema {
                types {
                  interfaces {
                    interfaces {
                      interfaces {
                        name
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_possible_types_deep_introspection_query_from_schema():
        assert_errors(
            """
            {
              __schema {
                types {
                  possibleTypes {
                    possibleTypes {
                      possibleTypes {
                        name
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_input_fields_deep_introspection_query_from_schema():
        assert_errors(
            """
            {
              __schema {
                types {
                  inputFields {
                    type {
                      inputFields {
                        type {
                          inputFields {
                            type {
                              name
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_fields_deep_introspection_query_from_multiple_schema():
        assert_errors(
            """
            {
              one: __schema {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
              two: __schema {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
              three: __schema {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(18, 15)],
                },
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(33, 15)],
                },
            ],
        )

    def three_fields_deep_introspection_query_from_type():
        assert_errors(
            """
            {
              __type(name: "Query") {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_fields_deep_introspection_query_from_multiple_type():
        assert_errors(
            """
            {
              one: __type(name: "Query") {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
              two: __type(name: "Query") {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
              three: __type(name: "Query") {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          fields {
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
""",
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(18, 15)],
                },
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(33, 15)],
                },
            ],
        )

    def one_fields_deep_with_three_fields_introspection_query():
        assert_valid(
            """
            {
              __schema {
                types {
                  fields {
                    type {
                      oneFields: fields {
                        name
                      }
                      twoFields: fields {
                        name
                      }
                      threeFields: fields {
                        name
                      }
                    }
                  }
                }
              }
            }
            """
        )

    def three_fields_deep_from_varying_parents_introspection_query():
        assert_errors(
            """
            {
              __schema {
                types {
                  fields {
                    type {
                      fields {
                        type {
                          ofType {
                            fields {
                              name
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_fields_deep_introspection_query_with_inline_fragments():
        assert_errors(
            """
            query test {
              __schema {
                types {
                  ... on __Type {
                    fields {
                      type {
                        ... on __Type {
                          ofType {
                            fields {
                              type {
                                ... on __Type {
                                  fields {
                                    name
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_fields_deep_introspection_query_with_fragments():
        assert_errors(
            """
            query test {
              __schema {
                types {
                  ...One
                }
              }
            }

            fragment One on __Type {
              fields {
                type {
                  ...Two
                }
              }
            }

            fragment Two on __Type {
              fields {
                type {
                  ...Three
                }
              }
            }

            fragment Three on __Type {
              fields {
                name
              }
            }
            """,
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(3, 15)],
                },
            ],
        )

    def three_fields_deep_inline_fragment_on_query():
        assert_errors(
            """
            {
              ... {
                __schema { types { fields { type { fields { type { fields { name } } } } } } }
              }
            }
            """,  # noqa: E501
            [
                {
                    "message": "Maximum introspection depth exceeded",
                    "locations": [(4, 17)],
                },
            ],
        )

    def opts_out_if_fragment_is_missing():
        assert_valid(
            """
            query test {
              __schema {
                types {
                  ...Missing
                }
              }
            }
            """
        )

    def does_not_infinitely_recurse_on_fragment_cycle():
        assert_valid(
            """
            query test {
              __schema {
                types {
                  ...Cycle
                }
              }
            }
            fragment Cycle on __Type {
              ...Cycle
            }
            """
        )


AST_SOURCE = Source("\n".join(" " * 40 for _ in range(30)))


def ast_name(value: str) -> NameNode:
    return NameNode(value=value)


def ast_location(line: int, column: int) -> Location:
    position = 41 * (line - 1) + column - 1
    token = Token(TokenKind.NAME, position, position + 1, line, column)
    return Location(token, token, AST_SOURCE)


def ast_field(
    field_name: str,
    *selections: SelectionNode,
    alias: str | None = None,
    directives: tuple[DirectiveNode, ...] = (),
    location: Location | None = None,
) -> FieldNode:
    return FieldNode(
        name=ast_name(field_name),
        alias=ast_name(alias) if alias else None,
        directives=directives,
        selection_set=SelectionSetNode(selections=tuple(selections))
        if selections
        else None,
        loc=location,
    )


def ast_directive(directive_name: str, value: ValueNode) -> DirectiveNode:
    return DirectiveNode(
        name=ast_name(directive_name),
        arguments=(ArgumentNode(name=ast_name("if"), value=value),),
    )


def ast_boolean(value: bool) -> BooleanValueNode:
    return BooleanValueNode(value=value)


def ast_variable(name: str) -> VariableNode:
    return VariableNode(name=ast_name(name))


def list_chain(
    depth: int,
    *,
    alias_lists: bool = False,
    deepest_directives: tuple[DirectiveNode, ...] = (),
) -> FieldNode:
    child = ast_field("name")
    for level in range(depth, 0, -1):
        list_field = ast_field(
            "fields",
            child,
            alias="sameFields" if alias_lists else None,
            directives=deepest_directives if level == depth else (),
        )
        child = ast_field("type", list_field) if level > 1 else list_field
    return child


def schema_list_chain(
    depth: int,
    *,
    alias_lists: bool = False,
    deepest_directives: tuple[DirectiveNode, ...] = (),
) -> FieldNode:
    return ast_field(
        "types",
        list_chain(
            depth,
            alias_lists=alias_lists,
            deepest_directives=deepest_directives,
        ),
    )


def introspection_root(
    root_name: str,
    *selections: SelectionNode,
    alias: str | None = None,
    line: int = 2,
    column: int = 3,
) -> FieldNode:
    return ast_field(
        root_name,
        *selections,
        alias=alias,
        location=ast_location(line, column),
    )


def fragment_definition(
    name: str, *selections: SelectionNode
) -> FragmentDefinitionNode:
    return FragmentDefinitionNode(
        name=ast_name(name),
        type_condition=NamedTypeNode(name=ast_name("__Type")),
        selection_set=SelectionSetNode(selections=tuple(selections)),
    )


def fragment_spread(
    name: str, directives: tuple[DirectiveNode, ...] = ()
) -> FragmentSpreadNode:
    return FragmentSpreadNode(name=ast_name(name), directives=directives)


def query_document(
    *roots: FieldNode,
    fragments: tuple[FragmentDefinitionNode, ...] = (),
    variable_definitions: tuple[VariableDefinitionNode, ...] = (),
) -> DocumentNode:
    operation = OperationDefinitionNode(
        operation=OperationType.QUERY,
        variable_definitions=variable_definitions,
        selection_set=SelectionSetNode(selections=roots),
    )
    return DocumentNode(definitions=(operation, *fragments))


def validate_max(document: DocumentNode):
    return validate(test_schema, document, [MaxIntrospectionDepthRule])


@pytest.mark.parametrize(
    ("directive_name", "condition", "expected_depth"),
    [
        ("skip", ast_boolean(True), None),
        ("include", ast_boolean(False), None),
        ("skip", ast_boolean(False), 3),
        ("include", ast_boolean(True), 3),
        ("skip", ast_variable("condition"), 3),
        ("include", ast_variable("condition"), 3),
    ],
    ids=[
        "literal-skip",
        "literal-include",
        "literal-do-not-skip",
        "literal-do-include",
        "variable-skip-default",
        "variable-include-default",
    ],
)
def test_static_skip_include_depth_and_execution_default(
    directive_name: str,
    condition: BooleanValueNode | VariableNode,
    expected_depth: int | None,
):
    directives = (ast_directive(directive_name, condition),)
    document = query_document(
        introspection_root(
            "__schema",
            schema_list_chain(3, deepest_directives=directives),
        ),
        variable_definitions=(
            VariableDefinitionNode(
                variable=ast_variable("condition"),
                type=NamedTypeNode(name=ast_name("Boolean")),
                default_value=ast_boolean(directive_name == "skip"),
            ),
        ),
    )

    errors = validate_max(document)

    if expected_depth is None:
        assert errors == []
    else:
        assert len(errors) == 1
        assert errors[0].locations == [(2, 3)]
        assert errors[0].extensions == {
            "depth": expected_depth,
            "maximumDepth": 3,
        }

    result = execute_sync(test_schema, document)
    assert result.errors is None
    assert result.data is not None
    assert "__schema" in result.data


@pytest.mark.parametrize(
    "conditional_location",
    ["field", "fragment-spread", "inline-fragment"],
)
def test_literal_exclusion_is_recognized_on_each_selection_kind(
    conditional_location: str,
):
    directive = ast_directive("skip", ast_boolean(True))
    third_list = ast_field("fields", ast_field("name"), directives=(directive,))
    if conditional_location == "field":
        conditional_selection: SelectionNode = third_list
    elif conditional_location == "fragment-spread":
        conditional_selection = fragment_spread("Deep", (directive,))
    else:
        conditional_selection = InlineFragmentNode(
            type_condition=NamedTypeNode(name=ast_name("__Type")),
            directives=(directive,),
            selection_set=SelectionSetNode(selections=(third_list,)),
        )

    inner = ast_field("type", conditional_selection)
    fragments: tuple[FragmentDefinitionNode, ...] = ()
    if conditional_location == "fragment-spread":
        fragments = (fragment_definition("Deep", third_list),)
    document = query_document(
        introspection_root(
            "__schema",
            ast_field(
                "types",
                ast_field("fields", inner),
            ),
        ),
        fragments=fragments,
    )

    assert validate_max(document) == []
    result = execute_sync(test_schema, document)
    assert result.errors is None
    assert result.data is not None


def test_malformed_or_duplicated_conditionals_remain_conservatively_included():
    malformed = ast_field(
        "fields",
        ast_field("name"),
        directives=(DirectiveNode(name=ast_name("skip")),),
    )
    duplicated = ast_field(
        "fields",
        ast_field("name"),
        directives=(
            ast_directive("skip", ast_boolean(True)),
            ast_directive("skip", ast_boolean(True)),
        ),
    )

    for third_list in (malformed, duplicated):
        document = query_document(
            introspection_root(
                "__schema",
                ast_field(
                    "types",
                    ast_field(
                        "fields",
                        ast_field(
                            "type",
                            ast_field("type", ast_field("fields", third_list)),
                        ),
                    ),
                ),
            )
        )
        errors = validate_max(document)
        assert len(errors) == 1
        assert errors[0].extensions["depth"] == 3


@pytest.mark.parametrize(
    ("root_name", "alias", "location"),
    [
        ("__schema", None, (2, 3)),
        ("__schema", "schemaAlias", (2, 3)),
        ("__type", "typeAlias", (2, 3)),
    ],
    ids=["schema-root", "aliased-schema-root", "aliased-type-root"],
)
def test_aliases_use_response_branch_and_actual_field_depth(
    root_name: str, alias: str | None, location: tuple[int, int]
):
    document = query_document(
        introspection_root(
            root_name,
            schema_list_chain(3, alias_lists=True)
            if root_name == "__schema"
            else list_chain(3, alias_lists=True),
            alias=alias,
        )
    )

    errors = validate_max(document)

    assert len(errors) == 1
    assert errors[0].locations == [location]
    assert errors[0].extensions == {"depth": 3, "maximumDepth": 3}


def test_schema_and_type_paths_are_not_merged():
    document = query_document(
        introspection_root(
            "__schema", schema_list_chain(2), alias="schemaRoot", line=2
        ),
        introspection_root(
            "__type",
            list_chain(2),
            alias="typeRoot",
            line=3,
        ),
    )

    assert validate_max(document) == []


def test_sibling_aliases_do_not_share_depth_across_branches():
    first_branch = ast_field(
        "fields",
        ast_field(
            "type",
            ast_field(
                "fields",
                ast_field("name"),
                alias="sameFields",
            ),
        ),
        alias="firstList",
    )
    second_branch = ast_field(
        "interfaces",
        ast_field(
            "type",
            ast_field(
                "fields",
                ast_field("name"),
                alias="sameFields",
            ),
        ),
        alias="secondList",
    )
    document = query_document(
        introspection_root("__schema", ast_field("types", first_branch, second_branch))
    )

    assert validate_max(document) == []


def test_recursive_fragments_have_independent_branch_depth():
    shared = fragment_definition("Shared", ast_field("fields", ast_field("name")))
    schema_branch = ast_field("type", fragment_spread("Shared"))
    type_branch = ast_field("ofType", fragment_spread("Shared"))
    reused_document = query_document(
        introspection_root("__schema", ast_field("types", schema_branch, type_branch)),
        fragments=(shared,),
    )
    assert validate_max(reused_document) == []

    recursive_a = fragment_definition(
        "A",
        ast_field("fields", ast_field("type", fragment_spread("B"))),
    )
    recursive_b = fragment_definition(
        "B",
        ast_field("fields", ast_field("type", fragment_spread("C"))),
    )
    recursive_c = fragment_definition(
        "C", ast_field("fields", ast_field("name")), fragment_spread("A")
    )
    recursive_document = query_document(
        introspection_root("__schema", ast_field("types", fragment_spread("A"))),
        fragments=(recursive_a, recursive_b, recursive_c),
    )
    recursive_errors = validate_max(recursive_document)
    assert len(recursive_errors) == 1
    assert recursive_errors[0].locations == [(2, 3)]
    assert recursive_errors[0].extensions == {
        "depth": 3,
        "maximumDepth": 3,
    }

    cycle = fragment_definition("Cycle", fragment_spread("Cycle"))
    cycle_document = query_document(
        introspection_root("__schema", ast_field("types", fragment_spread("Cycle"))),
        fragments=(cycle,),
    )
    assert validate_max(cycle_document) == []


def test_all_operations_report_their_own_schema_and_type_roots():
    document = DocumentNode(
        definitions=(
            OperationDefinitionNode(
                name=ast_name("One"),
                operation=OperationType.QUERY,
                selection_set=SelectionSetNode(
                    selections=(
                        introspection_root(
                            "__schema",
                            schema_list_chain(3),
                            line=2,
                            column=3,
                        ),
                        introspection_root(
                            "__type",
                            list_chain(3),
                            alias="a",
                            line=3,
                            column=6,
                        ),
                    )
                ),
            ),
            OperationDefinitionNode(
                name=ast_name("Two"),
                operation=OperationType.QUERY,
                selection_set=SelectionSetNode(
                    selections=(
                        introspection_root(
                            "__schema",
                            schema_list_chain(3),
                            alias="b",
                            line=5,
                            column=6,
                        ),
                    )
                ),
            ),
        )
    )

    errors = validate_max(document)

    assert [error.locations for error in errors] == [
        [(2, 3)],
        [(3, 6)],
        [(5, 6)],
    ]
    assert {error.extensions["depth"] for error in errors} == {3}


def test_very_deep_non_recursive_ast_does_not_overflow():
    child = ast_field("name")
    for _ in range(10_000):
        child = ast_field("type", child)
    document = query_document(
        introspection_root("__schema", ast_field("types", ast_field("fields", child)))
    )

    assert validate_max(document) == []


def test_enabling_the_rule_does_not_reorder_other_rule_errors():
    from graphql.validation import ASTValidationRule

    class FieldReportingRule(ASTValidationRule):
        def enter_field(self, node: FieldNode, *_args):
            self.report_error(GraphQLError(f"field:{node.name.value}", node))

    document = query_document(
        introspection_root("__schema", schema_list_chain(3)),
    )
    baseline_errors = validate(test_schema, document, [FieldReportingRule])
    combined_errors = validate(
        test_schema, document, [MaxIntrospectionDepthRule, FieldReportingRule]
    )
    other_errors = [
        error
        for error in combined_errors
        if error.message != "Maximum introspection depth exceeded"
    ]

    assert other_errors == baseline_errors
