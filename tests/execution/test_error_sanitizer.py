import json
from concurrent.futures import ThreadPoolExecutor

from pytest import mark

from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    ErrorSanitizer,
    execute_sync,
    graphql,
    graphql_sync,
    parse,
)
from graphql.error import GraphQLError

SECRET = "p@ssw0rd-t0p-secret"


class WeakSecretError(Exception):
    """Custom scalar error leaking the rejected value and extensions."""

    def __init__(self, value):
        super().__init__(f"Secret {value!r} rejected as too weak")
        self.extensions = {"code": "WEAK_SECRET", "seen": value}


def parse_api_key(value):
    if value != "valid-key":
        raise WeakSecretError(value)
    return value


def parse_api_key_literal(node, _variables=None):
    return parse_api_key(getattr(node, "value", None))


ApiKey = GraphQLScalarType(
    "ApiKey", parse_value=parse_api_key, parse_literal=parse_api_key_literal
)

Credentials = GraphQLInputObjectType(
    "Credentials",
    {
        "username": GraphQLInputField(GraphQLString),
        "password": GraphQLInputField(
            GraphQLNonNull(GraphQLString), extensions={"sensitive": True}
        ),
        "tokens": GraphQLInputField(
            GraphQLList(GraphQLString), extensions={"sensitive": True}
        ),
    },
)

received_values = []


def resolve_auth(_root, _info, apiKey=None):
    received_values.append(apiKey)
    return "ok"


def resolve_login(_root, _info, input=None):
    received_values.append(input)
    if input and input.get("password") == "trigger-resolver-error":
        raise GraphQLError(
            f"Login failed for token {input['tokens'][0]}",
            extensions={"code": "LOGIN_FAILED"},
        )
    return "ok"


Query = GraphQLObjectType(
    "Query",
    {
        "auth": GraphQLField(
            GraphQLString,
            args={
                "apiKey": GraphQLArgument(ApiKey, extensions={"sensitive": True})
            },
            resolve=resolve_auth,
        ),
        "authList": GraphQLField(
            GraphQLString,
            args={
                "apiKeys": GraphQLArgument(
                    GraphQLList(GraphQLNonNull(ApiKey)),
                    extensions={"sensitive": True},
                )
            },
            resolve=resolve_auth,
        ),
        "login": GraphQLField(
            GraphQLString,
            args={"input": GraphQLArgument(Credentials)},
            resolve=resolve_login,
        ),
    },
)

schema = GraphQLSchema(query=Query)

sanitizer = ErrorSanitizer()


def formatted_json(result):
    return json.dumps(result.formatted)


def describe_error_sanitizer():
    def hides_variable_values_in_public_errors():
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)
        error = result.formatted["errors"][0]
        assert "[hidden]" in error["message"]
        # locations and error category are preserved
        assert error["locations"] == [{"line": 1, "column": 7}]
        assert error["extensions"]["code"] == "WEAK_SECRET"
        # extensions from the original error cannot bypass the policy
        assert error["extensions"]["seen"] == "[hidden]"

    def hides_parent_object_previews():
        result = graphql_sync(
            schema,
            "query($c: Credentials) { login(input: $c) }",
            variable_values={"c": {"username": "u", "tokens": [SECRET]}},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)
        message = result.formatted["errors"][0]["message"]
        assert "tokens': [[hidden]]" in message
        assert "'username': 'u'" in message

    def hides_list_elements():
        result = graphql_sync(
            schema,
            "query($ks: [ApiKey!]) { authList(apiKeys: $ks) }",
            variable_values={"ks": [SECRET]},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)
        message = result.formatted["errors"][0]["message"]
        assert "ks[0]" in message
        assert "[hidden]" in message

    def hides_literals_written_in_the_query():
        result = graphql_sync(
            schema,
            '{ auth(apiKey: "' + SECRET + '") }',
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)
        error = result.formatted["errors"][0]
        assert "[hidden]" in error["message"]
        assert error["extensions"]["code"] == "WEAK_SECRET"
        assert error["extensions"]["seen"] == "[hidden]"

    def hides_literals_in_lists_and_input_objects():
        source = (
            '{ login(input: {username: "u", tokens: ["' + SECRET + '"]}) }'
        )
        result = graphql_sync(schema, source, error_sanitizer=sanitizer)
        assert result.errors  # missing required field "password"
        assert SECRET not in formatted_json(result)

    def hides_secrets_in_field_errors_and_keeps_path():
        result = graphql_sync(
            schema,
            "query($c: Credentials) { login(input: $c) }",
            variable_values={
                "c": {
                    "username": "u",
                    "password": "trigger-resolver-error",
                    "tokens": [SECRET],
                }
            },
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)
        error = result.formatted["errors"][0]
        assert error["path"] == ["login"]
        assert error["extensions"]["code"] == "LOGIN_FAILED"

    def does_not_change_coercion_results_or_swallow_errors():
        received_values.clear()
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": "valid-key"},
            error_sanitizer=sanitizer,
        )
        assert result.errors is None
        assert result.data == {"auth": "ok"}
        # the resolver received the original, unmodified value
        assert received_values == ["valid-key"]

    def same_schema_allows_internal_calls_to_keep_diagnostics():
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
        )
        assert result.errors
        assert SECRET in formatted_json(result)

    def original_errors_are_available_via_callback():
        originals = []
        callback_sanitizer = ErrorSanitizer(on_original_error=originals.append)
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
            error_sanitizer=callback_sanitizer,
        )
        assert SECRET not in formatted_json(result)
        assert len(originals) == 1
        assert SECRET in originals[0].message
        # the public error does not carry the original error
        assert result.errors[0].original_error is None

    def original_error_callback_is_off_by_default():
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        # no way to recover the secret from the public errors
        public = result.errors[0]
        assert SECRET not in str(public)
        assert SECRET not in repr(public)
        assert public.original_error is None

    def supports_custom_replacement_marker():
        custom = ErrorSanitizer(replacement="***")
        result = graphql_sync(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
            error_sanitizer=custom,
        )
        assert SECRET not in formatted_json(result)
        assert "***" in result.formatted["errors"][0]["message"]

    def works_with_execute_sync_directly():
        document = parse("query($k: ApiKey!) { auth(apiKey: $k) }")
        result = execute_sync(
            schema,
            document,
            variable_values={"k": SECRET},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)

    @mark.asyncio
    async def works_with_async_graphql():
        result = await graphql(
            schema,
            "query($k: ApiKey!) { auth(apiKey: $k) }",
            variable_values={"k": SECRET},
            error_sanitizer=sanitizer,
        )
        assert result.errors
        assert SECRET not in formatted_json(result)

    def concurrent_requests_do_not_interfere():
        source = "query($k: ApiKey!) { auth(apiKey: $k) }"
        masked = ErrorSanitizer(replacement="MASKED")

        def run(option):
            if option == "sanitized":
                result = graphql_sync(
                    schema,
                    source,
                    variable_values={"k": SECRET},
                    error_sanitizer=sanitizer,
                )
                return SECRET not in formatted_json(result)
            if option == "custom":
                result = graphql_sync(
                    schema,
                    source,
                    variable_values={"k": SECRET},
                    error_sanitizer=masked,
                )
                return (
                    SECRET not in formatted_json(result)
                    and "MASKED" in result.errors[0].message
                )
            result = graphql_sync(
                schema, source, variable_values={"k": SECRET}
            )
            return SECRET in formatted_json(result)

        options = ["sanitized", "custom", "unsanitized"] * 4
        with ThreadPoolExecutor(max_workers=6) as pool:
            assert all(pool.map(run, options))
