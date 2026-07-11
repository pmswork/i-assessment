"""app.db — the single SQLite database for this application.

**Schema design rationale** (jobs / interpreters / interpreter_availability
/ assignments / unassigned_reasons / reliability_events / settings — see
`_SCHEMA_SQL` below):

- Runtime reads and writes for scheduling logic go through `PlanningStore`
  — a plain Python dict, which is faster than any database for this MVP's
  actual scale (8 interpreters, ~100 jobs). This module is *not* the hot
  path; it exists purely for durability, so admin edits, CSV imports, and
  manual assignments survive a restart instead of silently resetting to
  the CSVs every time the server starts.
- `PlanningStore` writes through to these tables via `sync_store()` after
  a batch of mutations (see `store.persist_now()`), not on every
  individual change — a full resync (delete-all, reinsert-current-state,
  one transaction) is simple to reason about and correct at this data
  volume; it would not scale to a real multi-tenant deployment, which
  would want targeted upserts instead. Flagged as a known trade-off, not
  hidden.
- `reliability_events` (see `reliability.py`) is the one table that IS
  read on the scheduler's hot path (every candidate ranking), so it has
  its own index on `interpreter_id`. It's kept in this same file for one
  coherent database, but is otherwise a self-contained module.
- `settings` (see `settings.py`) is a tiny key/value table, one row per
  threshold, read once at startup and on every settings-tab save.

**Speed**: indexes on the columns actually filtered/joined on
(`language`, `date`, assignment `interpreter_id`). At tens to low hundreds
of rows every query here is sub-millisecond with or without an index —
they cost nothing to maintain and make the intended access patterns
explicit for whoever scales this up later.

**Security**, for what a local, single-user, no-auth MVP can reasonably
claim: every query is parameterized (no string-built SQL — no SQL
injection surface), foreign keys and CHECK constraints enforce basic
integrity at the database layer rather than trusting Python call sites,
and CSV import validates every row before any write and commits
atomically (all rows or none — see `csv_io.py`). What this deliberately
does NOT provide — authentication, authorization, encryption at rest,
audit logging — mirrors the original brief's explicit instruction to skip
auth/production hardening for this assessment; see README "known
limitations" for what a production version would add.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

from . import settings as settings_module
from .models import BlacklistEntry, Interpreter, Job

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "app.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interpreters (
    interpreter_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language TEXT NOT NULL,
    sworn INTEGER NOT NULL CHECK (sworn IN (0, 1)),
    home_city TEXT NOT NULL,
    home_lat REAL NOT NULL,
    home_lon REAL NOT NULL,
    rate_eur_per_hour REAL NOT NULL CHECK (rate_eur_per_hour > 0)
);
CREATE INDEX IF NOT EXISTS idx_interpreters_language ON interpreters(language);

CREATE TABLE IF NOT EXISTS interpreter_availability (
    interpreter_id TEXT NOT NULL REFERENCES interpreters(interpreter_id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    PRIMARY KEY (interpreter_id, day)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    duration_min INTEGER NOT NULL CHECK (duration_min > 0),
    end_time TEXT NOT NULL,
    language TEXT NOT NULL,
    sworn_required INTEGER NOT NULL CHECK (sworn_required IN (0, 1)),
    modality TEXT NOT NULL CHECK (modality IN ('on-site', 'remote')),
    client TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_date ON jobs(date);
CREATE INDEX IF NOT EXISTS idx_jobs_language ON jobs(language);

CREATE TABLE IF NOT EXISTS assignments (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    interpreter_id TEXT NOT NULL REFERENCES interpreters(interpreter_id) ON DELETE CASCADE,
    source TEXT NOT NULL CHECK (source IN ('auto', 'manual'))
);
CREATE INDEX IF NOT EXISTS idx_assignments_interpreter ON assignments(interpreter_id);

CREATE TABLE IF NOT EXISTS unassigned_reasons (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
    reasons_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist_entries (
    interpreter_id TEXT NOT NULL REFERENCES interpreters(interpreter_id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('global', 'client')),
    client TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (interpreter_id, scope, client)
);
CREATE INDEX IF NOT EXISTS idx_blacklist_interpreter ON blacklist_entries(interpreter_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_client ON blacklist_entries(client);

CREATE TABLE IF NOT EXISTS reliability_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interpreter_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    response_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_reliability_interpreter ON reliability_events(interpreter_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);
"""


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


