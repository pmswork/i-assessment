"""Interpreter reliability ledger: a scoring signal that is meant to matter
more than "who said yes first."

In the real business this app is modelling, jobs are often filled through a
first-come-first-served accept/decline auction, and that process alone
rewards whoever is fastest to tap "accept" — not whoever is actually
dependable. This module tracks the outcomes that should matter instead:
did the interpreter who accepted actually show up and complete the job, or
did they no-show / cancel late? That history is turned into a `points`
score per interpreter, which `scheduler.py` uses as a tie-break signal
ahead of cost (see `_candidate_sort_key`).

Honest scope note: this app doesn't (yet) have a real multi-interpreter
"broadcast a job, first to accept wins" auction UI, so there's nothing to
measure "response time" against yet. The event schema has a
`response_seconds` field ready for that once the invitation flow exists
(see README "what I'd build next"), but for now it's always recorded as
`None` and contributes nothing to the score. What IS real and wired up:
- an ACCEPTED event is logged when a planner confirms a manual assignment
- COMPLETED / NO_SHOW / LATE_CANCELLATION events are logged when a planner
  records the outcome of a job on the job detail page

Storage is a single SQLite file (`reliability.db`, auto-created) — no ORM,
no migrations framework, deliberately minimal for an MVP. Every function
takes an explicit `db_path` so tests can point at a temp file instead of
the real ledger.

Design note on "only matters when there's a real choice": this module does
not special-case the single-candidate case itself — it just reports a
score. `scheduler.py` only ever compares scores across the interpreters
who are actually eligible for a given job, so when there's exactly one
eligible candidate the score can't change the outcome; when there's only
ever been one candidate, nothing here forces the data to be discarded
either, so it keeps accumulating for the day multiple candidates exist.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "reliability.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reliability_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interpreter_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    response_seconds REAL
)
"""


class EventType(str, Enum):
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    NO_SHOW = "no_show"
    LATE_CANCELLATION = "late_cancellation"
    DECLINED = "declined"


# Points per outcome. Deliberately simple and easy to argue with — tune
# once there's real operational data on what a no-show actually costs
# relative to a completed job.
_POINTS: dict[EventType, float] = {
    EventType.ACCEPTED: 0.0,  # saying yes is neutral; the outcome is what counts
    EventType.COMPLETED: 3.0,
    EventType.NO_SHOW: -6.0,
    EventType.LATE_CANCELLATION: -4.0,
    EventType.DECLINED: -1.0,
}

# Bonus for a fast acceptance response, once response_seconds is actually
# populated by a future invitation flow: up to +2 for a near-instant
# accept, tapering to 0 by the 30-minute mark. Inert today (always 0)
# because nothing yet records a response time.
_FAST_RESPONSE_BONUS_CAP = 2.0
_FAST_RESPONSE_WINDOW_SECONDS = 1800.0


@dataclass(frozen=True)
class ReliabilityEvent:
    job_id: str
    event_type: str
    occurred_at: str
    response_seconds: float | None


@dataclass(frozen=True)
class ReliabilityScore:
    points: float
    sample_size: int
    accepted: int
    completed: int
    no_show: int
    late_cancellation: int
    declined: int

    @property
    def has_history(self) -> bool:
        return self.sample_size > 0


@contextlib.contextmanager
def _connection(db_path: Path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_SCHEMA_SQL)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_event(
    interpreter_id: str,
    job_id: str,
    event_type: EventType | str,
    *,
    response_seconds: float | None = None,
    occurred_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    occurred_at = occurred_at or datetime.now(timezone.utc).isoformat()
    with _connection(db_path if db_path is not None else DEFAULT_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO reliability_events "
            "(interpreter_id, job_id, event_type, occurred_at, response_seconds) VALUES (?, ?, ?, ?, ?)",
            (interpreter_id, job_id, EventType(event_type).value, occurred_at, response_seconds),
        )


def events_for(interpreter_id: str, db_path: Path | None = None) -> list[ReliabilityEvent]:
    with _connection(db_path if db_path is not None else DEFAULT_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT job_id, event_type, occurred_at, response_seconds FROM reliability_events "
            "WHERE interpreter_id = ? ORDER BY occurred_at DESC",
            (interpreter_id,),
        ).fetchall()
    return [ReliabilityEvent(*row) for row in rows]


def score(interpreter_id: str, db_path: Path | None = None) -> ReliabilityScore:
    events = events_for(interpreter_id, db_path=db_path)
    counts = Counter(e.event_type for e in events)
    points = sum(_POINTS.get(EventType(e.event_type), 0.0) for e in events)

    response_times = [
        e.response_seconds
        for e in events
        if e.event_type == EventType.ACCEPTED.value and e.response_seconds is not None
    ]
    if response_times:
        avg_response = sum(response_times) / len(response_times)
        points += max(0.0, _FAST_RESPONSE_BONUS_CAP - avg_response / _FAST_RESPONSE_WINDOW_SECONDS)

    return ReliabilityScore(
        points=points,
        sample_size=len(events),
        accepted=counts.get(EventType.ACCEPTED.value, 0),
        completed=counts.get(EventType.COMPLETED.value, 0),
        no_show=counts.get(EventType.NO_SHOW.value, 0),
        late_cancellation=counts.get(EventType.LATE_CANCELLATION.value, 0),
        declined=counts.get(EventType.DECLINED.value, 0),
    )


def points_for(interpreter_id: str, db_path: Path | None = None) -> float:
    """Convenience for callers (the scheduler) that only need the number."""
    return score(interpreter_id, db_path=db_path).points
