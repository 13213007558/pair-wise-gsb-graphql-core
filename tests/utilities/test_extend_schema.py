from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TypeAlias

import pytest

from graphql import graphql_sync
from graphql.error import GraphQLSyntaxError
from graphql.language import (
    DirectiveLocation,
    DocumentNode,
    FieldDefinitionNode,
    NamedTypeNode,
    NameNode,
    ObjectTypeExtensionNode,
    SourceLocation,
    parse,
    print_ast,
)
from graphql.type import (
    GraphQLArgument,
    GraphQLBoolean,
    GraphQLDirective,
    GraphQLEnumValue,
    GraphQLField,
    GraphQLFloat,
    GraphQLID,
    GraphQLInputField,
    GraphQLInt,
    GraphQLNamedType,
    GraphQLSchema,
    GraphQLString,
    assert_directive,
    assert_enum_type,
    assert_input_object_type,
    assert_interface_type,
    assert_object_type,
    assert_scalar_type,
    assert_union_type,
    is_non_null_type,
    specified_directives,
    validate_schema,
)
from graphql.utilities import (
    build_ast_schema,
    build_schema,
    concat_ast,
    extend_schema,
    introspection_from_schema,
    lexicographic_sort_schema,
    print_schema,
)
from graphql.validation.validate import validate_sdl

from ..utils import dedent

TypeWithAstNode: TypeAlias = (
    GraphQLArgument
    | GraphQLDirective
    | GraphQLEnumValue
    | GraphQLField
    | GraphQLInputField
    | GraphQLNamedType
    | GraphQLSchema
)

TypeWithExtensionAstNodes: TypeAlias = (
    GraphQLDirective | GraphQLNamedType | GraphQLSchema
)


def expect_extension_ast_nodes(obj: TypeWithExtensionAstNodes, expected: str) -> None:
    assert obj is not None
    assert obj.extension_ast_nodes is not None
    assert "\n\n".join(print_ast(node) for node in obj.extension_ast_nodes) == expected


def expect_ast_node(obj: TypeWithAstNode, expected: str) -> None:
    assert obj is not None
    assert obj.ast_node is not None
    assert print_ast(obj.ast_node) == expected


def expect_schema_changes(
    schema: GraphQLSchema, extended_schema: GraphQLSchema, expected: str
) -> None:
    schema_definitions = {
        print_ast(node) for node in parse(print_schema(schema)).definitions
    }
    assert (
        "\n\n".join(
            schema_def
            for schema_def in (
                print_ast(node)
                for node in parse(print_schema(extended_schema)).definitions
            )
            if schema_def not in schema_definitions
        )
        == expected
    )


