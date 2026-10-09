#!/usr/bin/env python3
"""Download the resources raven reads at runtime, from a source checkout.

A shim. The fetcher moved into the package (:mod:`raven.resources`) so that an
install with no checkout can still run it; this file stays because the Makefile
and the Dockerfile call it by path, and a developer's fingers know it.

Run `raven resources` instead where there is no checkout to run this from.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from raven.resources import main  # noqa: E402 - after the path above, which is what makes it importable

if __name__ == "__main__":
    raise SystemExit(main())
