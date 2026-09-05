"""Mochat channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``MochatChannel`` — that would import the
channel implementation (API/socket client) at package import and defeat cheap
spec discovery (``registry.discover_specs`` imports ``mochat.spec`` only).
Construct via ``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
