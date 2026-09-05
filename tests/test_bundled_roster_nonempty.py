"""The factory shelf must never be silently empty (S-G guard).

Ruled 2026-08-27: everos is a must-install product default (memory backend
"everos" ships with raven). Nothing an import-rename tool can see connects the
config to the factory: it used to be a path built from segments, and it is now
an entry-point group name matched against a distribution's metadata, which a
rename leaves green while discovery silently finds nothing. This is the closed
loop the test suite lacked: assemble with production sources and assert the
factory cargo is actually on the roster.
"""


def test_bundled_discovery_finds_factory_cargo():
    from raven.core.plugin_stack import plugin_discovery_sources
    from raven.plugins.bootstrap import assemble_plugin_registry

    reg = assemble_plugin_registry(**plugin_discovery_sources())
    backends = set(reg.memory_backend_names())
    assert "everos" in backends, (
        "discovery lost the factory memory backend — check that "
        "`everos-memory` is installed and registers the `raven.plugins` "
        f"entry point; roster: {sorted(backends)}"
    )
    assert "understand_media" in set(reg.tool_names())


def test_the_wheels_own_shelf_carries_the_playbook_tools():
    """The bundled source is live again: the playbook entry tools are plugin
    cargo (raven/plugins/bundled/playbook), so this shelf going empty or
    undiscovered means every install silently loses the feature -- the same
    closed loop the everos assertion above guards for the entry-point source.
    """
    from raven.core.plugin_stack import plugin_discovery_sources
    from raven.plugins.bootstrap import assemble_plugin_registry

    sources = plugin_discovery_sources()
    assert sources["bundled_dir"] is not None and sources["bundled_dir"].is_dir()
    reg = assemble_plugin_registry(**sources)
    assert {"load_playbook", "create_playbook"} <= set(reg.tool_names())
    assert reg.tool_plugin_id("load_playbook") == "playbook"
