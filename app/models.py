"""Domain models for the interpreter planner.

Kept as plain dataclasses with no framework dependencies so the business
rules in `rules.py` and `scheduler.py` can be unit tested without a web
server or database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time


@dataclass(frozen=True)
class Interpreter:
    interpreter_id: str
    name: str
    language: str
    sworn: bool
    home_city: str
    home_lat: float
    home_lon: float
    rate_eur_per_hour: float
    # date -> (window_start, window_end), or absent if not available that day
    availability: dict[date, tuple[time, time]] = field(default_factory=dict)

    def availability_on(self, day: date) -> tuple[time, time] | None:
        return self.availability.get(day)


@dataclass(frozen=True)
class Job:
    job_id: str
    date: date
    start_time: time
    duration_min: int
    end_time: time
    language: str
    sworn_required: bool
    modality: str  # "on-site" | "remote"
    client: str
    address: str
    city: str
    lat: float | None
    lon: float | None

    @property
    def is_on_site(self) -> bool:
        return self.modality == "on-site"

    @property
    def start_dt(self) -> datetime:
        return datetime.combine(self.date, self.start_time)

    @property
    def end_dt(self) -> datetime:
        return datetime.combine(self.date, self.end_time)

    @property
    def location(self) -> tuple[float, float] | None:
        if self.lat is None or self.lon is None:
            return None
        return (self.lat, self.lon)
