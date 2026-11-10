"""Diamond fragment graphs for measuring OverlappingFieldsCanBeMerged.

Public queries often spread shared fragments, so the same fragment pairs and
field pairs are reachable through many paths. This module builds small but
scalable "diamond" fragment graphs (layered fragments that all spread the next
layer and share one common sink fragment) and instruments the rule to count
the actual comparison work:

- field pair attempts (``find_conflict`` calls)
- source argument comparisons (``compare_arguments`` calls, uncached)
- fragment pair attempts (``collect_conflicts_between_fragments`` calls)
- fields/fragment attempts (``collect_conflicts_between_fields_and_fragment``)
- collection pair comparisons (``collect_conflicts_between`` calls, uncached)
- sub selection set recursions and collection computations
- pair cache queries and hits (``OrderedPairSet.has``)

Run ``python -m tests.benchmarks.diamond_fragments`` for a table of counts and
timings, including a run with the pair caches disabled to show the duplicate
work they eliminate.
"""

from __future__ import annotations

import cProfile
import pstats
import time
from typing import Any, Callable

from graphql import build_schema, parse, validate
from graphql.validation.rules import overlapping_fields_can_be_merged as ofcm
from graphql.validation.rules.overlapping_fields_can_be_merged import (
    OverlappingFieldsCanBeMergedRule,
)

SCHEMA_SDL = """
interface Iface { val: Int, other: Int }
interface OtherIface { val: String }
type ObjA implements Iface { val: Int, other: Int, nested: Iface }
type ObjB implements OtherIface { val: String, other: Int, nested: Iface }
union Choice = ObjA | ObjB
type Item {
  leaf: Int
  other: Int
  leafArg(x: Int): Int
  sub: Item
}
type Query {
  root: Item
  choice: Choice
  iface: Iface
}
"""

schema = build_schema(SCHEMA_SDL)


def build_diamond_document(depth: int, width: int, conflicting: bool = False) -> str:
    """Build a diamond fragment graph with ``depth`` layers of ``width`` fragments.

    Every fragment spreads all fragments of the next layer; the last layer
    spreads one shared sink fragment, so every pair of fragments is reachable
    through a growing number of paths as the graph scales.

    The valid variant exercises the mutually-exclusive-object-types rule with
    aliased fields of different types under two object types. The conflicting
    variant (needs depth >= 3 and width >= 2) adds an alias conflict, an
    argument conflict and a return type conflict under non-exclusive parents.
    """
    if conflicting and (depth < 3 or width < 2):
        msg = "conflicting diamond graphs need depth >= 3 and width >= 2"
        raise ValueError(msg)
    root_spreads = " ".join(f"...F_0_{i}" for i in range(width))
    if conflicting:
        # Non-exclusive interface parents: 'val' is Int vs String.
        choice_selection = "choice { ...RetA ...RetB }"
    else:
        # Mutually exclusive object types: aliased fields may diverge.
        choice_selection = "choice { ... on ObjA { v: val } ... on ObjB { v: other } }"
    parts = [f"query Q {{ root {{ {root_spreads} }} {choice_selection} }}"]
    for layer in range(depth):
        next_spreads = (
            " ".join(f"...F_{layer + 1}_{j}" for j in range(width))
            if layer + 1 < depth
            else "...Sink"
        )
        for i in range(width):
            fields = ["leaf", "leafArg(x: 0)", f"own_{layer}_{i}: leaf", "sub { leaf }"]
            if conflicting:
                if layer == 1 and i == 0:
                    fields.append("clash: leaf")
                if layer == 1 and i == 1:
                    fields.append("clash: other")
                if layer == 2 and i == 0:
                    fields[1] = "leafArg(x: 999)"
            joined_fields = " ".join(fields)
            parts.append(
                f"fragment F_{layer}_{i} on Item {{ {joined_fields} {next_spreads} }}"
            )
    parts.append("fragment Sink on Item { leaf leafArg(x: 0) }")
    if conflicting:
        parts.append("fragment RetA on Iface { val }")
        parts.append("fragment RetB on OtherIface { val }")
    return "\n".join(parts)