@contextlib.contextmanager
def _connection(db_path: Path | None):
    # Resolved here, at call time, rather than bound as a default parameter
    # value on every public function below — Python evaluates default
    # argument values once, at function-definition time, so a default of
    # `db_path: Path = DEFAULT_DB_PATH` would freeze in whatever
    # DEFAULT_DB_PATH was at import time and silently ignore any later
    # monkeypatch (e.g. tests/conftest.py isolating each test's database).
    conn = sqlite3.connect(db_path if db_path is not None else DEFAULT_DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(_SCHEMA_SQL)
        yield conn
        conn.commit()
    finally:
        conn.close()


def has_data(db_path: Path | None = None) -> bool:
    """True once anything has been persisted — used at startup to decide
    "load from the database" vs. "this is a first run, seed from the
    CSVs"."""
    with _connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    return row[0] > 0


def load_interpreters(db_path: Path | None = None) -> list[Interpreter]:
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT interpreter_id, name, language, sworn, home_city, home_lat, home_lon, "
            "rate_eur_per_hour FROM interpreters"
        ).fetchall()
        avail_rows = conn.execute(
            "SELECT interpreter_id, day, window_start, window_end FROM interpreter_availability"
        ).fetchall()

    availability: dict[str, dict] = {}
    for interpreter_id, day, start, end in avail_rows:
        availability.setdefault(interpreter_id, {})[_parse_date(day)] = (_parse_time(start), _parse_time(end))

    return [
        Interpreter(
            interpreter_id=r[0],
            name=r[1],
            language=r[2],
            sworn=bool(r[3]),
            home_city=r[4],
            home_lat=r[5],
            home_lon=r[6],
            rate_eur_per_hour=r[7],
            availability=availability.get(r[0], {}),
        )
        for r in rows
    ]


def load_jobs(db_path: Path | None = None) -> list[Job]:
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT job_id, date, start_time, duration_min, end_time, language, sworn_required, "
            "modality, client, address, city, lat, lon FROM jobs"
        ).fetchall()
    return [
        Job(
            job_id=r[0],
            date=_parse_date(r[1]),
            start_time=_parse_time(r[2]),
            duration_min=r[3],
            end_time=_parse_time(r[4]),
            language=r[5],
            sworn_required=bool(r[6]),
            modality=r[7],
            client=r[8],
            address=r[9],
            city=r[10],
            lat=r[11],
            lon=r[12],
        )
        for r in rows
    ]