def describe_extend_schema():
    def returns_the_original_schema_when_there_are_no_type_definitions():
        schema = build_schema("type Query")
        extended_schema = extend_schema(schema, parse("{ field }"))
        assert extended_schema == schema

    def can_be_used_for_limited_execution():
        schema = build_schema("type Query")
        extend_ast = parse(
            """
            extend type Query {
              newField: String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        result = graphql_sync(
            schema=extended_schema, source="{ newField }", root_value={"newField": 123}
        )
        assert result == ({"newField": "123"}, None)

    def does_not_modify_built_in_types_and_directives():
        schema = build_schema(
            """
              type Query {
                str: String
                int: Int
                float: Float
                id: ID
                bool: Boolean
              }
            """
        )

        extension_sdl = dedent(
            """
            extend type Query {
              foo: String
            }
            """
        )

        extended_schema = extend_schema(schema, parse(extension_sdl))

        # built-ins are used
        assert extended_schema.get_type("Int") is GraphQLInt
        assert extended_schema.get_type("Float") is GraphQLFloat
        assert extended_schema.get_type("String") is GraphQLString
        assert extended_schema.get_type("Boolean") is GraphQLBoolean
        assert extended_schema.get_type("ID") is GraphQLID

        assert extended_schema.directives == specified_directives

    def preserves_original_schema_config():
        description = "A schema description"
        extensions = {"foo": "bar"}
        schema = GraphQLSchema(description=description, extensions=extensions)

        extended_schema = extend_schema(schema, parse("scalar Bar"))

        assert extended_schema.description == description
        assert extended_schema.extensions is extensions

    def extends_objects_by_adding_new_fields():
        schema = build_schema(
            '''
            type Query {
              someObject: SomeObject
            }

            type SomeObject implements AnotherInterface & SomeInterface {
              self: SomeObject
              tree: [SomeObject]!
              """Old field description."""
              oldField: String
            }

            interface SomeInterface {
              self: SomeInterface
            }

            interface AnotherInterface {
              self: SomeObject
            }
            '''
        )
        extension_sdl = dedent(
            '''
            extend type SomeObject {
              """New field description."""
              newField(arg: Boolean): String
            }
          '''
        )
        extended_schema = extend_schema(schema, parse(extension_sdl))

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                '''
                type SomeObject implements AnotherInterface & SomeInterface {
                  self: SomeObject
                  tree: [SomeObject]!
                  """Old field description."""
                  oldField: String
                  """New field description."""
                  newField(arg: Boolean): String
                }
                '''
            ),
        )

    def extends_objects_with_standard_type_fields():
        schema = build_schema("type Query")

        # Only String and Boolean are used by introspection types
        assert schema.get_type("Int") is None
        assert schema.get_type("Float") is None
        assert schema.get_type("String") is GraphQLString
        assert schema.get_type("Boolean") is GraphQLBoolean
        assert schema.get_type("ID") is None

        extend_ast = parse(
            """
            extend type Query {
              bool: Boolean
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        assert extended_schema.get_type("Int") is None
        assert extended_schema.get_type("Float") is None
        assert extended_schema.get_type("String") is GraphQLString
        assert extended_schema.get_type("Boolean") is GraphQLBoolean
        assert extended_schema.get_type("ID") is None

        extend_twice_ast = parse(
            """
            extend type Query {
              int: Int
              float: Float
              id: ID
            }
            """
        )
        extended_twice_schema = extend_schema(schema, extend_twice_ast)

        assert validate_schema(extended_twice_schema) == []
        assert extended_twice_schema.get_type("Int") is GraphQLInt
        assert extended_twice_schema.get_type("Float") is GraphQLFloat
        assert extended_twice_schema.get_type("String") is GraphQLString
        assert extended_twice_schema.get_type("Boolean") is GraphQLBoolean
        assert extended_twice_schema.get_type("ID") is GraphQLID

    def extends_enums_by_adding_new_values():
        schema = build_schema(
            '''
            type Query {
              someEnum(arg: SomeEnum): SomeEnum
            }

            directive @foo(arg: SomeEnum) on SCHEMA

            enum SomeEnum {
              """Old value description."""
              OLD_VALUE
            }
            '''
        )
        extend_ast = parse(
            '''
            extend enum SomeEnum {
              """New value description."""
              NEW_VALUE
            }
            '''
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                '''
                enum SomeEnum {
                  """Old value description."""
                  OLD_VALUE
                  """New value description."""
                  NEW_VALUE
                }
                '''
            ),
        )

    def extends_unions_by_adding_new_types():
        schema = build_schema(
            """
            type Query {
              someUnion: SomeUnion
            }

            union SomeUnion = Foo | Biz

            type Foo { foo: String }
            type Biz { biz: String }
            type Bar { bar: String }
            """
        )
        extend_ast = parse(
            """
            extend union SomeUnion = Bar
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                union SomeUnion = Foo | Biz | Bar
                """
            ),
        )

    def allows_extension_of_union_by_adding_itself():
        schema = build_schema(
            """
            union SomeUnion
            """
        )
        extend_ast = parse(
            """
            extend union SomeUnion = SomeUnion
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema)
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                union SomeUnion = SomeUnion
                """
            ),
        )

    def extends_inputs_by_adding_new_fields():
        schema = build_schema(
            '''
            type Query {
              someInput(arg: SomeInput): String
            }

            directive @foo(arg: SomeInput) on SCHEMA

            input SomeInput {
              """Old field description."""
              oldField: String
            }
            '''
        )
        extend_ast = parse(
            '''
            extend input SomeInput {
              """New field description."""
              newField: String
            }
            '''
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                '''
                input SomeInput {
                  """Old field description."""
                  oldField: String
                  """New field description."""
                  newField: String
                }
                '''
            ),
        )

    def extends_scalars_by_adding_new_directives():
        schema = build_schema(
            """
            type Query {
              someScalar(arg: SomeScalar): SomeScalar
            }

            directive @foo(arg: SomeScalar) on SCALAR

            input FooInput {
              foo: SomeScalar
            }

            scalar SomeScalar
            """
        )
        extension_sdl = dedent(
            """
            extend scalar SomeScalar @foo
            """
        )
        extended_schema = extend_schema(schema, parse(extension_sdl))
        some_scalar = assert_scalar_type(extended_schema.get_type("SomeScalar"))

        assert validate_schema(extended_schema) == []
        expect_extension_ast_nodes(some_scalar, extension_sdl)

    def extends_scalars_by_adding_specified_by_directive():
        schema = build_schema(
            """
            type Query {
              foo: Foo
            }

            scalar Foo

            directive @foo on SCALAR
            """
        )
        extension_sdl = dedent(
            """
            extend scalar Foo @foo

            extend scalar Foo @specifiedBy(url: "https://example.com/foo_spec")
            """
        )

        extended_schema = extend_schema(schema, parse(extension_sdl))
        foo = assert_scalar_type(extended_schema.get_type("Foo"))

        assert foo.specified_by_url == "https://example.com/foo_spec"

        assert validate_schema(extended_schema) == []
        expect_extension_ast_nodes(foo, extension_sdl)

    def builds_scalars_with_specified_by_directive_from_extensions():
        schema = GraphQLSchema()
        extension_sdl = dedent(
            """
            schema {
              query: Query
            }

            type Query {
              foo: Foo
            }

            scalar Foo

            extend scalar Foo @specifiedBy(url: "https://example.com/foo_spec")
            """
        )

        extended_schema = extend_schema(schema, parse(extension_sdl))
        foo = assert_scalar_type(extended_schema.get_type("Foo"))

        assert foo.specified_by_url == "https://example.com/foo_spec"

        assert validate_schema(extended_schema) == []
        expect_ast_node(foo, "scalar Foo")
        expect_extension_ast_nodes(
            foo, 'extend scalar Foo @specifiedBy(url: "https://example.com/foo_spec")'
        )

    def correctly_assigns_ast_nodes_to_new_and_extended_types():
        schema = build_schema(
            """
            type Query

            scalar SomeScalar
            enum SomeEnum
            union SomeUnion
            input SomeInput
            type SomeObject
            interface SomeInterface

            directive @foo on SCALAR
            """
        )
        first_extension_ast = parse(
            """
            extend type Query {
              newField(testArg: TestInput): TestEnum
            }

            extend scalar SomeScalar @foo

            extend enum SomeEnum {
              NEW_VALUE
            }

            extend union SomeUnion = SomeObject

            extend input SomeInput {
              newField: String
            }

            extend interface SomeInterface {
              newField: String
            }

            enum TestEnum {
              TEST_VALUE
            }

            input TestInput {
              testInputField: TestEnum
            }
            """
        )
        extended_schema = extend_schema(schema, first_extension_ast)

        second_extension_ast = parse(
            """
            extend type Query {
              oneMoreNewField: TestUnion
            }

            extend scalar SomeScalar @test

            extend enum SomeEnum {
              ONE_MORE_NEW_VALUE
            }

            extend union SomeUnion = TestType

            extend input SomeInput {
              oneMoreNewField: String
            }

            extend interface SomeInterface {
              oneMoreNewField: String
            }

            union TestUnion = TestType

            interface TestInterface {
              interfaceField: String
            }

            type TestType implements TestInterface {
              interfaceField: String
            }

            directive @test(arg: Int) repeatable on FIELD | SCALAR
            """
        )
        extended_twice_schema = extend_schema(extended_schema, second_extension_ast)

        extend_in_one_go_schema = extend_schema(
            schema, concat_ast([first_extension_ast, second_extension_ast])
        )
        assert print_schema(extend_in_one_go_schema) == print_schema(
            extended_twice_schema
        )

        query = assert_object_type(extended_twice_schema.get_type("Query"))
        some_enum = assert_enum_type(extended_twice_schema.get_type("SomeEnum"))
        some_union = assert_union_type(extended_twice_schema.get_type("SomeUnion"))
        some_scalar = assert_scalar_type(extended_twice_schema.get_type("SomeScalar"))
        some_input = assert_input_object_type(
            extended_twice_schema.get_type("SomeInput")
        )
        some_interface = assert_interface_type(
            extended_twice_schema.get_type("SomeInterface")
        )

        test_input = assert_input_object_type(
            extended_twice_schema.get_type("TestInput")
        )
        test_enum = assert_enum_type(extended_twice_schema.get_type("TestEnum"))
        test_union = assert_union_type(extended_twice_schema.get_type("TestUnion"))
        test_type = assert_object_type(extended_twice_schema.get_type("TestType"))
        test_interface = assert_interface_type(
            extended_twice_schema.get_type("TestInterface")
        )
        test_directive = assert_directive(extended_twice_schema.get_directive("test"))

        assert test_type.extension_ast_nodes == ()
        assert test_enum.extension_ast_nodes == ()
        assert test_union.extension_ast_nodes == ()
        assert test_input.extension_ast_nodes == ()
        assert test_interface.extension_ast_nodes == ()

        assert query.extension_ast_nodes
        assert len(query.extension_ast_nodes) == 2
        assert some_scalar.extension_ast_nodes
        assert len(some_scalar.extension_ast_nodes) == 2
        assert some_enum.extension_ast_nodes
        assert len(some_enum.extension_ast_nodes) == 2
        assert some_union.extension_ast_nodes
        assert len(some_union.extension_ast_nodes) == 2
        assert some_input.extension_ast_nodes
        assert len(some_input.extension_ast_nodes) == 2
        assert some_interface.extension_ast_nodes
        assert len(some_interface.extension_ast_nodes) == 2

        assert {
            test_input.ast_node,
            test_enum.ast_node,
            test_union.ast_node,
            test_interface.ast_node,
            test_type.ast_node,
            test_directive.ast_node,
            *query.extension_ast_nodes,
            *some_scalar.extension_ast_nodes,
            *some_enum.extension_ast_nodes,
            *some_union.extension_ast_nodes,
            *some_input.extension_ast_nodes,
            *some_interface.extension_ast_nodes,
        } == {*first_extension_ast.definitions, *second_extension_ast.definitions}

        new_field = query.fields["newField"]
        expect_ast_node(new_field, "newField(testArg: TestInput): TestEnum")
        expect_ast_node(new_field.args["testArg"], "testArg: TestInput")
        expect_ast_node(query.fields["oneMoreNewField"], "oneMoreNewField: TestUnion")

        expect_ast_node(some_enum.values["NEW_VALUE"], "NEW_VALUE")

        one_more_new_value = some_enum.values["ONE_MORE_NEW_VALUE"]
        expect_ast_node(one_more_new_value, "ONE_MORE_NEW_VALUE")
        expect_ast_node(some_input.fields["newField"], "newField: String")
        expect_ast_node(some_input.fields["oneMoreNewField"], "oneMoreNewField: String")
        expect_ast_node(some_interface.fields["newField"], "newField: String")
        expect_ast_node(
            some_interface.fields["oneMoreNewField"], "oneMoreNewField: String"
        )

        expect_ast_node(test_input.fields["testInputField"], "testInputField: TestEnum")

        expect_ast_node(test_enum.values["TEST_VALUE"], "TEST_VALUE")

        expect_ast_node(
            test_interface.fields["interfaceField"], "interfaceField: String"
        )
        expect_ast_node(test_type.fields["interfaceField"], "interfaceField: String")
        expect_ast_node(test_directive.args["arg"], "arg: Int")

    def builds_types_with_deprecated_fields_and_values():
        schema = GraphQLSchema()
        extend_ast = parse(
            """
            type SomeObject {
              deprecatedField: String @deprecated(reason: "not used anymore")
            }

            enum SomeEnum {
              DEPRECATED_VALUE @deprecated(reason: "do not use")
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        some_type = assert_object_type(extended_schema.get_type("SomeObject"))
        deprecated_field = some_type.fields["deprecatedField"]
        assert deprecated_field.deprecation_reason == "not used anymore"

        some_enum = assert_enum_type(extended_schema.get_type("SomeEnum"))
        deprecated_enum = some_enum.values["DEPRECATED_VALUE"]
        assert deprecated_enum.deprecation_reason == "do not use"

    def extends_objects_with_deprecated_fields():
        schema = build_schema("type SomeObject")
        extend_ast = parse(
            """
            extend type SomeObject {
              deprecatedField: String @deprecated(reason: "not used anymore")
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        some_type = assert_object_type(extended_schema.get_type("SomeObject"))
        deprecated_field = some_type.fields["deprecatedField"]
        assert deprecated_field.deprecation_reason == "not used anymore"

    def extend_enums_with_deprecated_values():
        schema = build_schema("enum SomeEnum")
        extend_ast = parse(
            """
            extend enum SomeEnum {
              DEPRECATED_VALUE @deprecated(reason: "do not use")
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        some_enum = assert_enum_type(extended_schema.get_type("SomeEnum"))
        deprecated_value = some_enum.values["DEPRECATED_VALUE"]
        assert deprecated_value.deprecation_reason == "do not use"

    def adds_new_unused_types():
        schema = build_schema(
            """
            type Query {
              dummy: String
            }
            """
        )
        extension_sdl = dedent(
            """
            type DummyUnionMember {
              someField: String
            }

            enum UnusedEnum {
              SOME_VALUE
            }

            input UnusedInput {
              someField: String
            }

            interface UnusedInterface {
              someField: String
            }

            type UnusedObject {
              someField: String
            }

            union UnusedUnion = DummyUnionMember
            """
        )
        extended_schema = extend_schema(schema, parse(extension_sdl))

        assert validate_schema(extended_schema) == []
        expect_schema_changes(schema, extended_schema, extension_sdl)

    def extends_objects_by_adding_new_fields_with_arguments():
        schema = build_schema(
            """
            type SomeObject

            type Query {
              someObject: SomeObject
            }
            """
        )
        extend_ast = parse(
            """
            input NewInputObj {
              field1: Int
              field2: [Float]
              field3: String!
            }

            extend type SomeObject {
              newField(arg1: String, arg2: NewInputObj!): String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
            type SomeObject {
              newField(arg1: String, arg2: NewInputObj!): String
            }

            input NewInputObj {
              field1: Int
              field2: [Float]
              field3: String!
            }
            """
            ),
        )

    def extends_objects_by_adding_new_fields_with_existing_types():
        schema = build_schema(
            """
            type Query {
              someObject: SomeObject
            }

            type SomeObject
            enum SomeEnum { VALUE }
            """
        )
        extend_ast = parse(
            """
            extend type SomeObject {
              newField(arg1: SomeEnum!): SomeEnum
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
            type SomeObject {
              newField(arg1: SomeEnum!): SomeEnum
            }
            """
            ),
        )

    def extends_objects_by_adding_implemented_interfaces():
        schema = build_schema(
            """
            type Query {
              someObject: SomeObject
            }

            type SomeObject {
              foo: String
            }

            interface SomeInterface {
              foo: String
            }
            """
        )
        extend_ast = parse(
            """
            extend type SomeObject implements SomeInterface
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
            type SomeObject implements SomeInterface {
              foo: String
            }
            """
            ),
        )

    def extends_objects_by_including_new_types():
        schema = build_schema(
            """
            type Query {
              someObject: SomeObject
            }

            type SomeObject {
              oldField: String
            }
            """
        )
        new_types_sdl = """
            enum NewEnum {
              VALUE
            }

            interface NewInterface {
              baz: String
            }

            type NewObject implements NewInterface {
              baz: String
            }

            scalar NewScalar

            union NewUnion = NewObject
            """
        extend_ast = parse(
            new_types_sdl
            + """
            extend type SomeObject {
              newObject: NewObject
              newInterface: NewInterface
              newUnion: NewUnion
              newScalar: NewScalar
              newEnum: NewEnum
              newTree: [SomeObject]!
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
            type SomeObject {
              oldField: String
              newObject: NewObject
              newInterface: NewInterface
              newUnion: NewUnion
              newScalar: NewScalar
              newEnum: NewEnum
              newTree: [SomeObject]!
            }\n"""
                + new_types_sdl
            ),
        )

    def extends_objects_by_adding_implemented_new_interfaces():
        schema = build_schema(
            """
            type Query {
              someObject: SomeObject
            }

            type SomeObject implements OldInterface {
              oldField: String
            }

            interface OldInterface {
              oldField: String
            }
            """
        )
        extend_ast = parse(
            """
            extend type SomeObject implements NewInterface {
              newField: String
            }

            interface NewInterface {
              newField: String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
            type SomeObject implements OldInterface & NewInterface {
              oldField: String
              newField: String
            }

            interface NewInterface {
              newField: String
            }
            """
            ),
        )

    def extends_different_types_multiple_times():
        schema = build_schema(
            """
            type Query {
              someScalar: SomeScalar
              someObject(someInput: SomeInput): SomeObject
              someInterface: SomeInterface
              someEnum: SomeEnum
              someUnion: SomeUnion
            }

            scalar SomeScalar

            type SomeObject implements SomeInterface {
              oldField: String
            }

            interface SomeInterface {
              oldField: String
            }

            enum SomeEnum {
              OLD_VALUE
            }

            union SomeUnion = SomeObject

            input SomeInput {
              oldField: String
            }
            """
        )
        new_types_sdl = dedent(
            """
            scalar NewScalar

            scalar AnotherNewScalar

            type NewObject {
              foo: String
            }

            type AnotherNewObject {
              foo: String
            }

            interface NewInterface {
              newField: String
            }

            interface AnotherNewInterface {
              anotherNewField: String
            }
            """
        )
        schema_with_new_types = extend_schema(schema, parse(new_types_sdl))
        expect_schema_changes(schema, schema_with_new_types, new_types_sdl)

        extend_ast = parse(
            """
            extend scalar SomeScalar @specifiedBy(url: "http://example.com/foo_spec")

            extend type SomeObject implements NewInterface {
                newField: String
            }

            extend type SomeObject implements AnotherNewInterface {
                anotherNewField: String
            }

            extend enum SomeEnum {
                NEW_VALUE
            }

            extend enum SomeEnum {
                ANOTHER_NEW_VALUE
            }

            extend union SomeUnion = NewObject

            extend union SomeUnion = AnotherNewObject

            extend input SomeInput {
                newField: String
            }

            extend input SomeInput {
                anotherNewField: String
            }
            """
        )
        extended_schema = extend_schema(schema_with_new_types, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                scalar SomeScalar @specifiedBy(url: "http://example.com/foo_spec")

                type SomeObject implements SomeInterface & NewInterface & AnotherNewInterface {
                  oldField: String
                  newField: String
                  anotherNewField: String
                }

                enum SomeEnum {
                  OLD_VALUE
                  NEW_VALUE
                  ANOTHER_NEW_VALUE
                }

                union SomeUnion = SomeObject | NewObject | AnotherNewObject

                input SomeInput {
                  oldField: String
                  newField: String
                  anotherNewField: String
                }

                """  # noqa: E501
            )
            + "\n\n"
            + new_types_sdl,
        )

    def extends_interfaces_by_adding_new_fields():
        schema = build_schema(
            """
            interface SomeInterface {
              oldField: String
            }

            interface AnotherInterface implements SomeInterface {
              oldField: String
            }

            type SomeObject implements SomeInterface & AnotherInterface {
              oldField: String
            }

            type Query {
              someInterface: SomeInterface
            }
            """
        )
        extend_ast = parse(
            """
            extend interface SomeInterface {
              newField: String
            }

            extend interface AnotherInterface {
              newField: String
            }

            extend type SomeObject {
              newField: String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                interface SomeInterface {
                  oldField: String
                  newField: String
                }

                interface AnotherInterface implements SomeInterface {
                  oldField: String
                  newField: String
                }

                type SomeObject implements SomeInterface & AnotherInterface {
                  oldField: String
                  newField: String
                }
                """
            ),
        )

    def extends_interfaces_by_adding_new_implemented_interfaces():
        schema = build_schema(
            """
            interface SomeInterface {
              oldField: String
            }

            interface AnotherInterface implements SomeInterface {
              oldField: String
            }

            type SomeObject implements SomeInterface & AnotherInterface {
              oldField: String
            }

            type Query {
              someInterface: SomeInterface
            }
            """
        )
        extend_ast = parse(
            """
            interface NewInterface {
              newField: String
            }

            extend interface AnotherInterface implements NewInterface {
              newField: String
            }

            extend type SomeObject implements NewInterface {
              newField: String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                interface AnotherInterface implements SomeInterface & NewInterface {
                  oldField: String
                  newField: String
                }

                type SomeObject implements SomeInterface & AnotherInterface & NewInterface {
                  oldField: String
                  newField: String
                }

                interface NewInterface {
                  newField: String
                }
                """  # noqa: E501
            ),
        )

    def allows_extension_of_interface_with_missing_object_fields():
        schema = build_schema(
            """
            type Query {
              someInterface: SomeInterface
            }

            type SomeObject implements SomeInterface {
              oldField: SomeInterface
            }

            interface SomeInterface {
              oldField: SomeInterface
            }
            """
        )
        extend_ast = parse(
            """
            extend interface SomeInterface {
              newField: String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema)
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                interface SomeInterface {
                  oldField: SomeInterface
                  newField: String
                }
                """
            ),
        )

    def extends_interfaces_multiple_times():
        schema = build_schema(
            """
            type Query {
              someInterface: SomeInterface
            }

            interface SomeInterface {
              some: SomeInterface
            }
            """
        )
        extend_ast = parse(
            """
            extend interface SomeInterface {
              newFieldA: Int
            }

            extend interface SomeInterface {
              newFieldB(test: Boolean): String
            }
            """
        )
        extended_schema = extend_schema(schema, extend_ast)

        assert validate_schema(extended_schema) == []
        expect_schema_changes(
            schema,
            extended_schema,
            dedent(
                """
                interface SomeInterface {
                  some: SomeInterface
                  newFieldA: Int
                  newFieldB(test: Boolean): String
                }
                """
            ),
        )

    def may_extend_mutations_and_subscriptions():
        mutation_schema = build_schema(
            """
            type Query {
              queryField: String
            }

            type Mutation {
              mutationField: String
            }

            type Subscription {
              subscriptionField: String
            }
            """
        )
        ast = parse(
            """
            extend type Query {
              newQueryField: Int
            }

            extend type Mutation {
              newMutationField: Int
            }

            extend type Subscription {
              newSubscriptionField: Int
            }
            """
        )
        original_print = print_schema(mutation_schema)
        extended_schema = extend_schema(mutation_schema, ast)
        assert extended_schema != mutation_schema
        assert print_schema(mutation_schema) == original_print
        assert print_schema(extended_schema) == dedent(
            """
            type Query {
              queryField: String
              newQueryField: Int
            }

            type Mutation {
              mutationField: String
              newMutationField: Int
            }

            type Subscription {
              subscriptionField: String
              newSubscriptionField: Int
            }
            """
        )

    def may_extend_directives_with_new_directive():
        schema = build_schema(
            """
            type Query {
              foo: String
            }
            """
        )
        extension_sdl = dedent(
            '''
            """New directive."""
            directive @new(enable: Boolean!, tag: String) repeatable on QUERY | FIELD
            '''
        )
        extended_schema = extend_schema(schema, parse(extension_sdl))

        assert validate_schema(extended_schema) == []
        expect_schema_changes(schema, extended_schema, extension_sdl)

    def rejects_invalid_sdl():
        schema = GraphQLSchema()
        extend_ast = parse("extend schema @unknown")

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value) == "Unknown directive '@unknown'."

    def allows_to_disable_sdl_validation():
        schema = GraphQLSchema()
        extend_ast = parse("extend schema @unknown")

        extend_schema(schema, extend_ast, assume_valid=True)
        extend_schema(schema, extend_ast, assume_valid_sdl=True)

    def throws_on_unknown_types():
        schema = GraphQLSchema()
        ast = parse(
            """
            type Query {
              unknown: UnknownType
            }
            """
        )
        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, ast, assume_valid_sdl=True)
        assert str(exc_info.value).endswith("Unknown type: 'UnknownType'.")

    def does_not_allow_replacing_a_default_directive():
        schema = GraphQLSchema()
        extend_ast = parse(
            """
            directive @include(if: Boolean!) on FIELD | FRAGMENT_SPREAD
            """
        )

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value).startswith(
            "Directive '@include' already exists in the schema. It cannot be redefined."
        )

    def does_not_allow_replacing_an_existing_enum_value():
        schema = build_schema(
            """
            enum SomeEnum {
              ONE
            }
            """
        )
        extend_ast = parse(
            """
            extend enum SomeEnum {
              ONE
            }
            """
        )

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value).startswith(
            "Enum value 'SomeEnum.ONE' already exists in the schema."
            " It cannot also be defined in this type extension."
        )

    def describe_can_add_additional_root_operation_types():
        def does_not_automatically_include_common_root_type_names():
            schema = GraphQLSchema()
            extended_schema = extend_schema(schema, parse("type Mutation"))

            assert extended_schema.get_type("Mutation")
            assert extended_schema.mutation_type is None

        def adds_schema_definition_missing_in_the_original_schema():
            schema = build_schema(
                """
                directive @foo on SCHEMA
                type Foo
                """
            )
            assert schema.query_type is None

            extension_sdl = dedent(
                """
                schema @foo {
                  query: Foo
                }
                """
            )
            extended_schema = extend_schema(schema, parse(extension_sdl))

            query_type = assert_object_type(extended_schema.query_type)
            assert query_type.name == "Foo"
            expect_ast_node(extended_schema, extension_sdl)

        def adds_new_root_types_via_schema_extension():
            schema = build_schema(
                """
                type Query
                type MutationRoot
                """
            )
            extension_sdl = dedent(
                """
                extend schema {
                  mutation: MutationRoot
                }
                """
            )
            extended_schema = extend_schema(schema, parse(extension_sdl))

            mutation_type = assert_object_type(extended_schema.mutation_type)
            assert mutation_type.name == "MutationRoot"
            expect_extension_ast_nodes(extended_schema, extension_sdl)

        def adds_directive_via_schema_extension():
            schema = build_schema(
                """
                type Query

                directive @foo on SCHEMA
                """
            )
            extension_sdl = dedent(
                """
                extend schema @foo
                """
            )
            extended_schema = extend_schema(schema, parse(extension_sdl))

            expect_extension_ast_nodes(extended_schema, extension_sdl)

        def adds_multiple_new_root_types_via_schema_extension():
            schema = build_schema("type Query")
            extend_ast = parse(
                """
                extend schema {
                  mutation: Mutation
                  subscription: Subscription
                }

                type Mutation
                type Subscription
                """
            )
            extended_schema = extend_schema(schema, extend_ast)

            mutation_type = assert_object_type(extended_schema.mutation_type)
            assert mutation_type.name == "Mutation"

            subscription_type = assert_object_type(extended_schema.subscription_type)
            assert subscription_type.name == "Subscription"

        def applies_multiple_schema_extensions():
            schema = build_schema("type Query")
            extend_ast = parse(
                """
                extend schema {
                  mutation: Mutation
                }
                type Mutation

                extend schema {
                  subscription: Subscription
                }
                type Subscription
                """
            )
            extended_schema = extend_schema(schema, extend_ast)

            mutation_type = assert_object_type(extended_schema.mutation_type)
            assert mutation_type.name == "Mutation"

            subscription_type = assert_object_type(extended_schema.subscription_type)
            assert subscription_type.name == "Subscription"

        def schema_extension_ast_are_available_from_schema_object():
            schema = build_schema(
                """
                type Query

                directive @foo on SCHEMA
                """
            )
            extend_ast = parse(
                """
                extend schema {
                  mutation: Mutation
                }
                type Mutation

                extend schema {
                  subscription: Subscription
                }
                type Subscription
                """
            )
            extended_schema = extend_schema(schema, extend_ast)

            second_extend_ast = parse("extend schema @foo")
            extended_twice_schema = extend_schema(extended_schema, second_extend_ast)

            expect_extension_ast_nodes(
                extended_twice_schema,
                dedent(
                    """
                    extend schema {
                      mutation: Mutation
                    }

                    extend schema {
                      subscription: Subscription
                    }

                    extend schema @foo
                    """
                ),
            )

        def extend_directive_to_make_it_deprecated():
            schema = build_schema("directive @isDeprecated on FIELD_DEFINITION")
            extend_ast = parse(
                """
extend directive @isDeprecated @deprecated(reason: "use another directive")
""",
                experimental_directives_on_directive_definitions=True,
            )
            extended_schema = extend_schema(schema, extend_ast)

            is_deprecated_directive = assert_directive(
                extended_schema.get_directive("isDeprecated")
            )
            assert is_deprecated_directive.deprecation_reason == "use another directive"

        def preserves_deprecated_directives_when_extending_other_types():
            schema = build_ast_schema(
                parse(
                    """
type Query {
  foo: String
}

directive @isDeprecated @deprecated(reason: "use another directive") on FIELD_DEFINITION
""",
                    experimental_directives_on_directive_definitions=True,
                )
            )
            extend_ast = parse(
                dedent("""
                extend type Query {
                  bar: Int
                }
                """)
            )
            extended_schema = extend_schema(schema, extend_ast)

            is_deprecated_directive = assert_directive(
                extended_schema.get_directive("isDeprecated")
            )
            assert is_deprecated_directive.deprecation_reason == "use another directive"

        def applies_directive_extensions_defined_in_the_same_document():
            schema = build_ast_schema(
                parse(
                    dedent("""
                    directive @onDirective on DIRECTIVE_DEFINITION
                    directive @someDirective on FIELD_DEFINITION

                    extend directive @someDirective @onDirective
                    """),
                    experimental_directives_on_directive_definitions=True,
                )
            )

            some_directive = assert_directive(schema.get_directive("someDirective"))
            expect_extension_ast_nodes(
                some_directive, "extend directive @someDirective @onDirective"
            )

        def builds_directives_with_deprecation_from_extensions():
            schema = GraphQLSchema()
            extension_sdl = dedent("""
directive @isDeprecated on FIELD_DEFINITION

extend directive @isDeprecated @deprecated(reason: "use another directive")
""")
            extended_schema = extend_schema(
                schema,
                parse(
                    extension_sdl,
                    experimental_directives_on_directive_definitions=True,
                ),
            )

            is_deprecated_directive = assert_directive(
                extended_schema.get_directive("isDeprecated")
            )
            assert is_deprecated_directive.deprecation_reason == "use another directive"
            expect_ast_node(
                is_deprecated_directive, "directive @isDeprecated on FIELD_DEFINITION"
            )
            expect_extension_ast_nodes(
                is_deprecated_directive,
                "extend directive @isDeprecated"
                ' @deprecated(reason: "use another directive")',
            )

        def applies_multiple_directive_extensions_defined_in_the_same_document():
            schema = build_ast_schema(
                parse(
                    dedent("""
                    directive @onDirective on DIRECTIVE_DEFINITION
                    directive @otherDirective on DIRECTIVE_DEFINITION
                    directive @someDirective on FIELD_DEFINITION

                    extend directive @someDirective @onDirective
                    extend directive @someDirective @otherDirective
                    """),
                    experimental_directives_on_directive_definitions=True,
                )
            )

            some_directive = assert_directive(schema.get_directive("someDirective"))
            expect_extension_ast_nodes(
                some_directive,
                dedent("""
                    extend directive @someDirective @onDirective

                    extend directive @someDirective @otherDirective
                    """),
            )

        def extend_directive_without_adding_new_directives_is_an_error():
            with pytest.raises(GraphQLSyntaxError) as exc_info:
                parse(
                    "extend directive @isDeprecated",
                    experimental_directives_on_directive_definitions=True,
                )
            assert str(exc_info.value).startswith("Syntax Error: Unexpected <EOF>.")


