"""Pin the absence of centralized OpenTelemetry tracing / exporter.

No ``raven/**`` module imports opentelemetry, and no OTEL sdk or exporter is
installed. Adding either (or an import) later must break this test so the
decision is revisited deliberately.

``opentelemetry-api`` itself is present, and that is the deliberate part: the
A2A protocol face depends on ``a2a-sdk``, which requires ``google-api-core``,
which has required ``opentelemetry-api`` at base since 2.36. The api package
alone cannot trace anything -- with no sdk installed ``get_tracer`` hands back a
``ProxyTracer`` whose spans are ``NonRecordingSpan``, asserted below -- so it
changes nothing about what this file protects. Asserting its absence instead
would pin which transitive dependency a vendor happens to declare, which is not
the decision worth guarding.

The reversal, if that trade is ever judged wrong, is to drop ``a2a-sdk``; a
backwards pin on ``google-api-core`` would also work and is worse, since it
freezes an unrelated dependency to dodge a test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import raven


def _absent(module: str) -> bool:
    """Whether `module` cannot be imported. A missing parent raises rather than
    returning None, and either way the answer is the same."""
    try:
        return importlib.util.find_spec(module) is None
    except (ImportError, ModuleNotFoundError, ValueError):
        return True


def test_no_opentelemetry_sdk_or_exporter_is_installed():
    assert _absent("opentelemetry.sdk"), "an OTEL sdk makes tracing real; revisit the decision"
    assert _absent("opentelemetry.exporter"), "an OTEL exporter ships spans somewhere; revisit the decision"


def test_the_api_present_transitively_cannot_record():
    """The reason the api package's presence is not what this file guards."""
    from opentelemetry import trace

    with trace.get_tracer(__name__).start_as_current_span("probe") as span:
        assert span.is_recording() is False


def test_no_raven_module_imports_opentelemetry():
    root = Path(raven.__file__).resolve().parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "opentelemetry" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
