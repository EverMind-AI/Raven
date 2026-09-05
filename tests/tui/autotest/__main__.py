"""Entry point for ``python -m tests.tui.autotest``, delegating to the autotest CLI."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
