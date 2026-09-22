from functools import partial
from typing import Any

import pytest

from graphql import graphql_sync
from graphql.language import (
    ArgumentNode,
    BooleanValueNode,
    DirectiveNode,
    DocumentNode,
    FieldNode,
    NameNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
    parse,
)
from graphql.utilities import get_introspection_query
from graphql.validation import (
    FieldsOnCorrectTypeRule,
    MaxIntrospectionDepthRule,
    validate,
)

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


def nested_list_fields_query(depth, directive="", header=""):
    """Build a query with the given number of nested `fields` list fields.

    The given directive is attached to the innermost `fields` field.
    """
    inner = "fields" + (f" {directive}" if directive else "") + " { name }"
    for _ in range(depth - 1):
        inner = f"fields {{ type {{ {inner} }} }}"
    return f"{header}{{ __schema {{ types {{ {inner} }} }} }}"


DEPTH_EXCEEDED = "Maximum introspection depth exceeded"


def make_field_node(name, *selections, alias=None, directives=None):
    return FieldNode(
        alias=NameNode(value=alias) if alias else None,
        name=NameNode(value=name),
        directives=directives,
        selection_set=SelectionSetNode(selections=selections) if selections else None,
    )


def make_document_node(root_field):
    return DocumentNode(
        definitions=(
            OperationDefinitionNode(
                operation=OperationType.QUERY,
                selection_set=SelectionSetNode(selections=(root_field,)),
            ),
        )
    )


DEEP_FRAGMENT = """
fragment Deep on __Type {
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
"""


