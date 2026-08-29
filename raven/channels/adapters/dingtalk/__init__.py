"""DingTalk channel adapter.

Intentionally does NOT re-export ``DingTalkChannel`` — that would import
dingtalk_stream at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``dingtalk.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
