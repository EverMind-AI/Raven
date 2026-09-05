"""WeCom channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``WecomChannel`` — that would import
wecom_aibot_sdk at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``wecom.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
