"""Load synchronous host translations while keeping decisions in strategies."""

from inspect import iscoroutinefunction, signature

from .materialize import load_factory


def translator(reference, package, count):
    if reference is None:
        return None
    function = load_factory(reference.root, package)
    if iscoroutinefunction(function):
        raise TypeError("strategy translations must be synchronous; async decisions belong in the strategy")
    signature(function).bind(*[object() for _ in range(count)])
    return function
