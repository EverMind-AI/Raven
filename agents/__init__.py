"""Product definitions built on installed raven -- the A/B ground.

Each subdirectory is one agent product: a launcher plus its assets (config
baseline, mode overlays, roster row, secret slots), consuming the installed
raven as a library and serving over ``raven acp``. The retired vendored
forks were the A side (byte snapshots of their record live under
``tests/fixtures/vendored_fork/``); a product here is the B side of the same
transport face, and the acceptance bar is transport equivalence (transcript
shape, timeout semantics, everos records), never code equality.

Repo-level like ``evolver/``: the sixth import-linter contract keeps the
runtime from importing anything here back. The wheel carries the tree as data
(``raven/agents``, without this file) for the roster's file-level discovery.

The directory name is provisional by ruling ("naming to be settled"); while
everything in here is pure addition, a rename stays one ``git mv`` plus one
contract line.
"""
