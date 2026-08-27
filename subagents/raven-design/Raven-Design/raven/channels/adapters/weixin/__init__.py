"""Personal WeChat (iLink) channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``WeixinChannel`` — that would import httpx at
package import and defeat cheap spec discovery (``registry.discover_specs``
imports ``weixin.spec`` only). Construct via ``spec.SPEC.factory`` or import from
``.channel`` directly.
"""
