"""Feishu/Lark channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``FeishuChannel`` — that would import lark_oapi
at package import and defeat cheap spec discovery (``registry.discover_specs``
imports ``feishu.spec`` only). Construct via ``spec.SPEC.factory`` or import from
``.channel`` directly.
"""
