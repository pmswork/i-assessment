"""Planner-adjustable operational thresholds.

These used to be hardcoded module constants in `travel.py`, `rules.py` and
`coverage.py`. They're centralized here as a single mutable `Settings`
object so a planner can tune them from the Settings tab without a
redeploy.

Every consumer resolves the *current* value at call time, not at import
time (the same pattern `reliability.py` uses for its db_path) — so a
change here takes effect immediately for the next auto-assignment run and
every subsequent manual validation, with no caching to invalidate.

Persisted to the `settings` table in `app.db` (see `db.py`) so tuning
survives a restart; falls back to the field defaults below on first run.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import NamedTuple

AUTO_ASSIGN_RISK_LEVELS = {
    0: "Careful - only clean matches",
    1: "Balanced - allow interpreter-choice warnings",
    2: "Flexible - allow all warning-level matches",
    3: "God-Mode - manual override available",
}


@dataclass(frozen=True)
class Settings:
    auto_assign_risk_level: int = 2
    average_speed_kmh: float = 45.0
    fixed_overhead_min: float = 10.0
    travel_buffer_min: float = 15.0
    long_distance_km: float = 40.0
    workload_imbalance_threshold_min: float = 120.0
    coverage_radius_km: float = 100.0
    coverage_bar_cap: int = 3
    urgent_unassigned_days: int = 3


class FieldInfo(NamedTuple):
    label: str
    help_text: str
    min_value: float
    options: dict[int, str] | None = None


# Human-readable labels/explanations for the settings form, and a sane
# floor for validation (everything here is a positive, physically
# meaningful quantity — zero or negative would be nonsensical).
FIELD_INFO: dict[str, FieldInfo] = {
    "auto_assign_risk_level": FieldInfo(
        "Auto-assignment autonomy",
        "How much judgement the system may use by itself.\n"
        "Careful leaves every warning for a planner.\n"
        "Balanced may auto-assign warnings that are mainly up to the interpreter, such as long commute home.\n"
        "Flexible may auto-assign any warning-level match.\n"
        "God-Mode additionally lets a planner manually override validation blocks, except overlapping bookings.",
        0.0,
        AUTO_ASSIGN_RISK_LEVELS,
    ),
    "average_speed_kmh": FieldInfo(
        "Average travel speed (km/h)",
        "Used to convert straight-line distance into an estimated travel time.",
        1.0,
    ),
    "fixed_overhead_min": FieldInfo(
        "Fixed overhead per trip (min)",
        "Added to every travel estimate for parking/walking in, or dialling into a call.",
        0.0,
    ),
    "travel_buffer_min": FieldInfo(
        "Tight-travel warning buffer (min)",
        "Extra gap beyond the bare minimum required that still gets flagged as a warning "
        "instead of accepted cleanly.",
        0.0,
    ),
    "long_distance_km": FieldInfo(
        "Long-distance warning threshold (km)",
        "One-way home-to-job distance beyond which a warning is raised.",
        0.0,
    ),
    "workload_imbalance_threshold_min": FieldInfo(
        "Workload imbalance threshold (min)",
        "How far an interpreter's total booked time can exceed the least-loaded qualified "
        "alternative before a warning is raised.",
        0.0,
    ),
    "coverage_radius_km": FieldInfo(
        "Coverage radius (km)",
        "Straight-line radius used to count nearby qualified interpreters for the Coverage indicator.",
        0.0,
    ),
    "coverage_bar_cap": FieldInfo(
        "Coverage gauge cap (count)",
        'Number of nearby interpreters that reads as "full" on the Coverage gauge.',
        1.0,
    ),
    "urgent_unassigned_days": FieldInfo(
        "Urgent unassigned threshold (days)",
        "Unassigned jobs due within this many Amsterdam calendar days are highlighted in red on the overview.",
        0.0,
    ),
}

_current = Settings()


def get() -> Settings:
    return _current


def set_current(settings: Settings) -> None:
    """Replace the whole settings object (used when loading from db.py)."""
    global _current
    _current = settings


def update(**changes: float) -> Settings:
    """Validate and apply a partial update, returning the new settings."""
    for key, value in changes.items():
        if key not in FIELD_INFO:
            raise ValueError(f"Unknown setting: {key}")
        if key == "auto_assign_risk_level":
            value = int(value)
            changes[key] = value
            if value not in AUTO_ASSIGN_RISK_LEVELS:
                raise ValueError("Auto-assignment autonomy must be one of the listed options.")
            continue
        if key in ("coverage_bar_cap", "urgent_unassigned_days"):
            value = int(value)
            changes[key] = value
        if value < FIELD_INFO[key].min_value:
            raise ValueError(
                f"{FIELD_INFO[key].label} must be at least {FIELD_INFO[key].min_value}, got {value}."
            )
    global _current
    _current = replace(_current, **changes)
    return _current


def reset() -> Settings:
    global _current
    _current = Settings()
    return _current


def field_names() -> list[str]:
    return [f.name for f in fields(Settings)]