def describe_static_depth_boundary_policy():
    @pytest.mark.parametrize("depth", [1, 2], ids=["one_level", "two_levels"])
    def nested_list_fields_below_the_limit_are_valid(depth):
        assert_valid(nested_list_fields_query(depth))

    def third_nested_list_field_reaches_the_limit():
        assert_errors(
            nested_list_fields_query(3),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, 3)]}],
        )

    def fourth_nested_list_field_is_also_reported():
        assert_errors(
            nested_list_fields_query(4),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, 3)]}],
        )

    @pytest.mark.parametrize(
        "directive",
        ["@skip(if: true)", "@include(if: false)"],
        ids=["skip_true", "include_false"],
    )
    def statically_excluded_list_fields_do_not_count(directive):
        assert_valid(nested_list_fields_query(3, directive))

    @pytest.mark.parametrize(
        "directive",
        ["@skip(if: false)", "@include(if: true)", "@skip", "@include"],
        ids=["skip_false", "include_true", "skip_no_arg", "include_no_arg"],
    )
    def not_statically_excluded_list_fields_count(directive):
        assert_errors(
            nested_list_fields_query(3, directive),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, 3)]}],
        )

    @pytest.mark.parametrize(
        "directive",
        ["@skip(if: $flag)", "@include(if: $flag)"],
        ids=["skip_variable", "include_variable"],
    )
    def variable_dependent_list_fields_count(directive):
        header = "query Q($flag: Boolean!) "
        assert_errors(
            nested_list_fields_query(3, directive, header),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, len(header) + 3)]}],
        )

    @pytest.mark.parametrize(
        ("directive", "default"),
        [("@skip(if: $flag)", "true"), ("@include(if: $flag)", "false")],
        ids=["skip_variable_defaults_true", "include_variable_defaults_false"],
    )
    def variables_with_defaults_are_not_statically_decidable(directive, default):
        # Even though the default value would statically exclude the field,
        # the runtime value can override it, so the field is still counted.
        header = f"query Q($flag: Boolean = {default}) "
        assert_errors(
            nested_list_fields_query(3, directive, header),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, len(header) + 3)]}],
        )

    @pytest.mark.parametrize(
        ("directive", "locations"),
        [
            ("@skip(if: true)", []),
            ("@include(if: false)", []),
            ("@skip(if: false)", [(2, 3)]),
            ("@include(if: true)", [(2, 3)]),
            ("@skip(if: $flag)", [(2, 3)]),
            ("@include(if: $flag)", [(2, 3)]),
        ],
        ids=[
            "skip_true",
            "include_false",
            "skip_false",
            "include_true",
            "skip_variable",
            "include_variable",
        ],
    )
    def statically_excluded_fragment_spreads_do_not_count(directive, locations):
        query = (
            "query Q($flag: Boolean) {\n"
            "  __schema {\n"
            "    types {\n"
            f"      ...Deep {directive}\n"
            "    }\n"
            "  }\n"
            "}\n" + DEEP_FRAGMENT
        )
        errors: list[Any] = [{"message": DEPTH_EXCEEDED, "locations": locations}]
        assert_errors(query, errors if locations else [])

    @pytest.mark.parametrize(
        ("directive", "locations"),
        [
            ("@skip(if: true)", []),
            ("@include(if: false)", []),
            ("@skip(if: false)", [(1, 3)]),
            ("@include(if: true)", [(1, 3)]),
        ],
        ids=["skip_true", "include_false", "skip_false", "include_true"],
    )
    def statically_excluded_inline_fragments_do_not_count(directive, locations):
        inner = (
            f"... on __Type {directive} {{ fields {{ type {{ fields {{ name }} }} }} }}"
        )
        query = "{ __schema { types { fields { type { " + inner + " } } } } }"
        errors: list[Any] = [{"message": DEPTH_EXCEEDED, "locations": locations}]
        assert_errors(query, errors if locations else [])

    def statically_excluded_root_field_is_not_checked():
        assert_valid(
            "{ __schema @skip(if: true) { types {"
            " fields { type { fields { type { fields { name } } } } }"
            " } } }"
        )

    def aliased_list_fields_count_towards_the_depth():
        assert_errors(
            "{ __schema { types {"
            " one: fields { type {"
            " two: fields { type {"
            " three: fields { name }"
            " } } } } } } }",
            [{"message": DEPTH_EXCEEDED, "locations": [(1, 3)]}],
        )

    def schema_and_type_roots_are_checked_independently():
        assert_errors(
            """
            {
              __schema {
                types {
                  fields { type { fields { type { fields { name } } } } }
                }
              }
              __type(name: "Query") {
                fields { type { fields { type { fields { name } } } } }
              }
            }
            """,
            [
                {"message": DEPTH_EXCEEDED, "locations": [(3, 15)]},
                {"message": DEPTH_EXCEEDED, "locations": [(8, 15)]},
            ],
        )

    def aliased_roots_of_the_same_type_are_not_merged():
        assert_errors(
            """
            {
              one: __type(name: "Query") {
                fields { type { fields { type { fields { name } } } } }
              }
              two: __type(name: "Query") {
                fields { type { fields { type { fields { name } } } } }
              }
            }
            """,
            [
                {"message": DEPTH_EXCEEDED, "locations": [(3, 15)]},
                {"message": DEPTH_EXCEEDED, "locations": [(6, 15)]},
            ],
        )

    def multiple_operations_are_checked_independently():
        assert_errors(
            """
            query Deep {
              __schema {
                types {
                  fields { type { fields { type { fields { name } } } } }
                }
              }
            }
            query Shallow {
              __type(name: "Query") {
                fields { name }
              }
            }
            """,
            [{"message": DEPTH_EXCEEDED, "locations": [(3, 15)]}],
        )

    def each_operation_reports_its_own_root_field():
        assert_errors(
            """
            query DeepSchema {
              __schema {
                types {
                  fields { type { fields { type { fields { name } } } } }
                }
              }
            }
            query DeepType {
              __type(name: "Query") {
                fields { type { fields { type { fields { name } } } } }
              }
            }
            """,
            [
                {"message": DEPTH_EXCEEDED, "locations": [(3, 15)]},
                {"message": DEPTH_EXCEEDED, "locations": [(10, 15)]},
            ],
        )


