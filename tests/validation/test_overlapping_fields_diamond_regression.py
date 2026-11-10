"""Regression anchors for shared-fragment (diamond) graphs.

Pins the exact error sets, locations and stable order produced by
OverlappingFieldsCanBeMergedRule on the scalable diamond fragment graphs of
tests/benchmarks/diamond_fragments.py, so that optimizations of the rule keep
observable behavior identical on both valid and conflicting graphs.
"""

from functools import partial

from graphql import parse, validate
from graphql.validation import OverlappingFieldsCanBeMergedRule

from ..benchmarks.diamond_fragments import (
    build_diamond_document,
    build_exclusivity_ordering_document,
    schema,
)
from .harness import assert_validation_errors

assert_errors = partial(
    assert_validation_errors, OverlappingFieldsCanBeMergedRule, schema=schema
)

ARG_CONFLICT = (
    "Fields 'leafArg' conflict because they have differing arguments."
    " Use different aliases on the fields to fetch both if this was intentional."
)


def describe_diamond_fragment_graphs():
    def valid_graphs_produce_no_errors():
        for depth, width in [(2, 2), (3, 2), (4, 3)]:
            assert_errors(build_diamond_document(depth, width), [])

    def valid_graphs_are_valid_under_all_rules():
        document = parse(build_diamond_document(4, 3))
        assert validate(schema, document) == []

    def conflicting_graph_produces_stable_errors():
        # The diamond graph with an alias conflict (clash: leaf/other),
        # an argument conflict (leafArg(x: 999)) and a return type conflict
        # (val: Int/String under non-exclusive interface parents).
        assert_errors(
            build_diamond_document(3, 2, conflicting=True),
            [
                {"message": ARG_CONFLICT, "locations": [(2, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(6, 31), (8, 30)]},
                {"message": ARG_CONFLICT, "locations": [(4, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(7, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(5, 31), (6, 31)]},
                {
                    "message": "Fields 'clash' conflict because"
                    " 'other' and 'leaf' are different fields."
                    " Use different aliases on the fields"
                    " to fetch both if this was intentional.",
                    "locations": [(5, 72), (4, 72)],
                },
                {"message": ARG_CONFLICT, "locations": [(6, 31), (3, 31)]},
                {
                    "message": "Fields 'val' conflict because"
                    " they return conflicting types 'Int' and 'String'."
                    " Use different aliases on the fields"
                    " to fetch both if this was intentional.",
                    "locations": [(9, 26), (10, 31)],
                },
                {"message": ARG_CONFLICT, "locations": [(2, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(3, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(4, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(5, 31), (6, 31)]},
                {"message": ARG_CONFLICT, "locations": [(6, 31), (8, 30)]},
            ],
        )

    def conflicting_graph_scales_with_identical_error_kinds():
        # Larger conflicting graphs keep producing errors; no deep branches
        # are skipped as the graph grows.
        for depth, width, num_errors in [(3, 2, 13), (4, 3, 24), (5, 4, 39)]:
            document = parse(build_diamond_document(depth, width, conflicting=True))
            result = validate(schema, document, [OverlappingFieldsCanBeMergedRule])
            assert len(result) == num_errors

    def lenient_comparison_does_not_skip_stricter_one():
        # The fragment pair WeakX/WeakY is first compared under the mutually
        # exclusive object types ObjA/ObjB (where the aliased fields may
        # diverge) and must still be checked when it meets again under the
        # non-exclusive parent Iface.
        assert_errors(
            build_exclusivity_ordering_document(),
            [
                {
                    "message": "Fields 'k' conflict because"
                    " 'val' and 'other' are different fields."
                    " Use different aliases on the fields"
                    " to fetch both if this was intentional.",
                    "locations": [(9, 31), (10, 31)],
                }
            ],
        )
