"""Driver registry. One factory per benchmark."""

from __future__ import annotations

from ..driver import BenchmarkDriver


def get_driver(name: str) -> BenchmarkDriver:
    name = name.lower()
    if name == "pbench":
        from .pbench import PbenchDriver

        return PbenchDriver()
    if name == "longrun":
        from .longrun import LongRunDriver

        return LongRunDriver()
    raise ValueError(f"Unknown benchmark '{name}'. Registered: pbench, longrun")


__all__ = ["BenchmarkDriver", "get_driver"]
