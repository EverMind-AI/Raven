"""Slack channel adapter.

Intentionally does NOT re-export ``SlackChannel`` — that would import slack_sdk
at package import and defeat cheap spec discovery (``registry.discover_specs``
imports ``slack.spec`` only). Construct via ``spec.SPEC.factory`` or import from
``.channel`` directly.
"""
