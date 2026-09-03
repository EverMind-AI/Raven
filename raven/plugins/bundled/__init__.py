"""The wheel's own plugin shelf.

Every subdirectory holding a ``raven-plugin.toml`` is a plugin the wheel
ships and every install discovers (origin BUNDLED, which nothing shadows).
The cargo here obeys the same contract as third-party plugins -- manifest,
factories, ``ServiceLocator`` / ``RuntimeHandles`` grants -- so a feature
living here proves the contribution surface is wide enough for it, and
moving it out to its own distribution later is a packaging change, not a
refactor. First resident: the playbook entry tools.
"""
