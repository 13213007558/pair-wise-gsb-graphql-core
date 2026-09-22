"""Synchronous deferred batch loading.

This module provides an explicit, synchronous batch loading interface for the
GraphQL executor, similar in spirit to the JavaScript DataLoader, but without
relying on an event loop. Resolvers return a :class:`DeferredValue` (created
via :meth:`SyncDataLoader.load`) instead of an actual value. The executor
collects the deferred values of the currently executable layer, calls the
batch loading function of each loader once with all gathered keys, and then
continues completing the fields with the loaded values.
"""

from typing import Any, Callable, Hashable, List, Sequence

__all__ = ["DeferredValue", "SyncDataLoader"]


class DeferredValue:
    """A reference to a value that will be loaded in a batch by the executor.

    A deferred value is created via :meth:`SyncDataLoader.load` and can be
    returned from a resolver instead of the actual value. The executor will
    replace it with the value that the loader's batch loading function
    provides for the deferred value's key.
    """

    __slots__ = "loader", "key"

    loader: "SyncDataLoader"
    key: Hashable

    def __init__(self, loader: "SyncDataLoader", key: Hashable) -> None:
        self.loader = loader
        self.key = key

    def __repr__(self) -> str:
        name = self.__class__.__name__
        return f"{name}(loader={self.loader!r}, key={self.key!r})"


class SyncDataLoader:
    """A synchronous batch loading function with per-execution caching.

    The loader is initialized with a batch loading function that accepts a
    sequence of keys and returns a sequence of values (or exceptions) of the
    same length, with each value corresponding to the key at the same
    position.

    Resolvers call :meth:`load` with an individual (hashable) key and return
    the resulting :class:`DeferredValue`. The executor gathers the keys of
    all deferred values of the currently executable layer, calls the batch
    loading function once with these keys, and then continues execution with
    the loaded values. If a loaded value is an exception, it is raised as a
    field error at the position of the corresponding field, bubbling
    according to the nullability of the field type, without affecting the
    other values of the batch.

    Within one execution of an operation, each key is only loaded once per
    loader and the result is reused for all positions that refer to the same
    key. The cache is held by the execution context, so it is never shared
    between executions and loaders can safely be reused across requests.
    """

    __slots__ = ("batch_load_fn",)

    batch_load_fn: Callable[[Sequence[Hashable]], Sequence[Any]]

    def __init__(
        self, batch_load_fn: Callable[[Sequence[Hashable]], Sequence[Any]]
    ) -> None:
        if not callable(batch_load_fn):
            raise TypeError(
                "SyncDataLoader must be constructed with a batch loading"
                f" function, but got: {batch_load_fn!r}."
            )
        self.batch_load_fn = batch_load_fn

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(batch_load_fn={self.batch_load_fn!r})"

    def load(self, key: Hashable) -> DeferredValue:
        """Load the value for the given key in a batch.

        Returns a :class:`DeferredValue` that a resolver can return instead
        of the actual value.
        """
        try:
            hash(key)
        except TypeError:
            raise TypeError(
                "SyncDataLoader.load() must be called with a hashable key,"
                f" but got: {key!r}."
            ) from None
        return DeferredValue(self, key)

    def load_many(self, keys: Sequence[Hashable]) -> List[DeferredValue]:
        """Load the values for the given keys in a batch.

        Returns a list of :class:`DeferredValue` objects, one for each key.
        """
        return [self.load(key) for key in keys]
