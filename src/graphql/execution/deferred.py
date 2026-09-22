"""Deferred values for synchronous batched loading.

This module implements an explicitly synchronous variant of the "DataLoader"
pattern. A resolver may return a :class:`DeferredValue` (a reference to some
loader and some key) instead of an actual value. The executor then collects
all deferred values of the currently executable layer, dispatches them in one
batch per loader, and only then continues to complete the fields, which may
produce the next round of deferred values.
"""

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from ..error import GraphQLError, located_error
from ..pyutils import Path, Undefined

if TYPE_CHECKING:
    from ..language import FieldNode
    from ..type import GraphQLOutputType, GraphQLResolveInfo
    from .execute import ExecutionContext

__all__ = ["DeferredValue", "SyncBatchLoader"]


class DeferredValue:
    """A reference to a value that a loader will provide in a batch.

    Resolvers may return a ``DeferredValue`` instead of an actual value to
    take part in synchronous batched loading. The ``loader`` can be a
    :class:`SyncBatchLoader`, any object with a ``batch_load(keys)`` method,
    or a plain callable accepting a list of keys. The ``key`` identifies the
    value within the loader and must be hashable.

    The batch load function receives the list of unique keys of one round and
    must return a list of the same length with either the value or an
    exception for each key. An exception fails only the fields depending on
    that key, bubbling up according to the nullability of the fields.
    """

    __slots__ = "loader", "key"

    loader: Any
    key: Any

    def __init__(self, loader: Any, key: Any) -> None:
        self.loader = loader
        self.key = key

    def __repr__(self) -> str:
        return f"DeferredValue(loader={self.loader!r}, key={self.key!r})"


BatchLoadFn = Callable[[List[Any]], List[Any]]


class SyncBatchLoader:
    """A synchronous loader that loads values for keys in batches.

    The given ``batch_load_fn`` is called with the list of unique keys that
    have been requested in one round of execution and must return a list of
    the same length with either the value or an exception for each key.

    Loaders do not cache between rounds or between executions; deduplication
    of keys only happens within a single round of execution.
    """

    __slots__ = ("batch_load_fn",)

    batch_load_fn: BatchLoadFn

    def __init__(self, batch_load_fn: BatchLoadFn) -> None:
        if not callable(batch_load_fn):
            raise TypeError(
                "SyncBatchLoader must be created with a callable, got:"
                f" {batch_load_fn!r}."
            )
        self.batch_load_fn = batch_load_fn

    def __repr__(self) -> str:
        return f"SyncBatchLoader(batch_load_fn={self.batch_load_fn!r})"

    def load(self, key: Any) -> DeferredValue:
        """Return a deferred value for the given key."""
        return DeferredValue(self, key)

    def load_many(self, keys: List[Any]) -> List[DeferredValue]:
        """Return a list of deferred values for the given keys."""
        return [self.load(key) for key in keys]

    def batch_load(self, keys: List[Any]) -> List[Any]:
        """Load the values for the given keys in one batch."""
        return self.batch_load_fn(keys)


class DeferredNode:
    """Base class for the internal nodes of a deferred result tree."""

    __slots__ = ("parent",)

    parent: Optional["DeferredNode"]

    def __init__(self) -> None:
        self.parent = None

    def fail(self, context: "ExecutionContext", error: Exception) -> None:
        """Handle an error that occurred while completing this node."""
        raise NotImplementedError

    def propagate(self, context: "ExecutionContext", error: GraphQLError) -> None:
        """Pass an error that could not be handled on to the parent node."""
        parent = self.parent
        if parent is None:
            context.handle_deferred_root_error(error)
        else:
            parent.fail(context, error)

    def unwrap(self) -> Any:
        """Return the final plain value of this node."""
        raise NotImplementedError


class DeferredStructuralNode(DeferredNode):
    """Base class for deferred nodes that only forward errors to the parent."""

    __slots__ = ()

    def fail(self, context: "ExecutionContext", error: Exception) -> None:
        self.propagate(context, cast_graphql_error(error))


class DeferredDictNode(DeferredStructuralNode):
    """A deferred node wrapping a dictionary of field results."""

    __slots__ = ("results",)

    results: Dict[str, Any]

    def __init__(self, results: Dict[str, Any]) -> None:
        super().__init__()
        self.results = results
        for value in results.values():
            if isinstance(value, DeferredNode):
                value.parent = self

    def unwrap(self) -> Dict[str, Any]:
        return {key: unwrap_deferred(value) for key, value in self.results.items()}


