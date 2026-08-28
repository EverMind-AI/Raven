"""The factory shelf must never be silently empty (S-G guard).

Ruled 2026-08-27: everos is a must-install product default (memory backend
"everos" ships with raven). Discovery paths are built from path SEGMENTS
(`... / "plugins" / "memory"`), which import-rename tooling cannot see — the
2026-08-28 rename rehearsal proved a rename can leave every import green while
bundled discovery silently finds nothing. This is the closed loop the test
suite lacked: assemble with production sources and assert the factory cargo
is actually on the roster.
"""


def test_bundled_discovery_finds_factory_cargo():
    from raven.core.plugin_stack import plugin_discovery_sources
    from raven.plugins.bootstrap import assemble_plugin_registry

    reg = assemble_plugin_registry(**plugin_discovery_sources())
    backends = set(reg.memory_backend_names())
    assert "everos" in backends, (
        "bundled discovery lost the factory memory backend — check the "
        f"segment-built bundled_dir path; roster: {sorted(backends)}"
    )
    assert "understand_media" in set(reg.tool_names())
