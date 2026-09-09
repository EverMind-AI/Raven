"""Model routing: the EcoClaw-style :class:`ModelRouter` (PinchBench benchmarks)
and the :class:`~raven.routing.knn_router.KNNModelRouter` (per-task reward
memory); both satisfy the ``RoutesModels`` paper.
"""

from raven.routing.router import ModelRouter

__all__ = ["ModelRouter"]