def describe_extend_schema_from_multiple_sources():
    def produces_equivalent_schemas_via_sdl_and_ast_paths():
        schema = build_schema("type Query { a: String }")
        printed_before = print_schema(schema)

        sdl_extension = parse("extend type Query { added: String }")
        ast_extension = DocumentNode(
            definitions=(
                ObjectTypeExtensionNode(
                    name=NameNode(value="Query"),
                    fields=(
                        FieldDefinitionNode(
                            name=NameNode(value="added"),
                            type=NamedTypeNode(name=NameNode(value="String")),
                        ),
                    ),
                ),
            )
        )

        extended_via_sdl = extend_schema(schema, sdl_extension)
        extended_via_ast = extend_schema(schema, ast_extension)

        assert print_schema(extended_via_sdl) == print_schema(extended_via_ast)
        assert "added: String" in print_schema(extended_via_ast)

        # neither path polluted the input schema
        assert print_schema(schema) == printed_before

    def extending_the_same_ast_multiple_times_is_reproducible():
        schema = build_schema("type Query { a: String }")
        extend_ast = parse("extend type Query { b: Int }")
        printed_ast = print_ast(extend_ast)

        first = extend_schema(schema, extend_ast)
        second = extend_schema(schema, extend_ast)

        assert first is not second
        assert print_schema(first) == print_schema(second)
        assert introspection_from_schema(first) == introspection_from_schema(second)

        # the reused AST was not polluted by the extensions
        assert print_ast(extend_ast) == printed_ast

    def produces_equivalent_schemas_regardless_of_extension_order():
        schema = build_schema("type Query { a: String }")
        plugin_a = parse(
            dedent(
                """
                directive @tag(name: String!) repeatable on OBJECT

                extend type Query @tag(name: "a") { alpha: String }
                """
            )
        )
        plugin_b = parse(
            dedent(
                """
                extend type Query { beta: Int }

                extend schema { mutation: Mutation }

                type Mutation { m: String }
                """
            )
        )

        extended_ab = extend_schema(extend_schema(schema, plugin_a), plugin_b)
        extended_ba = extend_schema(extend_schema(schema, plugin_b), plugin_a)

        sorted_ab = print_schema(lexicographic_sort_schema(extended_ab))
        sorted_ba = print_schema(lexicographic_sort_schema(extended_ba))
        assert sorted_ab == sorted_ba

        combined_ab = extend_schema(schema, concat_ast([plugin_a, plugin_b]))
        combined_ba = extend_schema(schema, concat_ast([plugin_b, plugin_a]))
        assert print_schema(lexicographic_sort_schema(combined_ab)) == sorted_ab
        assert print_schema(lexicographic_sort_schema(combined_ba)) == sorted_ab

        # both orders accumulated the same fields and schema extensions
        assert list(extended_ab.query_type.fields) == ["a", "alpha", "beta"]
        assert list(extended_ba.query_type.fields) == ["a", "beta", "alpha"]
        assert extended_ab.mutation_type.name == "Mutation"
        assert extended_ba.mutation_type.name == "Mutation"
        assert len(extended_ab.extension_ast_nodes) == 1
        assert len(extended_ba.extension_ast_nodes) == 1

    def rejects_unknown_directives_and_keeps_the_input_schema_unchanged():
        schema = build_schema("type Query { a: String }")
        printed_before = print_schema(schema)
        introspection_before = introspection_from_schema(schema)

        extend_ast = parse("extend type Query @unknown { b: Int }")

        [error] = validate_sdl(extend_ast, schema)
        assert error.message == "Unknown directive '@unknown'."
        assert error.locations == [SourceLocation(1, 19)]

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value) == "Unknown directive '@unknown'."

        # the failed extension did not pollute the input schema
        assert print_schema(schema) == printed_before
        assert introspection_from_schema(schema) == introspection_before

        # and the schema can still be extended afterwards
        extended = extend_schema(schema, parse("extend type Query { b: Int }"))
        assert "b: Int" in print_schema(extended)

    def rejects_duplicate_argument_defaults_and_keeps_the_input_schema_unchanged():
        schema = build_schema("type Query { f(x: Int = 1): String }")
        printed_before = print_schema(schema)

        extend_ast = parse("extend type Query { g(x: Int = 1, x: Int = 2): String }")

        [error] = validate_sdl(extend_ast, schema)
        assert error.message == "Argument 'Query.g(x:)' can only be defined once."
        assert error.locations == [SourceLocation(1, 23), SourceLocation(1, 35)]

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value) == (
            "Argument 'Query.g(x:)' can only be defined once."
        )

        # the failed extension did not pollute the input schema
        assert print_schema(schema) == printed_before

        # including the default value of the existing argument
        extended = extend_schema(schema, parse("extend type Query { g: Int }"))
        default = extended.query_type.fields["f"].args["x"].default
        assert print_ast(default.literal) == "1"

    def does_not_allow_an_extension_to_override_an_argument_default():
        schema = build_schema("type Query { f(x: Int = 1): String }")
        printed_before = print_schema(schema)

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, parse("extend type Query { f(x: Int = 2): String }"))
        assert str(exc_info.value).startswith(
            "Field 'Query.f' already exists in the schema."
        )

        # the later definition did not silently overwrite the default
        assert print_schema(schema) == printed_before

    def rejects_conflicting_type_definitions_from_plugins_in_any_order():
        schema = build_schema("type Query { a: String }")
        printed_before = print_schema(schema)
        plugin_one = parse("type Conflict { x: String }")
        plugin_two = parse("type Conflict { y: Int }")

        for document in (
            concat_ast([plugin_one, plugin_two]),
            concat_ast([plugin_two, plugin_one]),
        ):
            [error] = validate_sdl(document, schema)
            assert error.message == "There can be only one type named 'Conflict'."
            assert error.locations == [SourceLocation(1, 6), SourceLocation(1, 6)]

            with pytest.raises(TypeError) as exc_info:
                extend_schema(schema, document)
            assert str(exc_info.value) == (
                "There can be only one type named 'Conflict'."
            )

        # sequential application of the plugins fails as well
        extended = extend_schema(schema, plugin_one)
        printed_extended = print_schema(extended)
        with pytest.raises(TypeError) as exc_info:
            extend_schema(extended, plugin_two)
        assert str(exc_info.value).startswith(
            "Type 'Conflict' already exists in the schema."
        )

        # neither failure polluted the input schemas
        assert print_schema(schema) == printed_before
        assert print_schema(extended) == printed_extended

    def rejects_redefinition_of_an_existing_directive():
        schema = build_schema(
            "directive @tag(name: String!) repeatable on OBJECT\n"
            "type Query { a: String }"
        )
        printed_before = print_schema(schema)

        extend_ast = parse("directive @tag(name: Int!) on FIELD_DEFINITION")

        [error] = validate_sdl(extend_ast, schema)
        assert error.message == (
            "Directive '@tag' already exists in the schema. It cannot be redefined."
        )
        assert error.locations == [SourceLocation(1, 12)]

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value) == (
            "Directive '@tag' already exists in the schema. It cannot be redefined."
        )

        # the later definition did not silently overwrite the directive
        assert print_schema(schema) == printed_before
        tag = schema.get_directive("tag")
        assert tag.is_repeatable is True
        assert tag.locations == (DirectiveLocation.OBJECT,)

    def builds_the_final_definition_of_a_repeatable_directive():
        schema = build_schema("type Query { a: String }")
        extend_ast = parse(
            dedent(
                '''
                """Tags a type."""
                directive @tag(name: String!, priority: Int = 0) repeatable
                  on OBJECT | INTERFACE

                extend type Query @tag(name: "first")
                '''
            )
        )
        extended = extend_schema(schema, extend_ast)
        extended = extend_schema(
            extended, parse('extend type Query @tag(name: "second")')
        )

        tag = extended.get_directive("tag")
        assert tag.name == "tag"
        assert tag.description == "Tags a type."
        assert tag.is_repeatable is True
        assert tag.locations == (DirectiveLocation.OBJECT, DirectiveLocation.INTERFACE)
        name_arg = tag.args["name"]
        assert is_non_null_type(name_arg.type)
        assert name_arg.type.of_type is GraphQLString
        assert name_arg.default is None
        priority_arg = tag.args["priority"]
        assert priority_arg.type == GraphQLInt
        assert print_ast(priority_arg.default.literal) == "0"

        # both applications were accumulated instead of overwritten
        applied = [
            print_ast(directive)
            for node in (
                extended.query_type.ast_node,
                *extended.query_type.extension_ast_nodes,
            )
            for directive in node.directives or ()
        ]
        assert applied == ['@tag(name: "first")', '@tag(name: "second")']

        # introspection exposes the final directive definition
        [introspected] = [
            directive
            for directive in introspection_from_schema(extended)["__schema"][
                "directives"
            ]
            if directive["name"] == "tag"
        ]
        assert introspected["isRepeatable"] is True
        assert introspected["locations"] == ["OBJECT", "INTERFACE"]
        assert [arg["name"] for arg in introspected["args"]] == ["name", "priority"]

    def rejects_a_non_repeatable_directive_applied_twice():
        schema = build_schema("directive @once on OBJECT\ntype Query { a: String }")
        printed_before = print_schema(schema)

        extend_ast = parse("extend type Query @once @once")

        [error] = validate_sdl(extend_ast, schema)
        assert error.message == (
            "The directive '@once' can only be used once at this location."
        )
        assert error.locations == [SourceLocation(1, 19), SourceLocation(1, 25)]

        with pytest.raises(TypeError) as exc_info:
            extend_schema(schema, extend_ast)
        assert str(exc_info.value) == (
            "The directive '@once' can only be used once at this location."
        )

        assert print_schema(schema) == printed_before

    def keeps_description_and_deprecation_reproducible_in_introspection():
        schema = build_schema(
            dedent(
                '''
                """Schema description."""
                schema { query: Query }

                type Query {
                  old: String @deprecated(reason: "Use new")
                  new: String
                }
                '''
            )
        )
        extend_ast = parse("extend type Query { extra: Int }")

        first = extend_schema(schema, extend_ast)
        second = extend_schema(schema, extend_ast)

        assert first.description == "Schema description."
        assert second.description == "Schema description."

        introspection = introspection_from_schema(first)
        assert introspection == introspection_from_schema(second)
        assert introspection["__schema"]["description"] == "Schema description."

        [query_type] = [
            type_
            for type_ in introspection["__schema"]["types"]
            if type_["name"] == "Query"
        ]
        [old_field] = [
            field for field in query_type["fields"] if field["name"] == "old"
        ]
        assert old_field["isDeprecated"] is True
        assert old_field["deprecationReason"] == "Use new"

    def supports_print_and_rebuild_round_trips():
        schema = build_schema("type Query { a: String }")
        extended = extend_schema(
            schema,
            parse(
                dedent(
                    """
                    directive @tag(name: String!) repeatable on OBJECT

                    extend type Query @tag(name: "one") { b(x: Int = 3): Int }
                    """
                )
            ),
        )

        printed = print_schema(extended)
        rebuilt = build_schema(printed)
        assert print_schema(rebuilt) == printed

        # extending the rebuilt schema yields the same result
        # as extending the original extended schema
        further = parse('extend type Query @tag(name: "two")')
        assert print_schema(extend_schema(rebuilt, further)) == print_schema(
            extend_schema(extended, further)
        )

    def supports_concurrent_read_only_use_while_extending():
        schema = build_schema(
            dedent(
                """
                type Query { a: String }

                type Mutation { m: String }
                """
            )
        )
        extend_ast = parse("extend type Query { b: Int }")

        expected_print = print_schema(schema)
        expected_introspection = introspection_from_schema(schema)

        def read_schema():
            for _ in range(10):
                assert print_schema(schema) == expected_print
                assert introspection_from_schema(schema) == expected_introspection
                assert validate_schema(schema) == []

        def extend_schema_repeatedly():
            for _ in range(10):
                extended = extend_schema(schema, extend_ast)
                assert "b: Int" in print_schema(extended)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(read_schema),
                executor.submit(read_schema),
                executor.submit(read_schema),
                executor.submit(extend_schema_repeatedly),
            ]
            for future in futures:
                future.result()

        # the concurrent extensions did not pollute the input schema
        assert print_schema(schema) == expected_print
