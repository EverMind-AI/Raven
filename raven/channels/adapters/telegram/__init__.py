"""Telegram channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``TelegramChannel`` — that would import the
python-telegram-bot SDK at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``telegram.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
