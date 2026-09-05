"""Discord channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``DiscordChannel`` — that would import
httpx/websockets at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``discord.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