def build_exclusivity_ordering_document() -> str:
    """Compare one fragment pair in a lenient context before a strict one.

    The spreads of ``WeakX`` and ``WeakY`` meet first under the mutually
    exclusive object types ``ObjA``/``ObjB`` (via the ``box`` field pair),
    where their aliased fields may diverge. The same pair meets again under
    ``iface`` in a non-exclusive context, where the conflict must still be
    reported: the earlier lenient comparison must not skip the strict one.
    """
    return """
    query {
      choice {
        ... on ObjA { box: nested { ...WeakX } }
        ... on ObjB { box: nested { ...WeakY } }
      }
      iface { ...WeakX ...WeakY }
    }
    fragment WeakX on Iface { k: val }
    fragment WeakY on Iface { k: other }
    """


COUNTED_FUNCTIONS = (
    "find_conflict",
    "compare_arguments",
    "collect_conflicts_between_fragments",
    "collect_conflicts_between_fields_and_fragment",
    "collect_conflicts_between",
    "find_conflicts_between_sub_selection_sets",
    "collect_fields_impl",
    "do_types_conflict",
)


class ComparisonMeter:
    """Count calls to the comparison functions of the rule while validating."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {
            name: 0 for name in COUNTED_FUNCTIONS
        } | {"pair_cache_queries": 0, "pair_cache_hits": 0}
        self._originals: dict[str, Any] = {}

    def __enter__(self) -> ComparisonMeter:
        counts = self.counts
        for name in COUNTED_FUNCTIONS:
            original = getattr(ofcm, name)
            self._originals[name] = original

            def make_wrapper(func: Callable, key: str) -> Callable:
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    counts[key] += 1
                    return func(*args, **kwargs)

                return wrapper

            setattr(ofcm, name, make_wrapper(original, name))
        original_has = ofcm.OrderedPairSet.has
        self._originals["OrderedPairSet.has"] = original_has

        def counting_has(self: Any, a: Any, b: Any, weakly_present: bool) -> bool:
            hit = original_has(self, a, b, weakly_present)
            counts["pair_cache_queries"] += 1
            counts["pair_cache_hits"] += 1 if hit else 0
            return hit

        ofcm.OrderedPairSet.has = counting_has  # type: ignore[method-assign]
        return self

    def __exit__(self, *_args: Any) -> None:
        for name, original in self._originals.items():
            if name == "OrderedPairSet.has":
                ofcm.OrderedPairSet.has = original  # type: ignore[method-assign]
            else:
                setattr(ofcm, name, original)
        self._originals.clear()


def validate_diamond(
    depth: int, width: int, conflicting: bool = False
) -> list[Any]:
    document = parse(build_diamond_document(depth, width, conflicting))
    return validate(schema, document, [OverlappingFieldsCanBeMergedRule])


def measure(
    depth: int, width: int, conflicting: bool = False, repeats: int = 5
) -> tuple[dict[str, int], float, int]:
    """Return (comparison counts, best time in ms, number of errors)."""
    with ComparisonMeter() as meter:
        errors = validate_diamond(depth, width, conflicting)
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        validate_diamond(depth, width, conflicting)
        best = min(best, time.perf_counter() - start)
    return meter.counts, best * 1000, len(errors)


def measure_without_pair_cache(
    depth: int, width: int
) -> tuple[dict[str, int], float, int]:
    """Measure with the fragment/field-map pair caches disabled.

    Shows the duplicate work the caches eliminate. Only meaningful on small
    acyclic graphs; the complexity budget is raised so the run can finish.
    """
    original_has = ofcm.OrderedPairSet.has
    original_budget = ofcm.MAX_COMPLEXITY_BUDGET
    ofcm.OrderedPairSet.has = lambda *_args: False  # type: ignore[method-assign]
    ofcm.MAX_COMPLEXITY_BUDGET = 2**63 - 1
    try:
        with ComparisonMeter() as meter:
            start = time.perf_counter()
            errors = validate_diamond(depth, width)
            elapsed = time.perf_counter() - start
    finally:
        ofcm.OrderedPairSet.has = original_has  # type: ignore[method-assign]
        ofcm.MAX_COMPLEXITY_BUDGET = original_budget
    return meter.counts, elapsed * 1000, len(errors)


def profile(depth: int, width: int, conflicting: bool = False) -> None:
    profiler = cProfile.Profile()
    profiler.enable()
    validate_diamond(depth, width, conflicting)
    profiler.disable()
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(25)


def main() -> None:
    header = (
        f"{'graph':>18} {'errors':>6} {'time_ms':>9} {'field_pairs':>11}"
        f" {'arg_cmps':>8} {'frag_pairs':>10} {'fld_frag':>8} {'coll_pairs':>10}"
        f" {'sub_sets':>8} {'collects':>8} {'cache_q':>8} {'cache_hit':>9}"
    )
    print("== valid diamond graphs (pair caches enabled) ==")
    print(header)
    for depth, width in [(2, 2), (3, 2), (3, 3), (4, 3), (5, 4), (6, 4)]:
        counts, elapsed, num_errors = measure(depth, width)
        print(
            f"{'d%dw%d' % (depth, width):>18} {num_errors:>6} {elapsed:>9.2f}"
            f" {counts['find_conflict']:>11} {counts['compare_arguments']:>8}"
            f" {counts['collect_conflicts_between_fragments']:>10}"
            f" {counts['collect_conflicts_between_fields_and_fragment']:>8}"
            f" {counts['collect_conflicts_between']:>10}"
            f" {counts['find_conflicts_between_sub_selection_sets']:>8}"
            f" {counts['collect_fields_impl']:>8}"
            f" {counts['pair_cache_queries']:>8} {counts['pair_cache_hits']:>9}"
        )
    print()
    print("== conflicting diamond graphs (pair caches enabled) ==")
    print(header)
    for depth, width in [(3, 2), (4, 3), (5, 4)]:
        counts, elapsed, num_errors = measure(depth, width, conflicting=True)
        print(
            f"{'d%dw%d-conflict' % (depth, width):>18} {num_errors:>6}"
            f" {elapsed:>9.2f} {counts['find_conflict']:>11}"
            f" {counts['compare_arguments']:>8}"
            f" {counts['collect_conflicts_between_fragments']:>10}"
            f" {counts['collect_conflicts_between_fields_and_fragment']:>8}"
            f" {counts['collect_conflicts_between']:>10}"
            f" {counts['find_conflicts_between_sub_selection_sets']:>8}"
            f" {counts['collect_fields_impl']:>8}"
            f" {counts['pair_cache_queries']:>8} {counts['pair_cache_hits']:>9}"
        )
    print()
    print("== valid diamond graphs, pair caches disabled (expected duplication) ==")
    print(header)
    for depth, width in [(2, 2), (3, 2), (4, 2)]:
        counts, elapsed, num_errors = measure_without_pair_cache(depth, width)
        print(
            f"{'d%dw%d-nocache' % (depth, width):>18} {num_errors:>6}"
            f" {elapsed:>9.2f} {counts['find_conflict']:>11}"
            f" {counts['compare_arguments']:>8}"
            f" {counts['collect_conflicts_between_fragments']:>10}"
            f" {counts['collect_conflicts_between_fields_and_fragment']:>8}"
            f" {counts['collect_conflicts_between']:>10}"
            f" {counts['find_conflicts_between_sub_selection_sets']:>8}"
            f" {counts['collect_fields_impl']:>8}"
            f" {counts['pair_cache_queries']:>8} {counts['pair_cache_hits']:>9}"
        )


if __name__ == "__main__":
    main()
