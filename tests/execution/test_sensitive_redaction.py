import json
from concurrent.futures import ThreadPoolExecutor

from pytest import mark

from graphql import (
    GraphQLArgument,
    GraphQLError,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    SensitiveErrorRedaction,
    graphql,
    graphql_sync,
)
from graphql.execution import execute_sync
from graphql.language import parse

SECRET = "s3cr3t-token-123"


def parse_password_value(value):
    raise GraphQLError(
        f"Invalid password '{value}'",
        extensions={"code": "BAD_PASSWORD", "hint": f"got '{value}'"},
    )


def parse_password_literal(node, _variables=None):
    return parse_password_value(getattr(node, "value", None))


PasswordScalar = GraphQLScalarType(
    name="Password",
    parse_value=parse_password_value,
    parse_literal=parse_password_literal,
)

CredentialsInput = GraphQLInputObjectType(
    "Credentials",
    {
        "username": GraphQLInputField(GraphQLString),
        "password": GraphQLInputField(PasswordScalar, extensions={"sensitive": True}),
        "tokens": GraphQLInputField(
            GraphQLList(GraphQLString), extensions={"sensitive": True}
        ),
    },
)

NestedInput = GraphQLInputObjectType(
    "Nested",
    {
        "credentials": GraphQLInputField(CredentialsInput),
    },
)


def resolve_login(_root, info, **_args):
    return "logged in"


def resolve_boom(_root, _info, password=None):
    error = GraphQLError(f"Login failed for password '{password}'")
    error.extensions = {"code": "LOGIN_FAILED", "debug": {"password": password}}
    raise error


QueryType = GraphQLObjectType(
    "Query",
    {
        "login": GraphQLField(
            GraphQLString,
            args={
                "creds": GraphQLArgument(CredentialsInput),
                "nested": GraphQLArgument(NestedInput),
                "apiKey": GraphQLArgument(
                    GraphQLString, extensions={"sensitive": True}
                ),
            },
            resolve=resolve_login,
        ),
        "boom": GraphQLField(
            GraphQLString,
            args={
                "password": GraphQLArgument(
                    GraphQLString, extensions={"sensitive": True}
                ),
            },
            resolve=resolve_boom,
        ),
    },
)

schema = GraphQLSchema(query=QueryType)


def formatted_json(result):
    return json.dumps(result.formatted)


