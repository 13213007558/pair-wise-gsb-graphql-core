"""Max introspection depth rule"""

from typing import Any

from ...error import GraphQLError
from ...language import (
    SKIP,
    BooleanValueNode,
    FieldNode,
    FragmentSpreadNode,
    Node,
    VisitorAction,
)
from . import ASTValidationRule, ValidationContext

__all__ = ["MaxIntrospectionDepthRule"]

MAX_LIST_DEPTH = 3
"""Maximum number of nested introspection list fields that is allowed."""

INTROSPECTION_LIST_FIELDS = ("fields", "interfaces", "possibleTypes", "inputFields")
"""Introspection fields that are counted when determining the list depth."""

INTROSPECTION_ROOT_FIELDS = ("__schema", "__type")
"""Root fields that start an introspection query."""


class MaxIntrospectionDepthRule(ASTValidationRule):
    """Checks maximum introspection depth.

    Since the final values of variables are not known during validation,
    the following static analysis policies are applied:

    * Depth boundary: the depth is the number of nested introspection list
      fields (see `INTROSPECTION_LIST_FIELDS`) below a `__schema` or `__type`
      root field. Reaching `MAX_LIST_DEPTH` nested list fields is reported
      once at the root field. Errors from different root fields are never
      merged, even if they select the same type.
    * Aliases are ignored: the actual field name determines whether a field
      counts towards the depth.
    * Multiple operations: each operation in the document is checked
      independently of the others.
    * `@skip`/`@include`: only literal Boolean `if:` arguments are
      statically decidable. Selections whose inclusion depends on variable
      values (including variables with default values, which can still be
      overridden at runtime) are conservatively treated as included.
    * Fragment cycles and missing fragments are left to the
      `NoFragmentCyclesRule` and `KnownFragmentNamesRule`; the traversal
      simply stops when it encounters them.
    * The traversal is iterative, so pathologically deep or large documents
      cannot exhaust the Python call stack.
    """

    def __init__(self, context: ValidationContext) -> None:
        super().__init__(context)
        self._get_fragment = context.get_fragment

    def _check_depth(self, node: Node, depth: int = 0) -> bool:
        """Check whether the maximum introspection depth has been reached.

        Counts the depth of list fields in "__Type" iteratively
        and returns `True` if the limit has been reached.
        """
        get_fragment = self._get_fragment
        # Names of the fragments on the current expansion path.
        fragments_on_path: set[str] = set()
        # An explicit work stack is used instead of recursion so that
        # pathologically deep documents cannot exhaust the call stack.
        # Fragment names are pushed as exit markers that remove the fragment
        # from the expansion path once its subtree has been processed.
        stack: list[Any] = [(node, depth)]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                fragments_on_path.discard(item)
                continue
            current, current_depth = item
            if self._is_statically_excluded(current):
                continue
            if isinstance(current, FragmentSpreadNode):
                fragment_name = current.name.value
                if fragment_name in fragments_on_path:
                    # Fragment cycles are handled by `NoFragmentCyclesRule`.
                    continue
                fragment = get_fragment(fragment_name)
                if not fragment:
                    # Missing fragments are handled by `KnownFragmentNamesRule`.
                    continue
                fragments_on_path.add(fragment_name)
                stack.append(fragment_name)
                stack.append((fragment, current_depth))
                continue
            if (
                isinstance(current, FieldNode)
                and current.name.value in INTROSPECTION_LIST_FIELDS
            ):
                current_depth += 1
                if current_depth >= MAX_LIST_DEPTH:
                    return True
            # handle fields, fragments and inline fragments
            selection_set = getattr(current, "selection_set", None)
            if selection_set:
                stack.extend(
                    (child, current_depth)
                    for child in reversed(selection_set.selections)
                )
        return False

    @staticmethod
    def _is_statically_excluded(node: Node) -> bool:
        """Whether a selection is statically excluded by `@skip`/`@include`.

        Only literal Boolean `if:` arguments are statically decidable;
        anything depending on variable values is treated as included.
        """
        directives = getattr(node, "directives", None)
        if not directives:
            return False
        for directive in directives:
            name = directive.name.value
            if name not in {"skip", "include"}:
                continue
            arguments = directive.arguments
            if not arguments:
                continue
            if_argument = next(
                (argument for argument in arguments if argument.name.value == "if"),
                None,
            )
            if if_argument is None:
                continue
            value = if_argument.value
            if isinstance(value, BooleanValueNode) and value.value == (name == "skip"):
                # `@skip(if: true)` or `@include(if: false)`
                return True
        return False

    def enter_field(self, node: FieldNode, *_args: Any) -> VisitorAction:
        if (
            node.name.value in INTROSPECTION_ROOT_FIELDS
            and not self._is_statically_excluded(node)
            and self._check_depth(node)
        ):
            self.report_error(
                GraphQLError(
                    "Maximum introspection depth exceeded",
                    [node],
                )
            )
            return SKIP
        return None
