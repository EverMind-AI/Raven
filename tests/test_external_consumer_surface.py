"""Tripwire for the four symbols external installers import (S-G item).

The vendored product trees (subagents/raven-*/install.py) run against the
INSTALLED raven package and import exactly these four functions to register
themselves as third-party subagents. Those installers are not in this repo's
CI, so this tripwire stands in for them: renaming or moving any of the four
breaks four downstream products at their next install. Frozen for v0.2.0
(the subagents/ tree is frozen for v0.2.0; external consumers pin this surface); changes must
be coordinated with the product teams first.
"""


def test_update_subagents_registration_surface_is_stable():
    from raven.config.update_subagents import (
        add_third_party_subagent,
        get_third_party_subagents,
    )

    assert callable(add_third_party_subagent)
    assert callable(get_third_party_subagents)


def test_config_loader_surface_is_stable():
    from raven.config.loader import get_config_path, read_raw_or_raise

    assert callable(get_config_path)
    assert callable(read_raw_or_raise)
