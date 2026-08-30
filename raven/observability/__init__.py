"""What a raven span means: the attribute vocabulary and the usage it reports.

Span machinery -- context, suppression, the ``instrument`` decorator, the store
-- is kernel (:mod:`raven.tracing`) and stands alone. Deciding that an LLM call
span carries the routing backend, or that a usage block prices out at a number,
is product knowledge: it has to read the provider registry to split a model id
and the token ledger to cost one, and a kernel that reaches for those is not
standing alone. So the vocabulary sits out here, on a shelf, and the kernel
keeps the two contracts it can keep.

Instrumented call sites take an extractor as an argument
(``@trace.instrument("llm.call", extract=semconv.llm_call)``), so the extractor
travels with the caller and the kernel never names one.
"""

from __future__ import annotations

from raven.observability import semconv, usage

__all__ = ["semconv", "usage"]
