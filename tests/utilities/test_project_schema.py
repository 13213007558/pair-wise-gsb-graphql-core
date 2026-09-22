import json

import pytest

from graphql import (
    GraphQLArgument,
    GraphQLEnumType,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLInputField,
    GraphQLInputObjectType,
    GraphQLInt,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLScalarType,
    GraphQLSchema,
    GraphQLString,
    GraphQLUnionType,
    assert_valid_schema,
    build_schema,
    get_introspection_query,
    get_named_type,
    graphql_sync,
    is_introspection_type,
    is_specified_scalar_type,
    print_schema,
    validate_schema,
)
from graphql.utilities import SchemaProjectionError, project_schema

from ..utils import dedent


def build_test_schema() -> GraphQLSchema:
    """A schema with all kinds of types and behavior attached to them."""

    episode_enum = GraphQLEnumType(
        "Episode",
        values={
            "NEWHOPE": GraphQLEnumValue(4),
            "EMPIRE": GraphQLEnumValue(5),
            "JEDI": GraphQLEnumValue(6),
        },
    )

    secret_scalar = GraphQLScalarType("Secret", serialize=lambda _value: "classified")

    def resolve_character_type(value, _info, _type):
        return "Human" if "homePlanet" in value else "Droid"

    character_interface = GraphQLInterfaceType(
        "Character",
        resolve_type=resolve_character_type,
        fields=lambda: {
            "id": GraphQLField(GraphQLNonNull(GraphQLString)),
            "name": GraphQLField(GraphQLString),
            "secret": GraphQLField(secret_scalar),
            "appearsIn": GraphQLField(GraphQLList(episode_enum)),
        },
    )

    human_type = GraphQLObjectType(
        "Human",
        interfaces=[character_interface],
        is_type_of=lambda value, _info: isinstance(value, dict)
        and "homePlanet" in value,
        fields=lambda: {
            "id": GraphQLField(GraphQLNonNull(GraphQLString)),
            "name": GraphQLField(GraphQLString),
            "secret": GraphQLField(secret_scalar),
            "appearsIn": GraphQLField(GraphQLList(episode_enum)),
            "homePlanet": GraphQLField(GraphQLString),
        },
    )

    droid_type = GraphQLObjectType(
        "Droid",
        interfaces=[character_interface],
        fields=lambda: {
            "id": GraphQLField(GraphQLNonNull(GraphQLString)),
            "name": GraphQLField(GraphQLString),
            "secret": GraphQLField(secret_scalar),
            "appearsIn": GraphQLField(GraphQLList(episode_enum)),
            "primaryFunction": GraphQLField(GraphQLString),
        },
    )

    search_result_union = GraphQLUnionType(
        "SearchResult",
        types=[human_type, droid_type],
        resolve_type=resolve_character_type,
    )

    filter_input = GraphQLInputObjectType(
        "Filter",
        fields={
            "text": GraphQLInputField(GraphQLString),
            "limit": GraphQLInputField(GraphQLNonNull(GraphQLInt)),
        },
    )

    luke = {"id": "1", "name": "Luke", "homePlanet": "Tatooine"}

    query_type = GraphQLObjectType(
        "Query",
        fields={
            "hero": GraphQLField(character_interface, resolve=lambda *_: luke),
            "heroine": GraphQLField(character_interface, resolve=lambda *_: luke),
            "human": GraphQLField(
                human_type,
                args={"id": GraphQLArgument(GraphQLNonNull(GraphQLString))},
                resolve=lambda _obj, _info, id: {**luke, "id": id},
            ),
            "search": GraphQLField(
                GraphQLList(search_result_union),
                args={
                    "filter": GraphQLArgument(filter_input),
                    "text": GraphQLArgument(GraphQLString),
                },
                resolve=lambda *_: [luke],
            ),
            "secret": GraphQLField(secret_scalar, resolve=lambda *_: "hidden"),
            "episode": GraphQLField(episode_enum, resolve=lambda *_: 5),
        },
    )

    mutation_type = GraphQLObjectType(
        "Mutation",
        fields={
            "setName": GraphQLField(
                GraphQLString,
                args={"name": GraphQLArgument(GraphQLNonNull(GraphQLString))},
                resolve=lambda _obj, _info, name: name,
            ),
        },
    )

    async def subscribe_names(_obj, _info):
        yield "Leia"

    subscription_type = GraphQLObjectType(
        "Subscription",
        fields={
            "nameChanged": GraphQLField(
                GraphQLString,
                subscribe=subscribe_names,
                resolve=lambda event, _info: event,
            ),
        },
    )

    return GraphQLSchema(
        query=query_type,
        mutation=mutation_type,
        subscription=subscription_type,
    )


