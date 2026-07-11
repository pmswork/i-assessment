"""Coverage: how many qualified interpreters could realistically reach a job.

This is a resource-scarcity signal for planners, separate from full
feasibility (`rules.validate_assignment`). It answers "how many language/
sworn-qualified interpreters live within a reasonable straight-line
distance of this job?" — it does NOT account for availability, existing
bookings, or exact travel time, and the distance itself is straight-line
haversine (see `travel.py`), not a routed distance. Treat the numbers here
as approximate, at-a-glance context, not a validation result.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import settings
from .models import Interpreter, Job
from .rules import is_qualified
from .travel import haversine_km


@dataclass(frozen=True)
class CoverageStats:
    qualified_total: int
    within_radius: int
    distance_applicable: bool  # False for remote jobs: distance doesn't apply

    @property
    def is_scarce(self) -> bool:
        """True when interpreters exist for this language/sworn combination,
        but none of them are within the coverage radius — i.e. any assignment
        will involve real travel, not just a scheduling squeeze."""
        return self.distance_applicable and self.qualified_total > 0 and self.within_radius == 0


def coverage_stats(job: Job, interpreters: list[Interpreter], radius_km: float | None = None) -> CoverageStats:
    if radius_km is None:
        radius_km = settings.get().coverage_radius_km

    qualified = [i for i in interpreters if is_qualified(job, i)]

    if not job.is_on_site or job.location is None:
        return CoverageStats(qualified_total=len(qualified), within_radius=len(qualified), distance_applicable=False)

    within = [i for i in qualified if haversine_km((i.home_lat, i.home_lon), job.location) <= radius_km]
    return CoverageStats(qualified_total=len(qualified), within_radius=len(within), distance_applicable=True)


@dataclass(frozen=True)
class CoverageGauge:
    """A signal-strength-style reading for the job-list UI: `segments_lit`
    out of `segments_total` bars are lit, like a phone signal or battery
    icon, colour-coded by `level`."""

    segments_lit: int
    segments_total: int
    level: str  # "none" | "empty" | "low" | "mid" | "good"


def coverage_gauge(stats: CoverageStats, cap: int | None = None) -> CoverageGauge:
    if cap is None:
        cap = settings.get().coverage_bar_cap

    if stats.qualified_total == 0:
        # Nobody qualifies at all — a different, already-reported problem
        # (see coverage_label). No bars lit, distinct "none" level so it
        # doesn't visually read the same as "qualified but zero nearby".
        return CoverageGauge(0, cap, "none")

    lit = min(stats.within_radius, cap)
    if lit == 0:
        level = "empty"
    elif lit == 1:
        level = "low"
    elif lit < cap:
        level = "mid"
    else:
        level = "good"
    return CoverageGauge(lit, cap, level)


def coverage_label(stats: CoverageStats, radius_km: float | None = None) -> str:
    """Compact string for the job-list UI."""
    if stats.qualified_total == 0:
        # No language/sworn match at all — a different, already-reported
        # problem (see scheduler._explain_unassigned); "0/0 <=100km" would
        # just be confusing here.
        return "0 qualified"
    if not stats.distance_applicable:
        return f"{stats.qualified_total} qualified (remote)"
    if radius_km is None:
        radius_km = settings.get().coverage_radius_km
    return f"{stats.within_radius}/{stats.qualified_total} ≤{radius_km:.0f}km"
