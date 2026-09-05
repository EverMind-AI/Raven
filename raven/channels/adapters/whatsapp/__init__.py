"""WhatsApp channel adapter (migrated to the capability contract).

Intentionally does NOT re-export ``WhatsAppChannel`` — that would import the
channel implementation (Node bridge client) at package import and defeat cheap
spec discovery (``registry.discover_specs`` imports ``whatsapp.spec`` only).
Construct via ``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
