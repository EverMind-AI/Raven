"""Matrix channel adapter.

Intentionally does NOT re-export ``MatrixChannel`` — that would import
matrix-nio at package import and defeat cheap spec discovery
(``registry.discover_specs`` imports ``matrix.spec`` only). Construct via
``spec.SPEC.factory`` or import from ``.channel`` directly.
"""
