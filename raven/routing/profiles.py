"""Routing profiles — quality vs cost trade-offs."""

from raven.routing.types import RoutingProfile, RoutingProfileName

ROUTING_PROFILES: dict[RoutingProfileName, RoutingProfile] = {
    "best":     RoutingProfile(quality_weight=0.99, cost_weight=0.01),
    "balanced": RoutingProfile(quality_weight=0.50, cost_weight=0.50),
    "eco":      RoutingProfile(quality_weight=0.20, cost_weight=0.80),
}
