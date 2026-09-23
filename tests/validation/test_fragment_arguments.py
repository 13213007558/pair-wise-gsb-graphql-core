from typing import List

from graphql.language import parse
from graphql.utilities import build_schema
from graphql.validation import validate

schema = build_schema(
    """
    type User {
      id: ID
      name(format: String): String
      friends(first: Int): [User]
    }

    type Query {
      user: User
    }
    """
)


def assert_fragment_errors(query_str: str, messages: List[str]) -> None:
    doc = parse(query_str, allow_fragment_arguments=True)
    errors = validate(schema, doc)
    assert [error.message for error in errors] == messages


def assert_valid(query_str: str) -> None:
    assert_fragment_errors(query_str, [])


def describe_fragment_arguments_validation():
    def valid_fragment_with_arguments():
        assert_valid(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 5) }
            }
            """
        )

    def valid_when_required_parameter_is_provided():
        assert_valid(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 5) }
            }
            """
        )

    def missing_required_parameter():
        assert_fragment_errors(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card }
            }
            """,
            [
                "Fragment 'Card' argument 'size' of type 'Int!' is required,"
                " but it was not provided."
            ],
        )

    def unknown_argument():
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(count: 5) }
            }
            """,
            ["Unknown argument 'count' on fragment 'Card'."],
        )

    def duplicate_argument():
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 1, size: 2) }
            }
            """,
            ["There can be only one argument named 'size'."],
        )

    def argument_value_of_wrong_type():
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: "five") }
            }
            """,
            ['Int cannot represent non-integer value: "five"'],
        )

    def duplicate_parameter_names():
        assert_fragment_errors(
            """
            fragment Card($size: Int, $size: Int) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 1) }
            }
            """,
            ["There can be only one variable named '$size'."],
        )

    def parameter_with_non_input_type():
        assert_fragment_errors(
            """
            fragment Card($friend: User) on User {
              id
            }
            query {
              user { ...Card }
            }
            """,
            ["Variable '$friend' cannot be non-input type 'User'."],
        )

    def variable_in_argument_position_with_wrong_type():
        assert_fragment_errors(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query Q($s: Int) {
              user { ...Card(size: $s) }
            }
            """,
            [
                "Variable '$s' of type 'Int' used in position"
                " expecting type 'Int!'."
            ],
        )

    def fragment_parameter_used_in_wrong_position():
        assert_fragment_errors(
            """
            fragment Card($size: String) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: "5") }
            }
            """,
            [
                "Variable '$size' of type 'String' used in position"
                " expecting type 'Int'."
            ],
        )

    def undefined_variable_inside_fragment():
        assert_fragment_errors(
            """
            fragment Card on User {
              friends(first: $size) { id }
            }
            query Q {
              user { ...Card }
            }
            """,
            ["Variable '$size' is not defined by operation 'Q'."],
        )

    def fragment_parameter_shadows_operation_variable():
        # The operation variable is shadowed by the fragment parameter,
        # so it is never used and needs not be defined for the fragment.
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query Q($size: Int = 1) {
              user { ...Card }
            }
            """,
            ["Variable '$size' is never used in operation 'Q'."],
        )

    def free_variable_in_fragment_resolves_to_operation_variable():
        assert_valid(
            """
            fragment Card on User {
              friends(first: $size) { id }
            }
            query Q($size: Int = 2) {
              user { ...Card }
            }
            """
        )

    def variable_in_spread_argument_uses_call_site_scope():
        assert_valid(
            """
            fragment Inner($n: Int!) on User {
              friends(first: $n) { id }
            }
            fragment Outer($n: Int!) on User {
              ...Inner(n: $n)
            }
            query {
              user { ...Outer(n: 5) }
            }
            """
        )

    def fragment_cycle_with_arguments():
        assert_fragment_errors(
            """
            fragment Card($size: Int) on User {
              friends(first: $size) { ...Card(size: 1) }
            }
            query {
              user { ...Card(size: 5) }
            }
            """,
            ["Cannot spread fragment 'Card' within itself."],
        )

    def same_fragment_with_different_arguments_conflicts():
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 2) ...Card(size: 4) }
            }
            """,
            [
                "Fields 'friends' conflict because they have differing"
                " arguments. Use different aliases on the fields to fetch"
                " both if this was intentional."
            ],
        )

    def same_fragment_with_same_arguments_merges():
        assert_valid(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 2) ...Card(size: 2) }
            }
            """
        )

    def same_fragment_with_default_and_explicit_default_merges():
        assert_valid(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card ...Card(size: 3) }
            }
            """
        )

    def same_fragment_with_default_and_other_argument_conflicts():
        assert_fragment_errors(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card ...Card(size: 4) }
            }
            """,
            [
                "Fields 'friends' conflict because they have differing"
                " arguments. Use different aliases on the fields to fetch"
                " both if this was intentional."
            ],
        )

    def same_fragment_with_same_variable_argument_merges():
        assert_valid(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query Q($s: Int!) {
              user { ...Card(size: $s) ...Card(size: $s) }
            }
            """
        )

    def same_fragment_with_different_variable_arguments_conflicts():
        assert_fragment_errors(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query Q($s: Int!, $t: Int!) {
              user { ...Card(size: $s) ...Card(size: $t) }
            }
            """,
            [
                "Fields 'friends' conflict because they have differing"
                " arguments. Use different aliases on the fields to fetch"
                " both if this was intentional."
            ],
        )

    def nested_fragment_arguments_conflict():
        assert_fragment_errors(
            """
            fragment Inner($n: Int!) on User {
              friends(first: $n) { id }
            }
            fragment OuterA($n: Int!) on User {
              ...Inner(n: $n)
            }
            fragment OuterB($n: Int!) on User {
              ...Inner(n: 4)
            }
            query {
              user { ...OuterA(n: 2) ...OuterB(n: 2) }
            }
            """,
            [
                "Fields 'friends' conflict because they have differing"
                " arguments. Use different aliases on the fields to fetch"
                " both if this was intentional."
            ],
        )