def describe_recursive_fragments():
    def self_referencing_fragment_with_list_field_terminates():
        # The cycle itself is reported by the `NoFragmentCyclesRule`;
        # this rule simply stops the traversal instead of recursing forever.
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
              fields {
                ...Cycle
              }
            }
            """
        )

    def mutually_recursive_fragments_with_list_fields_terminate():
        assert_valid(
            """
            query test {
              __schema {
                types {
                  ...A
                }
              }
            }
            fragment A on __Type {
              fields {
                ...B
              }
            }
            fragment B on __Type {
              fields {
                ...A
              }
            }
            """
        )

    def same_fragment_can_be_spread_on_different_depths():
        # The fragment is shallow where it is spread first, but exceeds
        # the limit where it is spread on a deeper path.
        assert_errors(
            """
            query test {
              __schema {
                types {
                  ...Pair
                  fields {
                    type {
                      ...Pair
                    }
                  }
                }
              }
            }
            fragment Pair on __Type {
              fields {
                type {
                  fields {
                    name
                  }
                }
              }
            }
            """,
            [{"message": DEPTH_EXCEEDED, "locations": [(3, 15)]}],
        )


def describe_pathological_documents():
    def long_fragment_chain_does_not_overflow_the_stack():
        count = 2000
        parts = ["{ __schema { types { ...F0 } } }"]
        for index in range(count):
            inner = f"...F{index + 1}" if index + 1 < count else "name"
            parts.append(f"fragment F{index} on __Type {{ ofType {{ {inner} }} }}")
        assert_valid("\n".join(parts))

    def deep_fragment_chain_over_the_limit_is_reported_once():
        count = 2000
        parts = ["{ __schema { types { ...F0 } } }"]
        for index in range(count):
            if index + 1 < count:
                inner = f"...F{index + 1}"
            else:
                inner = "fields { type { fields { type { fields { name } } } } }"
            parts.append(f"fragment F{index} on __Type {{ ofType {{ {inner} }} }}")
        assert_errors(
            "\n".join(parts),
            [{"message": DEPTH_EXCEEDED, "locations": [(1, 3)]}],
        )

    def wide_selection_set_does_not_overflow_the_stack():
        selections = " ".join(f"f{index}: fields {{ name }}" for index in range(5000))
        assert_valid("{ __schema { types { " + selections + " } } }")


def describe_constructed_ast():
    def deep_query_built_from_ast_nodes():
        field = make_field_node
        document = make_document_node(
            field(
                "__schema",
                field(
                    "types",
                    field(
                        "fields",
                        field(
                            "type",
                            field(
                                "fields",
                                field("type", field("fields", field("name"))),
                            ),
                        ),
                    ),
                ),
            )
        )
        errors = validate(test_schema, document, [MaxIntrospectionDepthRule])
        assert len(errors) == 1
        assert errors[0].message == DEPTH_EXCEEDED

    def aliased_query_built_from_ast_nodes():
        field = make_field_node
        document = make_document_node(
            field(
                "__schema",
                field(
                    "types",
                    field(
                        "fields",
                        field(
                            "type",
                            field(
                                "fields",
                                field(
                                    "type",
                                    field("fields", field("name"), alias="deep"),
                                ),
                            ),
                        ),
                    ),
                ),
                alias="root",
            )
        )
        errors = validate(test_schema, document, [MaxIntrospectionDepthRule])
        assert len(errors) == 1
        assert errors[0].message == DEPTH_EXCEEDED

    def statically_skipped_query_built_from_ast_nodes():
        field = make_field_node
        skip = DirectiveNode(
            name=NameNode(value="skip"),
            arguments=(
                ArgumentNode(
                    name=NameNode(value="if"),
                    value=BooleanValueNode(value=True),
                ),
            ),
        )
        document = make_document_node(
            field(
                "__schema",
                field(
                    "types",
                    field(
                        "fields",
                        field(
                            "type",
                            field(
                                "fields",
                                field("type", field("fields", field("name"))),
                            ),
                        ),
                    ),
                ),
                directives=(skip,),
            )
        )
        assert validate(test_schema, document, [MaxIntrospectionDepthRule]) == []


def describe_interplay_with_other_rules_and_execution():
    def does_not_change_the_order_of_other_validation_errors():
        query = """
        {
          __schema {
            types {
              fields { type { fields { type { fields { name } } } } }
            }
          }
          unknownOne
          unknownTwo
        }
        """
        document = parse(query)
        other_rules = [FieldsOnCorrectTypeRule]
        baseline_errors = validate(test_schema, document, other_rules)
        assert len(baseline_errors) == 2
        combined_errors = validate(
            test_schema, document, [*other_rules, MaxIntrospectionDepthRule]
        )
        depth_errors = [
            error for error in combined_errors if error.message == DEPTH_EXCEEDED
        ]
        other_errors = [
            error for error in combined_errors if error.message != DEPTH_EXCEEDED
        ]
        assert len(depth_errors) == 1
        assert other_errors == baseline_errors

    def valid_introspection_query_executes_normally_by_default():
        result = graphql_sync(test_schema, "{ __schema { types { fields { name } } } }")
        assert not result.errors
        assert result.data is not None

    def regular_query_executes_normally_by_default():
        result = graphql_sync(test_schema, "{ __typename }")
        assert not result.errors
        assert result.data == {"__typename": "QueryRoot"}
