"""Max introspection depth rule"""

from typing import Any

from ...error import GraphQLError
from ...language import (
    SKIP,
    BooleanValueNode,
    DirectiveNode,
    FieldNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    Node,
    VisitorAction,
)
from . import ASTValidationRule

__all__ = ["MaxIntrospectionDepthRule"]

MAX_LIST_DEPTH = 3
INTROSPECTION_LIST_FIELDS = frozenset(
    {"fields", "inputFields", "interfaces", "possibleTypes"}
)


def _static_boolean_argument(directive: DirectiveNode | None) -> bool | None:
    """Return a boolean literal argument, or ``None`` when not statically known."""
    arguments = directive.arguments if directive else None
    if not arguments or len(arguments) != 1:
        return None
    argument = arguments[0]
    if argument.name.value != "if" or not isinstance(argument.value, BooleanValueNode):
        return None
    return argument.value.value


def _is_statically_excluded(
    node: FieldNode | FragmentSpreadNode | InlineFragmentNode,
) -> bool:
    """Check whether a conditional selection is excluded without variable values.

    Only boolean literals are considered statically known. Variables are treated as
    potentially included, even when the operation defines a default, because request
    variables can override that default. Malformed or duplicated directives are also
    treated conservatively as potentially included.
    """
    directives = node.directives or ()
    skip_directives = [d for d in directives if d.name.value == "skip"]
    include_directives = [d for d in directives if d.name.value == "include"]
    if len(skip_directives) > 1 or len(include_directives) > 1:
        return False

    skip = _static_boolean_argument(next(iter(skip_directives), None))
    include = _static_boolean_argument(next(iter(include_directives), None))
    return skip is True or include is False


class MaxIntrospectionDepthRule(ASTValidationRule):
    """Checks maximum introspection depth"""

    def _exceeds_max_depth(self, root: FieldNode) -> int | None:
        """Return the first depth exceeding the limit for one introspection root.

        Each introspection root and each response branch keeps its own depth. The
        traversal uses an explicit stack and a per-branch fragment set, so recursive
        fragments terminate without recursion or memoizing paths by type or alias.
        """
        get_fragment = self.context.get_fragment
        stack: list[tuple[Node, int, frozenset[str]]] = [(root, 0, frozenset())]

        while stack:
            node, depth, active_fragments = stack.pop()

            if isinstance(node, FragmentSpreadNode):
                if _is_statically_excluded(node):
                    continue
                fragment_name = node.name.value
                if fragment_name in active_fragments:
                    # Fragment cycles are handled by `NoFragmentCyclesRule`.
                    continue
                fragment = get_fragment(fragment_name)
                if fragment is None:
                    # Missing fragments checks are handled by `KnownFragmentNamesRule`.
                    continue
                stack.append((fragment, depth, active_fragments | {fragment_name}))
                continue

            if isinstance(node, InlineFragmentNode):
                if _is_statically_excluded(node):
                    continue
                selections = node.selection_set.selections
            elif isinstance(node, FieldNode):
                if _is_statically_excluded(node):
                    continue
                next_depth = depth
                if node.name.value in INTROSPECTION_LIST_FIELDS:
                    next_depth += 1
                    if next_depth >= MAX_LIST_DEPTH:
                        return next_depth
                selection_set = node.selection_set
                selections = selection_set.selections if selection_set else ()
                depth = next_depth
            else:
                selection_set = getattr(node, "selection_set", None)
                selections = selection_set.selections if selection_set else ()

            stack.extend(
                (child, depth, active_fragments) for child in reversed(selections)
            )

        return None

    def enter_field(self, node: FieldNode, *_args: Any) -> VisitorAction:
        depth = (
            self._exceeds_max_depth(node)
            if node.name.value
            in (
                "__schema",
                "__type",
            )
            else None
        )
        if depth is not None:
            self.report_error(
                GraphQLError(
                    "Maximum introspection depth exceeded",
                    [node],
                    extensions={"depth": depth, "maximumDepth": MAX_LIST_DEPTH},
                )
            )
            return SKIP
        return None
