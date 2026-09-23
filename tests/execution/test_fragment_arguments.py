from typing import Optional

from graphql.execution import execute_sync
from graphql.execution.collect_fields import collect_fields
from graphql.language import parse
from graphql.utilities import build_schema


def make_schema():
    schema = build_schema(
        """
        type User {
          id: ID
          friends(first: Int): [User]
        }

        type Query {
          user: User
        }
        """
    )

    def resolve_user(_root, _info):
        return {"id": "0"}

    def resolve_friends(user, _info, first: Optional[int] = None):
        return [{"id": str(i + 1)} for i in range(first or 0)]

    schema.get_type("Query").fields["user"].resolve = resolve_user
    schema.get_type("User").fields["friends"].resolve = resolve_friends
    return schema


schema = make_schema()


def execute_query(source: str, variable_values=None):
    doc = parse(source, allow_fragment_arguments=True)
    return execute_sync(schema, doc, variable_values=variable_values)


def friend_ids(data, *path):
    node = data
    for key in path:
        node = node[key]
    return [friend["id"] for friend in node]


def describe_fragment_arguments_execution():
    def argument_is_passed_to_the_fragment():
        result = execute_query(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 5) }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2", "3", "4", "5"]

    def default_value_is_used_when_argument_is_missing():
        result = execute_query(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2", "3"]

    def fragment_can_be_reused_with_different_arguments():
        result = execute_query(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              small: user { ...Card(size: 2) }
              large: user { ...Card(size: 4) }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "small", "friends") == ["1", "2"]
        assert friend_ids(result.data, "large", "friends") == ["1", "2", "3", "4"]

    def parameter_shadows_operation_variable():
        result = execute_query(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query ($size: Int = 1) {
              user { ...Card }
            }
            """
        )
        assert result.errors is None
        # the fragment parameter (default 3) shadows the operation variable
        assert friend_ids(result.data, "user", "friends") == ["1", "2", "3"]

    def unbound_parameter_shadows_operation_variable():
        result = execute_query(
            """
            fragment Card($size: Int) on User {
              friends(first: $size) { id }
            }
            query ($size: Int = 2) {
              user { ...Card }
            }
            """
        )
        assert result.errors is None
        # the unbound fragment parameter shadows the operation variable,
        # so no argument is passed to the friends field
        assert friend_ids(result.data, "user", "friends") == []

    def free_variable_resolves_to_operation_variable():
        result = execute_query(
            """
            fragment Card on User {
              friends(first: $size) { id }
            }
            query ($size: Int = 2) {
              user { ...Card }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2"]

    def arguments_are_evaluated_at_the_call_site():
        result = execute_query(
            """
            fragment Inner($n: Int!) on User {
              friends(first: $n) { id }
            }
            fragment Outer($n: Int!) on User {
              ...Inner(n: 2)
            }
            query {
              user { ...Outer(n: 5) }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2"]

    def nested_calls_have_their_own_scope():
        result = execute_query(
            """
            fragment Inner($n: Int!) on User {
              friends(first: $n) { id }
            }
            fragment Outer($n: Int!) on User {
              ...Inner(n: $n)
            }
            query {
              user { ...Outer(n: 4) }
            }
            """
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2", "3", "4"]

    def argument_value_can_be_an_operation_variable():
        result = execute_query(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query ($s: Int!) {
              user { ...Card(size: $s) }
            }
            """,
            variable_values={"s": 5},
        )
        assert result.errors is None
        assert friend_ids(result.data, "user", "friends") == ["1", "2", "3", "4", "5"]

    def missing_required_parameter_is_an_error():
        result = execute_query(
            """
            fragment Card($size: Int!) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card }
            }
            """
        )
        assert result.data == {"user": None}
        assert [error.message for error in result.errors] == [
            "Fragment 'Card' argument 'size' of required type 'Int!'"
            " was not provided."
        ]

    def same_fragment_with_different_arguments_is_not_deduplicated():
        doc = parse(
            """
            fragment Card($size: Int = 3) on User {
              friends(first: $size) { id }
            }
            query {
              user { ...Card(size: 2) ...Card(size: 4) ...Card(size: 2) }
            }
            """,
            allow_fragment_arguments=True,
        )
        operation = doc.definitions[1]
        fragments = {
            definition.name.value: definition
            for definition in doc.definitions
            if definition.kind == "fragment_definition"
        }
        user_selection_set = operation.selection_set.selections[0].selection_set
        fields = collect_fields(
            schema,
            fragments,
            {},
            schema.get_type("User"),
            user_selection_set,
        )
        # the two spreads with size: 2 are collected only once, while the
        # spread with size: 4 is collected separately
        assert set(fields) == {"friends"}
        assert len(fields["friends"]) == 2