def describe_sensitive_error_redaction():
    def does_not_change_anything_without_the_option():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        result = graphql_sync(schema, query, variable_values=variables)
        assert result.errors
        assert SECRET in formatted_json(result)
        assert "<redacted>" not in formatted_json(result)

    def redacts_secret_in_variable_coercion_errors():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.data is None
        assert result.errors and len(result.errors) == 1
        output = formatted_json(result)
        assert SECRET not in output
        assert "<redacted>" in output
        # locations and error category are preserved
        (error,) = result.formatted["errors"]
        assert error["locations"]
        assert error["extensions"]["code"] == "BAD_PASSWORD"
        assert error["extensions"]["redacted"] is True

    def redacts_secret_in_parent_object_preview():
        # an unknown field triggers an error that previews the whole object
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET, "unknown": "x"}}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        assert "password" in output  # the field name is still visible
        assert "<redacted>" in output

    def redacts_long_secrets_that_get_truncated_in_previews():
        long_secret = "s3cr3t-" + "x" * 300
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {
            "creds": {"username": "u", "password": long_secret, "unknown": "x"}
        }
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert long_secret not in output
        # no visible fragment of the truncated secret remains either
        assert long_secret[:100] not in output
        assert long_secret[-100:] not in output

    def redacts_secret_in_list_elements():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {
            "creds": {"username": "u", "password": "ok", "tokens": [SECRET, 42]}
        }
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        # the non-string list element is redacted as well
        assert "42" not in output

    def redacts_secret_in_custom_scalar_error_text():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        (error,) = result.errors
        assert SECRET not in error.message
        assert "Invalid password" in error.message
        assert "<redacted>" in error.message

    def redacts_secret_in_nested_input_objects():
        query = "query($nested: Nested) { login(nested: $nested) }"
        variables = {"nested": {"credentials": {"password": SECRET}}}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        assert SECRET not in formatted_json(result)

    def redacts_secret_variable_used_for_sensitive_argument():
        query = "query($key: String!) { login(apiKey: $key) }"
        variables = {"key": 12345}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert "12345" not in output
        assert "<redacted>" in output

    def redacts_secret_literals_in_validation_errors():
        query = '{ login(creds: {username: "u", password: "%s"}) }' % SECRET
        result = graphql_sync(schema, query, error_redaction=SensitiveErrorRedaction())
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        assert "<redacted>" in output

    def redacts_secret_literals_when_executing_directly():
        document = parse(
            '{ login(creds: {username: "u", password: "%s"}) }' % SECRET
        )
        result = execute_sync(
            schema, document, error_redaction=SensitiveErrorRedaction()
        )
        assert result.errors
        assert SECRET not in formatted_json(result)

    def redacts_secret_in_argument_literal_preview():
        # an unknown argument value triggers "has invalid value" with the
        # literal printed into the message
        document = parse("{ login(apiKey: %d) }" % 987654)
        result = execute_sync(
            schema, document, error_redaction=SensitiveErrorRedaction()
        )
        assert result.errors
        output = formatted_json(result)
        assert "987654" not in output

    def redacts_secret_raised_by_resolvers_and_original_error_extensions():
        query = 'query($password: String!) { boom(password: $password) }'
        variables = {"password": SECRET}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        (error,) = result.formatted["errors"]
        # path, locations and error category are preserved
        assert error["path"] == ["boom"]
        assert error["locations"]
        assert error["extensions"]["code"] == "LOGIN_FAILED"
        assert error["extensions"]["redacted"] is True

    def redacts_extensions_derived_from_original_error():
        class SensitiveFailure(Exception):
            def __init__(self, value):
                super().__init__(f"failure with '{value}'")
                self.extensions = {"code": "SENSITIVE_FAILURE", "secret": value}

        def parse_failing(value):
            raise SensitiveFailure(value)

        failing_scalar = GraphQLScalarType(
            name="Failing", parse_value=parse_failing
        )
        input_type = GraphQLInputObjectType(
            "FailingInput",
            {
                "secret": GraphQLInputField(
                    failing_scalar, extensions={"sensitive": True}
                )
            },
        )
        query_type = GraphQLObjectType(
            "Query",
            {
                "check": GraphQLField(
                    GraphQLString,
                    args={"input": GraphQLArgument(input_type)},
                    resolve=lambda *_args, **_kwargs: "ok",
                )
            },
        )
        local_schema = GraphQLSchema(query=query_type)
        query = "query($input: FailingInput) { check(input: $input) }"
        variables = {"input": {"secret": SECRET}}
        result = graphql_sync(
            local_schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        (error,) = result.formatted["errors"]
        assert error["extensions"]["code"] == "SENSITIVE_FAILURE"
        assert error["extensions"]["secret"] == "<redacted>"

    def keeps_original_error_for_internal_diagnostics_via_callback():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        originals = []
        redaction = SensitiveErrorRedaction(on_original_error=originals.append)
        result = graphql_sync(
            schema, query, variable_values=variables, error_redaction=redaction
        )
        assert result.errors
        # the public result is redacted
        assert SECRET not in formatted_json(result)
        # the callback received the original, unredacted errors
        assert len(originals) == len(result.errors)
        assert any(SECRET in error.message for error in originals)

    def callback_is_off_by_default():
        redaction = SensitiveErrorRedaction()
        assert redaction.on_original_error is None

    def does_not_change_coercion_results_or_swallow_errors():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        redacted = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        plain = graphql_sync(schema, query, variable_values=variables)
        # same number of errors, same locations
        assert len(redacted.errors) == len(plain.errors)
        for redacted_error, plain_error in zip(redacted.errors, plain.errors):
            assert redacted_error.locations == plain_error.locations
            assert redacted_error.path == plain_error.path

    def valid_requests_are_not_affected():
        query = "{ login }"
        result = graphql_sync(
            schema, query, error_redaction=SensitiveErrorRedaction()
        )
        assert not result.errors
        assert result.data == {"login": "logged in"}

    def supports_a_custom_marker():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        variables = {"creds": {"username": "u", "password": SECRET}}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(marker="***"),
        )
        output = formatted_json(result)
        assert SECRET not in output
        assert "***" in output
        assert "<redacted>" not in output

    def concurrent_calls_do_not_interfere():
        other_secret = "0th3r-secret"
        query = "query($creds: Credentials) { login(creds: $creds) }"

        def run(secret, marker):
            variables = {"creds": {"username": "u", "password": secret}}
            result = graphql_sync(
                schema,
                query,
                variable_values=variables,
                error_redaction=SensitiveErrorRedaction(marker=marker),
            )
            return formatted_json(result)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(run, SECRET, "<redacted>"),
                pool.submit(run, other_secret, "***"),
            ]
            first, second = (future.result() for future in futures)

        assert SECRET not in first and other_secret not in first
        assert "<redacted>" in first and "***" not in first
        assert SECRET not in second and other_secret not in second
        assert "***" in second and "<redacted>" not in second

    def shared_configuration_is_safe_for_concurrent_calls():
        query = "query($creds: Credentials) { login(creds: $creds) }"
        redaction = SensitiveErrorRedaction()
        secrets = ["shared-secret-%d" % index for index in range(8)]

        def run(secret):
            variables = {"creds": {"username": "u", "password": secret}}
            result = graphql_sync(
                schema, query, variable_values=variables, error_redaction=redaction
            )
            return formatted_json(result)

        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(run, secrets))

        for secret, output in zip(secrets, outputs):
            assert secret not in output
            assert "<redacted>" in output

    def non_null_list_of_sensitive_scalars():
        input_type = GraphQLInputObjectType(
            "Tokens",
            {
                "tokens": GraphQLInputField(
                    GraphQLNonNull(GraphQLList(GraphQLNonNull(PasswordScalar))),
                    extensions={"sensitive": True},
                )
            },
        )
        query_type = GraphQLObjectType(
            "Query",
            {
                "check": GraphQLField(
                    GraphQLString,
                    args={"tokens": GraphQLArgument(input_type)},
                    resolve=lambda *_args, **_kwargs: "ok",
                )
            },
        )
        local_schema = GraphQLSchema(query=query_type)
        query = "query($tokens: Tokens) { check(tokens: $tokens) }"
        variables = {"tokens": {"tokens": [SECRET]}}
        result = graphql_sync(
            local_schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        assert SECRET not in formatted_json(result)

    def redacted_public_error_drops_the_original_error():
        query = 'query($password: String!) { boom(password: $password) }'
        variables = {"password": SECRET}
        result = graphql_sync(
            schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        (error,) = result.errors
        assert error.original_error is None
        assert SECRET not in formatted_json(result)

    def syntax_errors_are_not_redacted():
        result = graphql_sync(
            schema, "{ login(", error_redaction=SensitiveErrorRedaction()
        )
        assert result.errors
        (error,) = result.errors
        assert "Syntax Error" in error.message

    @mark.asyncio
    async def redacts_errors_from_async_resolvers():
        async def async_boom(_root, _info, password=None):
            raise GraphQLError(f"Async failure with '{password}'")

        query_type = GraphQLObjectType(
            "Query",
            {
                "boom": GraphQLField(
                    GraphQLString,
                    args={
                        "password": GraphQLArgument(
                            GraphQLString, extensions={"sensitive": True}
                        )
                    },
                    resolve=async_boom,
                )
            },
        )
        local_schema = GraphQLSchema(query=query_type)
        query = "query($password: String!) { boom(password: $password) }"
        variables = {"password": SECRET}
        result = await graphql(
            local_schema,
            query,
            variable_values=variables,
            error_redaction=SensitiveErrorRedaction(),
        )
        assert result.errors
        output = formatted_json(result)
        assert SECRET not in output
        assert "<redacted>" in output
