"""QQ channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``QQChannel`` — that would import botpy at
package import and defeat cheap spec discovery (``registry.discover_specs``
imports ``qq.spec`` only). Construct via ``spec.SPEC.factory`` or import from
``.channel`` directly.
"""
