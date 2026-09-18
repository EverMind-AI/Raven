"""What a multi-round run over a workspace needs, independent of who drives it.

A round is one pass of every role over one project: each role works the tree,
what it wrote is measured against what it may write, and what it learned is
appended to a file the next round reads. None of that is specific to the loop
that schedules the rounds, so it lives here rather than inside a driver, and
both drivers -- H*'s own loop and the playbook's ``mode: stint`` -- read it
from one place.
"""
