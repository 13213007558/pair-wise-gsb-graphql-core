from graphql import parse, validate
from graphql.validation import OverlappingFieldsCanBeMergedRule

from .diamond_fragments import build_diamond_document, schema

valid_document = parse(build_diamond_document(4, 3))
conflicting_document = parse(build_diamond_document(4, 3, conflicting=True))


def test_validate_diamond_fragment_graph(benchmark):
    errors = benchmark(
        lambda: validate(schema, valid_document, [OverlappingFieldsCanBeMergedRule])
    )
    assert errors == []


def test_validate_conflicting_diamond_fragment_graph(benchmark):
    errors = benchmark(
        lambda: validate(
            schema, conflicting_document, [OverlappingFieldsCanBeMergedRule]
        )
    )
    assert len(errors) == 24
