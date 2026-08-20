"""PPT deck authoring.

The capability is one package rather than a set of edits spread through the
agent because it has to be removable: `raven[ppt]` installs it, and nothing
outside `raven/ppt/` plus a single assembly point in the tool registry knows it
exists.

Layers, and the only direction dependencies may run:

    contracts <- services <- backends <- stages <- profiles <- tools

`tests/ppt/test_layering.py` enforces that with a static check, because the
boundary is the whole design: a service that reaches forward into a stage has
made itself part of one route, and the routes exist precisely so that the
services can be shared by all of them.
"""