class DeferredListNode(DeferredStructuralNode):
    """A deferred node wrapping a list of item results."""

    __slots__ = ("items",)

    items: List[Any]

    def __init__(self, items: List[Any]) -> None:
        super().__init__()
        self.items = items
        for item in items:
            if isinstance(item, DeferredNode):
                item.parent = self

    def unwrap(self) -> List[Any]:
        return [unwrap_deferred(item) for item in self.items]


class DeferredBoundaryNode(DeferredNode):
    """A deferred node adding an error boundary for a field or a list item.

    This mirrors the protection that the executor applies around the
    completion of a field value or list item: errors are handled with the
    given output type and only propagated to the parent node if that type is
    non-null.
    """

    __slots__ = ("return_type", "field_nodes", "path", "child", "nulled")

    return_type: "GraphQLOutputType"
    field_nodes: List["FieldNode"]
    path: Path
    child: DeferredNode
    nulled: bool

    def __init__(
        self,
        return_type: "GraphQLOutputType",
        field_nodes: List["FieldNode"],
        path: Path,
        child: DeferredNode,
    ) -> None:
        super().__init__()
        self.return_type = return_type
        self.field_nodes = field_nodes
        self.path = path
        self.child = child
        self.nulled = False
        child.parent = self

    def fail(self, context: "ExecutionContext", error: Exception) -> None:
        located = located_error(error, self.field_nodes, self.path.as_list())
        try:
            context.handle_field_error(located, self.return_type)
        except GraphQLError as bubbling:
            self.propagate(context, bubbling)
        else:
            self.nulled = True

    def unwrap(self) -> Any:
        return None if self.nulled else unwrap_deferred(self.child)


class DeferredLoadNode(DeferredNode):
    """A deferred node waiting for a value from a batched loader.

    The node also acts as the error boundary for the completion of the loaded
    value, mirroring the protection that the executor applies around the
    completion of a field value or list item.
    """

    __slots__ = (
        "loader",
        "key",
        "return_type",
        "field_nodes",
        "info",
        "path",
        "outcome",
        "resolved",
        "nulled",
    )

    loader: Any
    key: Any
    return_type: "GraphQLOutputType"
    field_nodes: List["FieldNode"]
    info: "GraphQLResolveInfo"
    path: Path
    outcome: Any
    resolved: Any
    nulled: bool

    def __init__(
        self,
        loader: Any,
        key: Any,
        return_type: "GraphQLOutputType",
        field_nodes: List["FieldNode"],
        info: "GraphQLResolveInfo",
        path: Path,
    ) -> None:
        super().__init__()
        self.loader = loader
        self.key = key
        self.return_type = return_type
        self.field_nodes = field_nodes
        self.info = info
        self.path = path
        self.outcome = Undefined
        self.resolved = None
        self.nulled = False

    def complete(self, context: "ExecutionContext") -> None:
        """Complete the node with the outcome of the batched load."""
        outcome = self.outcome
        if isinstance(outcome, Exception):
            self.fail(context, outcome)
            return
        try:
            completed = context.complete_value(
                self.return_type, self.field_nodes, self.info, self.path, outcome
            )
        except Exception as raw_error:
            self.fail(context, raw_error)
        else:
            self.resolved = completed
            if isinstance(completed, DeferredNode):
                # Completing the loaded value has itself produced deferred
                # nodes; their errors are handled by this node's boundary.
                completed.parent = self

    def fail(self, context: "ExecutionContext", error: Exception) -> None:
        located = located_error(error, self.field_nodes, self.path.as_list())
        try:
            context.handle_field_error(located, self.return_type)
        except GraphQLError as bubbling:
            self.propagate(context, bubbling)
        else:
            self.nulled = True

    def unwrap(self) -> Any:
        return None if self.nulled else unwrap_deferred(self.resolved)


def cast_graphql_error(error: Exception) -> GraphQLError:
    """Ensure that the given error is a GraphQLError."""
    return error if isinstance(error, GraphQLError) else located_error(error)


def unwrap_deferred(value: Any) -> Any:
    """Replace deferred nodes with their final values, if there are any."""
    if isinstance(value, DeferredNode):
        return value.unwrap()
    if isinstance(value, dict):
        if any(isinstance(item, DeferredNode) for item in value.values()):
            return {key: unwrap_deferred(item) for key, item in value.items()}
    elif isinstance(value, list):
        if any(isinstance(item, DeferredNode) for item in value):
            return [unwrap_deferred(item) for item in value]
    return value
