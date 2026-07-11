"""In-memory planning state: the roster, the jobs, and the current
job -> interpreter assignments.

This stays a plain-dict, in-memory store on purpose — it's the fastest
possible representation for this MVP's scale (8 interpreters, ~100 jobs)
and every scheduling/validation function in the app is written against it
directly. Durability is layered on top, not built in: pass
`persist=True` and every mutation *can* be written through to SQLite (see
`db.py`) by calling `persist_now()` explicitly — it's opt-in and explicit
(not automatic on every mutation) so a bulk operation like
`run_auto_assignment` can make ~100 in-memory changes and write them to
disk once, not 100 times. Existing/test code that constructs
`PlanningStore(jobs, interpreters)` without `persist=True` behaves exactly
as before: pure in-memory, no I/O.

Deliberately `persist: bool` rather than an explicit `db_path`: this store
never resolves *which* database file to use — it always delegates that to
`db.sync_store()`'s own default (`db.DEFAULT_DB_PATH`, resolved fresh on
every call). If this store instead captured a concrete path once at
construction time, a test that monkeypatches `db.DEFAULT_DB_PATH` for
isolation (see tests/conftest.py) would have no effect on a store built
before the monkeypatch applied — exactly the situation `app.main`'s
module-level store is in during tests. Always re-resolving avoids that
whole class of bug.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import db as db_module
from .models import BlacklistEntry, Interpreter, Job


@dataclass
class UnassignedInfo:
    job_id: str
    reasons: list[str]


class PlanningStore:
    def __init__(self, jobs: list[Job], interpreters: list[Interpreter], persist: bool = False):
        self.jobs: dict[str, Job] = {j.job_id: j for j in jobs}
        self.interpreters: dict[str, Interpreter] = {i.interpreter_id: i for i in interpreters}
        self.blacklist_entries: list[BlacklistEntry] = []
        self.assignments: dict[str, str] = {}  # job_id -> interpreter_id
        self.assignment_source: dict[str, str] = {}  # job_id -> "auto" | "manual"
        self.unassigned_reasons: dict[str, list[str]] = {}
        self.persist = persist

    # -- persistence -----------------------------------------------------------

    def persist_now(self) -> None:
        """Write the current full state to SQLite (db.DEFAULT_DB_PATH,
        resolved at call time). A no-op unless this store was constructed
        with `persist=True` (the default is off — see module docstring)."""
        if self.persist:
            db_module.sync_store(self)

    # -- queries used by rules.py (workload_lookup protocol) ---------------

    def schedule_for(self, interpreter_id: str, exclude_job_id: str | None = None) -> list[Job]:
        return [
            self.jobs[job_id]
            for job_id, iid in self.assignments.items()
            if iid == interpreter_id and job_id != exclude_job_id
        ]

    def workload_minutes(self, interpreter_id: str, exclude_job_id: str | None = None) -> int:
        return sum(j.duration_min for j in self.schedule_for(interpreter_id, exclude_job_id))

    # -- blacklist -------------------------------------------------------------

    @staticmethod
    def normalize_client(client: str) -> str:
        return " ".join(client.casefold().split())

    def blacklist_for(self, interpreter_id: str) -> list[BlacklistEntry]:
        return [entry for entry in self.blacklist_entries if entry.interpreter_id == interpreter_id]

    def blacklist_reasons(self, interpreter_id: str, client: str) -> list[str]:
        normalized_client = self.normalize_client(client)
        reasons: list[str] = []
        for entry in self.blacklist_for(interpreter_id):
            if entry.is_global:
                reasons.append(entry.reason or "Interpreter is blacklisted for all clients.")
            elif self.normalize_client(entry.client) == normalized_client:
                reasons.append(entry.reason or f"Interpreter is blacklisted for client {entry.client}.")
        return reasons

    def is_blacklisted(self, interpreter_id: str, client: str) -> bool:
        return bool(self.blacklist_reasons(interpreter_id, client))

    def add_blacklist_entry(self, entry: BlacklistEntry) -> None:
        client = entry.client.strip() if entry.scope == "client" else ""
        normalized_client = self.normalize_client(client)
        kept = []
        for existing in self.blacklist_entries:
            same_interpreter = existing.interpreter_id == entry.interpreter_id
            same_scope = existing.scope == entry.scope
            same_client = self.normalize_client(existing.client) == normalized_client
            if same_interpreter and same_scope and (entry.scope == "global" or same_client):
                continue
            kept.append(existing)
        kept.append(
            BlacklistEntry(
                interpreter_id=entry.interpreter_id,
                scope=entry.scope,
                client=client,
                reason=entry.reason.strip(),
            )
        )
        self.blacklist_entries = kept
        self._clear_blacklisted_assignments(entry.interpreter_id)

    def delete_blacklist_entry(self, interpreter_id: str, scope: str, client: str = "") -> None:
        normalized_client = self.normalize_client(client)
        self.blacklist_entries = [
            entry
            for entry in self.blacklist_entries
            if not (
                entry.interpreter_id == interpreter_id
                and entry.scope == scope
                and (scope == "global" or self.normalize_client(entry.client) == normalized_client)
            )
        ]

    def _clear_blacklisted_assignments(self, interpreter_id: str) -> None:
        for job_id, assigned_interpreter_id in list(self.assignments.items()):
            job = self.jobs.get(job_id)
            if assigned_interpreter_id == interpreter_id and job is not None and self.is_blacklisted(interpreter_id, job.client):
                self.unassign(job_id)

    # -- assignment mutation -----------------------------------------------------

    def assign(self, job_id: str, interpreter_id: str, source: str = "manual") -> None:
        self.assignments[job_id] = interpreter_id
        self.assignment_source[job_id] = source
        self.unassigned_reasons.pop(job_id, None)

    def unassign(self, job_id: str) -> None:
        self.assignments.pop(job_id, None)
        self.assignment_source.pop(job_id, None)

    def mark_unassigned(self, job_id: str, reasons: list[str]) -> None:
        self.assignments.pop(job_id, None)
        self.assignment_source.pop(job_id, None)
        self.unassigned_reasons[job_id] = reasons

    def reset_assignments(self) -> None:
        self.assignments.clear()
        self.assignment_source.clear()
        self.unassigned_reasons.clear()

    def reset_auto_assignments(self) -> None:
        """Clear system-generated assignments while keeping planner choices."""
        auto_jobs = [job_id for job_id, source in self.assignment_source.items() if source == "auto"]
        for job_id in auto_jobs:
            self.assignments.pop(job_id, None)
            self.assignment_source.pop(job_id, None)
        self.unassigned_reasons.clear()

    # -- roster/job mutation (admin) ---------------------------------------------

    def upsert_job(self, job: Job) -> None:
        """Add or replace a job. On replace, drops any existing assignment
        for it — the old assignment may no longer be valid against the new
        job data (different time/language/etc.), so re-assigning is a
        deliberate decision, not something to carry over silently."""
        self.jobs[job.job_id] = job
        self.assignments.pop(job.job_id, None)
        self.assignment_source.pop(job.job_id, None)
        self.unassigned_reasons.pop(job.job_id, None)

    def delete_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        self.assignments.pop(job_id, None)
        self.assignment_source.pop(job_id, None)
        self.unassigned_reasons.pop(job_id, None)

    def upsert_interpreter(self, interpreter: Interpreter) -> None:
        self.interpreters[interpreter.interpreter_id] = interpreter

    def delete_interpreter(self, interpreter_id: str) -> None:
        """Removing an interpreter also frees whatever they were assigned
        to — those jobs go back to needing a decision (no stale reason is
        recorded; the next auto-assignment run or manual check will
        produce a current one)."""
        self.interpreters.pop(interpreter_id, None)
        affected_jobs = [job_id for job_id, iid in self.assignments.items() if iid == interpreter_id]
        for job_id in affected_jobs:
            self.assignments.pop(job_id, None)
            self.assignment_source.pop(job_id, None)
        self.blacklist_entries = [
            entry for entry in self.blacklist_entries if entry.interpreter_id != interpreter_id
        ]

    def replace_jobs(self, jobs: list[Job]) -> None:
        """CSV import of jobs.csv: replace the whole job list, dropping
        assignments/reasons for job_ids that no longer exist. Assignments
        for job_ids that still exist are left alone — the caller should
        re-run auto-assignment (or check manually) afterwards to catch
        jobs whose *data* changed enough to invalidate their assignment."""
        self.jobs = {j.job_id: j for j in jobs}
        stale = [job_id for job_id in self.assignments if job_id not in self.jobs]
        for job_id in stale:
            self.assignments.pop(job_id, None)
            self.assignment_source.pop(job_id, None)
        self.unassigned_reasons = {
            job_id: reasons for job_id, reasons in self.unassigned_reasons.items() if job_id in self.jobs
        }
        for interpreter_id in list(self.interpreters):
            self._clear_blacklisted_assignments(interpreter_id)

    def replace_interpreters(self, interpreters: list[Interpreter]) -> None:
        """CSV import of interpreters.csv: replace the whole roster,
        dropping assignments that reference an interpreter_id that no
        longer exists."""
        self.interpreters = {i.interpreter_id: i for i in interpreters}
        stale = [job_id for job_id, iid in self.assignments.items() if iid not in self.interpreters]
        for job_id in stale:
            self.assignments.pop(job_id, None)
            self.assignment_source.pop(job_id, None)
        self.blacklist_entries = [
            entry for entry in self.blacklist_entries if entry.interpreter_id in self.interpreters
        ]

    # -- read helpers for the UI ---------------------------------------------

    def jobs_sorted(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: (j.date, j.start_time, j.job_id))

    def interpreters_sorted(self) -> list[Interpreter]:
        return sorted(self.interpreters.values(), key=lambda i: i.interpreter_id)

    def assigned_interpreter(self, job_id: str) -> Interpreter | None:
        iid = self.assignments.get(job_id)
        return self.interpreters.get(iid) if iid else None
