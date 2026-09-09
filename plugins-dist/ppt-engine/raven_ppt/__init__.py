"""PPT deck authoring, shipped as its own raven plugin distribution.

The capability is one package rather than a set of edits spread through the
host because it has to be removable: installing the ``ppt-engine`` wheel adds
it, and nothing host-side knows it exists beyond the ``raven.plugins`` entry
point that names this package. The fork carried the same subtree as
``raven/ppt`` inside its own checkout; here it is respelled to ``raven_ppt``
and contributed through the plugin surface instead of a forked assembly point.

Layers, and the only direction dependencies may run:

    contracts <- services <- backends <- stages <- profiles <- tools

``tests/test_ppt_engine_layering.py`` enforces that with a static check,
because the boundary is the whole design: a service that reaches forward into
a stage has made itself part of one route, and the routes exist precisely so
that the services can be shared by all of them.

Beside the engine subtree the package carries its assets (``assets/templates``
for the fetched template payload, ``services/assets`` for fonts and shape
data), the deck-authoring skill (``skill/``), the fork's three drifted
identity prompts (``prompts/``), and the plugin layer (``plugin/``) whose
factories the manifest ``raven-plugin.toml`` names.

Kept import-cheap on purpose: PluginDiscovery imports this module to resolve
``raven-plugin.toml``, so nothing here may pull python-pptx or the engine.
"""