def assert_view_is_self_contained(view: GraphQLSchema) -> None:
    """Every type referenced by a view must be the view's own type object."""
    for type_ in view.type_map.values():
        if is_introspection_type(type_):
            continue
        referenced = []
        if isinstance(type_, (GraphQLObjectType, GraphQLInterfaceType)):
            referenced.extend(type_.interfaces)
            for field in type_.fields.values():
                referenced.append(get_named_type(field.type))
                referenced.extend(
                    get_named_type(arg.type) for arg in field.args.values()
                )
        elif isinstance(type_, GraphQLUnionType):
            referenced.extend(type_.types)
        elif isinstance(type_, GraphQLInputObjectType):
            referenced.extend(
                get_named_type(field.type) for field in type_.fields.values()
            )
        for ref in referenced:
            assert view.type_map[ref.name] is ref


def describe_project_schema():
    def projects_a_schema_to_the_given_coordinates():
        schema = build_schema(
            """
            type Query {
              hero: Character
              secret: String
            }

            interface Character {
              id: ID!
              name: String
            }
            """
        )
        projected = project_schema(schema, ["Query.hero", "Character.id"])
        assert print_schema(projected) == dedent(
            """
            type Query {
              hero: Character
            }

            interface Character {
              id: ID!
            }
            """
        )

    def the_projected_schema_is_valid_and_executable():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.secret"])
        assert_valid_schema(projected)
        assert validate_schema(projected) == []
        result = graphql_sync(projected, "{ secret }")
        assert not result.errors
        assert result.data == {"secret": "classified"}
        # custom scalars are shared by identity to preserve their behavior
        assert projected.type_map["Secret"] is schema.type_map["Secret"]

    def removed_fields_cannot_be_executed_or_introspected():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.secret"])

        result = graphql_sync(projected, "{ heroine { id } }")
        assert result.errors
        assert "Cannot query field 'heroine'" in result.errors[0].message

        introspection = graphql_sync(projected, get_introspection_query())
        assert not introspection.errors
        payload = json.dumps(introspection.data)
        for removed_name in [
            "hero",
            "heroine",
            "human",
            "search",
            "episode",
            "Character",
            "Human",
            "Droid",
            "SearchResult",
            "Filter",
            "Episode",
            "Mutation",
            "Subscription",
        ]:
            assert f'"name": "{removed_name}"' not in payload
        assert '"name": "Secret"' in payload

    def removed_fields_are_not_part_of_error_suggestions():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.hero", "Character.id"])

        original_result = graphql_sync(schema, "{ heroin }")
        assert "'heroine'" in original_result.errors[0].message

        projected_result = graphql_sync(projected, "{ heroin }")
        message = projected_result.errors[0].message
        assert "Cannot query field 'heroin'" in message
        assert "'heroine'" not in message

    def removes_types_that_are_no_longer_referenced():
        schema = build_schema(
            """
            type Query {
              a: A
              b: B
            }

            type A {
              x: String
            }

            type B {
              y: C
            }

            type C {
              z: String
            }
            """
        )
        projected = project_schema(schema, ["Query.a", "A.x"])
        assert print_schema(projected) == dedent(
            """
            type Query {
              a: A
            }

            type A {
              x: String
            }
            """
        )

    def cascades_the_removal_of_types_that_lose_all_members():
        schema = build_schema(
            """
            type Query {
              b: B
            }

            type B {
              y: C
            }

            type C {
              z: String
            }
            """
        )
        # B loses its only field, so B and C are removed as well, which
        # leaves the query root type without any field.
        with pytest.raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.b"])
        assert "query root type 'Query' does not retain any field" in str(
            exc_info.value
        )

    def shrinks_unions_to_their_remaining_members():
        schema = build_schema(
            """
            type Query {
              search: Search
            }

            union Search = Human | Droid

            type Human {
              h: String
            }

            type Droid {
              d: String
            }
            """
        )
        projected = project_schema(schema, ["Query.search", "Human.h"])
        assert print_schema(projected) == dedent(
            """
            type Query {
              search: Search
            }

            union Search = Human

            type Human {
              h: String
            }
            """
        )

    def projects_enum_values():
        schema = build_test_schema()
        projected = project_schema(
            schema, ["Query.episode", "Episode.EMPIRE", "Episode.JEDI"]
        )
        episode = projected.type_map["Episode"]
        assert list(episode.values) == ["EMPIRE", "JEDI"]
        result = graphql_sync(projected, "{ episode }")
        assert not result.errors
        assert result.data == {"episode": "EMPIRE"}
        introspection = graphql_sync(projected, get_introspection_query())
        assert "NEWHOPE" not in json.dumps(introspection.data)

    def drops_optional_args_and_input_fields_with_removed_types():
        schema = build_schema(
            """
            type Query {
              search(filter: Filter, range: Range): String
            }

            input Filter {
              text: String
              range: Range
            }

            input Range {
              min: Int
            }
            """
        )
        projected = project_schema(schema, ["Query.search", "Filter.text"])
        assert print_schema(projected) == dedent(
            """
            type Query {
              search(filter: Filter): String
            }

            input Filter {
              text: String
            }
            """
        )

    def reports_required_arguments_that_block_the_projection():
        schema = build_schema(
            """
            type Query {
              search(filter: Filter!): String
            }

            input Filter {
              text: String
            }
            """
        )
        with pytest.raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.search"])
        message = str(exc_info.value)
        assert (
            "field 'Query.search' requires argument 'filter' of type 'Filter'"
            in message
        )

    def reports_required_input_fields_that_block_the_projection():
        schema = build_schema(
            """
            type Query {
              search(filter: Filter): String
            }

            input Filter {
              text: String
              range: Range!
            }

            input Range {
              min: Int
            }
            """
        )
        with pytest.raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.search", "Filter.text", "Filter.range"])
        message = str(exc_info.value)
        assert (
            "input field 'Filter.range' is required and depends on type 'Range'"
            in message
        )

    def revalidates_interface_implementations():
        schema = build_test_schema()
        with pytest.raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Query.hero", "Character.id", "Human.name"])
        message = str(exc_info.value)
        assert "type 'Human' must provide field 'Character.id'" in message

        projected = project_schema(
            schema,
            [
                "Query.hero",
                "Character.id",
                "Human.id",
                "Human.name",
                "Human.homePlanet",
            ],
        )
        assert_valid_schema(projected)
        result = graphql_sync(
            projected, "{ hero { id ... on Human { homePlanet } } }"
        )
        assert not result.errors
        assert result.data == {"hero": {"id": "1", "homePlanet": "Tatooine"}}

    def fails_when_the_query_root_type_becomes_empty():
        schema = build_test_schema()
        with pytest.raises(SchemaProjectionError) as exc_info:
            project_schema(schema, ["Mutation.setName"])
        assert "query root type 'Query' does not retain any field" in str(
            exc_info.value
        )

    def keeps_or_drops_mutation_and_subscription_roots():
        schema = build_test_schema()
        projected = project_schema(schema, ["Query.secret"])
        assert projected.mutation_type is None
        assert projected.subscription_type is None

        projected = project_schema(
            schema,
            ["Query.secret", "Mutation.setName", "Subscription.nameChanged"],
        )
        assert projected.mutation_type is not None
        assert projected.subscription_type is not None
        original_field = schema.type_map["Subscription"].fields["nameChanged"]
        projected_field = projected.type_map["Subscription"].fields["nameChanged"]
        assert projected_field.subscribe is original_field.subscribe
        assert projected_field.resolve is original_field.resolve
        result = graphql_sync(projected, 'mutation { setName(name: "Leia") }')
        assert not result.errors
        assert result.data == {"setName": "Leia"}

    def preserves_attached_behavior():
        schema = build_test_schema()
        projected = project_schema(
            schema,
            [
                "Query.human",
                "Query.search",
                "Human.id",
                "Human.name",
                "Human.homePlanet",
                "Droid.id",
                "Droid.primaryFunction",
            ],
        )
        original_query = schema.type_map["Query"]
        projected_query = projected.type_map["Query"]
        assert (
            projected_query.fields["human"].resolve
            is original_query.fields["human"].resolve
        )
        assert (
            projected.type_map["Human"].is_type_of
            is schema.type_map["Human"].is_type_of
        )
        assert (
            projected.type_map["SearchResult"].resolve_type
            is schema.type_map["SearchResult"].resolve_type
        )

        result = graphql_sync(
            projected, '{ human(id: "7") { id name homePlanet } }'
        )
        assert not result.errors
        assert result.data == {
            "human": {"id": "7", "name": "Luke", "homePlanet": "Tatooine"}
        }

        result = graphql_sync(
            projected, "{ search { ... on Human { homePlanet } } }"
        )
        assert not result.errors
        assert result.data == {"search": [{"homePlanet": "Tatooine"}]}

    def rejects_invalid_coordinates():
        schema = build_test_schema()
        with pytest.raises(SchemaProjectionError, match="Invalid schema coordinate"):
            project_schema(schema, ["Query"])
        with pytest.raises(SchemaProjectionError, match="Unknown type 'Nope'"):
            project_schema(schema, ["Nope.field"])
        with pytest.raises(SchemaProjectionError, match="Unknown field"):
            project_schema(schema, ["Query.nope"])
        with pytest.raises(SchemaProjectionError, match="Unknown enum value"):
            project_schema(schema, ["Query.episode", "Episode.SITH"])
        with pytest.raises(SchemaProjectionError, match="Invalid schema coordinate"):
            project_schema(schema, ["Secret.value"])

    def keeps_directives_whose_arguments_survive():
        schema = build_schema(
            """
            directive @tag(name: String!) on FIELD_DEFINITION
            directive @filter(rule: Rule!) on FIELD_DEFINITION

            input Rule {
              pattern: String
            }

            type Query {
              a: String @tag(name: "x")
            }
            """
        )
        projected = project_schema(schema, ["Query.a"])
        names = [directive.name for directive in projected.directives]
        assert "tag" in names
        assert "filter" not in names
        assert "Rule" not in projected.type_map

    def does_not_modify_the_original_schema():
        schema = build_test_schema()
        printed_before = print_schema(schema)
        query_fields_before = dict(schema.type_map["Query"].fields)
        human_fields_before = dict(schema.type_map["Human"].fields)

        project_schema(schema, ["Query.secret"])
        project_schema(schema, ["Query.hero", "Character.id"])

        assert print_schema(schema) == printed_before
        assert schema.type_map["Query"].fields == query_fields_before
        assert schema.type_map["Human"].fields == human_fields_before
        assert validate_schema(schema) == []
        result = graphql_sync(schema, "{ heroine { id } }")
        assert not result.errors
        assert result.data == {"heroine": {"id": "1"}}

    def creates_independent_views_of_the_same_schema():
        schema = build_test_schema()
        view_a = project_schema(
            schema, ["Query.human", "Human.id", "Human.name", "Human.homePlanet"]
        )
        view_b = project_schema(
            schema,
            [
                "Query.episode",
                "Episode.EMPIRE",
                "Episode.JEDI",
                "Episode.NEWHOPE",
                "Mutation.setName",
            ],
        )

        # both views are valid and only reference their own type objects
        for view in (view_a, view_b):
            assert validate_schema(view) == []
            assert_view_is_self_contained(view)

        # composite types are distinct objects in each view and the original
        for name, type_a in view_a.type_map.items():
            if is_introspection_type(type_a) or is_specified_scalar_type(type_a):
                continue
            assert type_a is not schema.type_map.get(name)
            assert type_a is not view_b.type_map.get(name)

        # both views can be queried and introspected independently
        result_a = graphql_sync(view_a, '{ human(id: "1") { name } }')
        assert not result_a.errors
        assert result_a.data == {"human": {"name": "Luke"}}
        result_b = graphql_sync(view_b, "{ episode }")
        assert not result_b.errors
        assert result_b.data == {"episode": "EMPIRE"}

        introspection_a = graphql_sync(view_a, get_introspection_query())
        introspection_b = graphql_sync(view_b, get_introspection_query())
        assert not introspection_a.errors
        assert not introspection_b.errors
        payload_a = json.dumps(introspection_a.data)
        payload_b = json.dumps(introspection_b.data)
        assert "Episode" not in payload_a
        assert "Human" not in payload_b
        assert view_a.mutation_type is None
        assert view_b.mutation_type is not None
