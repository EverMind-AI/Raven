"""Email channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``EmailChannel`` — that would import the IMAP/
SMTP channel implementation at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``email.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
