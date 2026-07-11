"""Straight-line distance and travel-time estimation.

No routing service is available, so distance is approximated with the
haversine great-circle formula (`haversine_km`) — the straight-line
"as the crow flies" distance between two coordinates, **not** a routed
road distance. It is then converted to minutes (`travel_minutes_for_distance`)
with a documented, easily-replaceable assumption:

  - average effective road speed in the Netherlands: 45 km/h (a mix of city
    driving, provincial roads and short highway hops between the towns in
    this dataset)
  - fixed overhead per leg: 10 minutes (parking / walking in, or dialling
    into a call)

Distance and the minutes conversion are kept as two separate functions on
purpose: callers that only care about "how far" (e.g. coverage counts) can
use `haversine_km` directly without pulling in the speed assumption, and
the speed assumption can be swapped independently of the distance formula.

This is intentionally crude and makes no network calls. Swap
`travel_minutes_for_distance` for a real routing API's response (e.g.
OSRM, Google Distance Matrix) without touching any caller — every caller
only depends on this module's function signatures, not on how the numbers
are produced.

The 45 km/h and 10-minute figures are planner-adjustable defaults, not
hardcoded constants — see `settings.py`. This function reads the *current*
setting on every call, so a change in the Settings tab applies immediately.
"""

from __future__ import annotations

import math

from . import settings

Coordinate = tuple[float, float]


def haversine_km(a: Coordinate, b: Coordinate) -> float:
    """Straight-line ("as the crow flies") distance in km. Not a road distance."""
    lat1, lon1 = a
    lat2, lon2 = b
    r_km = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r_km * math.asin(math.sqrt(h))


def travel_minutes_for_distance(distance_km: float) -> float:
    """Convert a straight-line distance into an estimated travel time.

    This is a flat-speed approximation, not a routed ETA — real road
    distance (and therefore time) is almost always longer than the
    straight-line distance it's derived from.
    """
    current = settings.get()
    return distance_km / current.average_speed_kmh * 60 + current.fixed_overhead_min


def estimate_travel_minutes(a: Coordinate, b: Coordinate) -> float:
    """Convenience wrapper: straight-line distance between two coordinates,
    converted directly to estimated minutes."""
    return travel_minutes_for_distance(haversine_km(a, b))