def load_assignments(
    db_path: Path | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    """Returns (assignments, assignment_source, unassigned_reasons) —
    matching the three `PlanningStore` attributes they populate."""
    with _connection(db_path) as conn:
        assignment_rows = conn.execute("SELECT job_id, interpreter_id, source FROM assignments").fetchall()
        reason_rows = conn.execute("SELECT job_id, reasons_json FROM unassigned_reasons").fetchall()

    assignments = {job_id: interpreter_id for job_id, interpreter_id, _source in assignment_rows}
    sources = {job_id: source for job_id, _interpreter_id, source in assignment_rows}
    reasons = {job_id: json.loads(reasons_json) for job_id, reasons_json in reason_rows}
    return assignments, sources, reasons


def load_blacklist_entries(db_path: Path | None = None) -> list[BlacklistEntry]:
    with _connection(db_path) as conn:
        rows = conn.execute(
            "SELECT interpreter_id, scope, client, reason FROM blacklist_entries "
            "ORDER BY interpreter_id, scope, client"
        ).fetchall()
    return [
        BlacklistEntry(interpreter_id=r[0], scope=r[1], client=r[2], reason=r[3])
        for r in rows
    ]


def sync_store(store, db_path: Path | None = None) -> None:
    """Persist a PlanningStore's full current state — jobs, interpreters,
    assignments, unassigned reasons — in one transaction.

    A full delete-and-reinsert rather than targeted upserts: simple to
    reason about and correct (no partial-update bugs), and cheap enough at
    this MVP's data volume. Called explicitly via `store.persist_now()`
    after a batch of mutations, not on every individual change — e.g. once
    at the end of `run_auto_assignment`, not once per job.
    """
    with _connection(db_path) as conn:
        conn.execute("DELETE FROM interpreter_availability")
        conn.execute("DELETE FROM blacklist_entries")
        conn.execute("DELETE FROM interpreters")
        conn.execute("DELETE FROM jobs")  # cascades assignments + unassigned_reasons

        interpreters = list(store.interpreters.values())
        conn.executemany(
            "INSERT INTO interpreters "
            "(interpreter_id, name, language, sworn, home_city, home_lat, home_lon, rate_eur_per_hour) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (i.interpreter_id, i.name, i.language, int(i.sworn), i.home_city, i.home_lat, i.home_lon,
                 i.rate_eur_per_hour)
                for i in interpreters
            ],
        )
        conn.executemany(
            "INSERT INTO interpreter_availability (interpreter_id, day, window_start, window_end) "
            "VALUES (?, ?, ?, ?)",
            [
                (i.interpreter_id, day.isoformat(), window[0].strftime("%H:%M"), window[1].strftime("%H:%M"))
                for i in interpreters
                for day, window in i.availability.items()
            ],
        )

        jobs = list(store.jobs.values())
        conn.executemany(
            "INSERT INTO jobs "
            "(job_id, date, start_time, duration_min, end_time, language, sworn_required, modality, "
            "client, address, city, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (j.job_id, j.date.isoformat(), j.start_time.strftime("%H:%M"), j.duration_min,
                 j.end_time.strftime("%H:%M"), j.language, int(j.sworn_required), j.modality, j.client,
                 j.address, j.city, j.lat, j.lon)
                for j in jobs
            ],
        )

        conn.executemany(
            "INSERT INTO assignments (job_id, interpreter_id, source) VALUES (?, ?, ?)",
            [
                (job_id, interpreter_id, store.assignment_source.get(job_id, "manual"))
                for job_id, interpreter_id in store.assignments.items()
            ],
        )
        conn.executemany(
            "INSERT INTO unassigned_reasons (job_id, reasons_json) VALUES (?, ?)",
            [(job_id, json.dumps(reasons)) for job_id, reasons in store.unassigned_reasons.items()],
        )
        conn.executemany(
            "INSERT INTO blacklist_entries (interpreter_id, scope, client, reason) VALUES (?, ?, ?, ?)",
            [
                (entry.interpreter_id, entry.scope, entry.client, entry.reason)
                for entry in store.blacklist_entries
                if entry.interpreter_id in store.interpreters
            ],
        )


def save_settings(current: settings_module.Settings, db_path: Path | None = None) -> None:
    with _connection(db_path) as conn:
        conn.execute("DELETE FROM settings")
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            [(name, getattr(current, name)) for name in settings_module.field_names()],
        )


def load_settings(db_path: Path | None = None) -> settings_module.Settings | None:
    """None if the settings table is empty or incomplete — caller should
    fall back to `Settings()` defaults rather than guess at missing
    fields."""
    with _connection(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()

    values = dict(rows)
    field_names = settings_module.field_names()
    if not all(name in values for name in field_names):
        return None

    kwargs = dict(values)
    kwargs["coverage_bar_cap"] = int(kwargs["coverage_bar_cap"])
    return settings_module.Settings(**kwargs)
