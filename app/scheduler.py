"""Deterministic greedy auto-assignment.

Strategy (see README "assignment strategy" for the full rationale):

1. Order jobs by scarcity of the resource they need, most scarce first:
   sworn jobs before non-sworn, then jobs with fewer statically-eligible
   interpreters, then chronologically, then job_id for a stable order.
2. For each job, evaluate every interpreter through the exact same
   `rules.validate_assignment` used by the manual-assignment endpoint, so
   auto- and manual assignment can never disagree.
3. Among interpreters who are ACCEPTED or WARNING (never REJECTED), pick
   the best by: prefer no warnings, then higher reliability score (see
   `reliability.py` — track record beats being first to say yes), then
   cheapest rate, then least travel added (minutes, then km as a tie-break
   between otherwise-equal legs), then lightest current workload, then
   interpreter_id (tie-break for determinism). Distance and reliability are
   only ever tie-break signals here — neither overrides a hard constraint
   (rule 2 already dropped anyone REJECTED).
4. If nobody is eligible, leave the job unassigned and record the most
   useful reason (see `_explain_unassigned`), including a note when the
   job has zero qualified interpreters within the coverage radius (see
   `coverage.py`) so a planner knows travel distance, not scheduling, is
   the root problem.

This is a single greedy pass, not a global optimum — see README for the
trade-off discussion.
"""

from __future__ import annotations

from . import settings
from .coverage import coverage_stats
from .models import Interpreter, Job
from .reliability import points_for as reliability_points
from .rules import (
    ValidationResult,
    ValidationStatus,
    check_hard_constraints_with_blacklist,
    is_qualified,
    total_added_travel_km,
    total_added_travel_minutes,
    validate_assignment,
)
from .store import PLANNER_OWNED_SOURCES, PlanningStore


def _job_sort_key(job: Job, interpreters: list[Interpreter]):
    eligible_count = sum(1 for i in interpreters if is_qualified(job, i))
    return (
        0 if job.sworn_required else 1,
        eligible_count,
        job.date,
        job.start_time,
        job.job_id,
    )


def _candidate_sort_key(job: Job, interpreter: Interpreter, status: ValidationStatus, store: PlanningStore):
    schedule = store.schedule_for(interpreter.interpreter_id)
    added_travel_min = total_added_travel_minutes(job, interpreter, schedule)
    added_travel_km = total_added_travel_km(job, interpreter, schedule)
    workload = store.workload_minutes(interpreter.interpreter_id)
    return (
        0 if status == ValidationStatus.ACCEPTED else 1,
        -reliability_points(interpreter.interpreter_id),  # higher score ranks first
        interpreter.rate_eur_per_hour,
        added_travel_min,
        added_travel_km,
        workload,
        interpreter.interpreter_id,
    )


def _is_interpreter_choice_warning(reason: str) -> bool:
    lower = reason.lower()
    return lower.startswith("tight commute") or "long one-way distance" in lower


def _auto_assignment_allows(result: ValidationResult) -> bool:
    if result.status == ValidationStatus.ACCEPTED:
        return True
    if result.status == ValidationStatus.REJECTED:
        return False

    risk_level = settings.get().auto_assign_risk_level
    if risk_level <= 0:
        return False
    if risk_level >= 2:
        return True
    return bool(result.reasons) and all(_is_interpreter_choice_warning(reason) for reason in result.reasons)


def _warning_needs_decision(job: Job, interpreter: Interpreter, result: ValidationResult) -> list[str]:
    return [
        f"{interpreter.name} is possible for {job.job_id}, but auto-assignment autonomy requires planner review: "
        f"{reason}"
        for reason in result.reasons
    ] or [f"{interpreter.name} is possible for {job.job_id}, but needs planner review before assignment."]


def _explain_unassigned(job: Job, store: PlanningStore) -> list[str]:
    """Why couldn't this job be placed? Named interpreters, named numbers —
    never a generic "unavailable" summary.

    The roster-wide gaps (nobody speaks the language at all; nobody sworn
    exists for it) get their own one-line explanation, since there's no
    per-interpreter detail to add. For everyone else who's at least
    language/sworn-qualified, `check_hard_constraints` already computes the
    exact, specific reason(s) they were rejected (which job they overlap,
    or exactly how many minutes short the travel time was) — this just
    collects those instead of collapsing them into one vague sentence, so a
    planner sees precisely who was considered and precisely why each one
    didn't work.
    """
    all_interpreters = list(store.interpreters.values())

    language_matches = [i for i in all_interpreters if i.language == job.language]
    if not language_matches:
        return [f"No interpreter on the roster supports {job.language}."]

    if job.sworn_required:
        sworn_pool = [i for i in language_matches if i.sworn]
        if not sworn_pool:
            return [
                f"This job requires a sworn {job.language} interpreter, but no sworn "
                f"{job.language} interpreter exists on the roster."
            ]
    else:
        sworn_pool = language_matches

    reasons: list[str] = []
    for interpreter in sworn_pool:
        reasons.extend(
            check_hard_constraints_with_blacklist(
                job,
                interpreter,
                store.schedule_for(interpreter.interpreter_id),
                blacklist_lookup=store,
            )
        )

    # Should not happen: if every qualified interpreter had zero hard-
    # constraint violations, validate_assignment should have found at
    # least a WARNING-tier candidate. Kept as a defensive fallback.
    return reasons or ["No interpreter could be matched for this job; needs manual review."]


def _coverage_note(job: Job, store: PlanningStore) -> str | None:
    """Extra context appended to unassigned reasons: when interpreters exist
    for this language/sworn combination but none of them are within the
    coverage radius, travel distance — not scheduling — is the likely root
    cause, which is worth calling out even when the concrete blocking reason
    above is something else (e.g. an overlap)."""
    stats = coverage_stats(job, list(store.interpreters.values()))
    if not stats.is_scarce:
        return None
    return (
        f"No qualified {job.language} interpreter lives within {settings.get().coverage_radius_km:.0f} km "
        f"(straight-line) of this job — all {stats.qualified_total} qualified interpreter(s) on "
        f"the roster are further away, so travel time is a likely contributing factor."
    )


def _explain_unassigned_with_coverage(job: Job, store: PlanningStore) -> list[str]:
    reasons = _explain_unassigned(job, store)
    note = _coverage_note(job, store)
    if note is not None and note not in reasons:
        reasons = [*reasons, note]
    return reasons


def best_candidate_for(job: Job, store: PlanningStore, *, respect_auto_policy: bool = False):
    """The single best ACCEPTED/WARNING candidate for `job` right now, by the
    same ranking `run_auto_assignment` uses — without assigning anything.

    Used to power the "suggested candidate" hint on the job detail page for
    jobs that need a human decision: even when a job isn't auto-assignable
    cleanly, this surfaces the least-bad option (if any) so a planner has a
    starting point instead of an empty dropdown. Returns
    `(Interpreter, ValidationResult)` or `None` if nobody qualifies at all.
    """
    interpreters = list(store.interpreters.values())
    best: tuple[Interpreter, ValidationResult] | None = None
    best_key = None
    for interpreter in interpreters:
        if not is_qualified(job, interpreter):
            continue
        schedule = store.schedule_for(interpreter.interpreter_id, exclude_job_id=job.job_id)
        result = validate_assignment(
            job, interpreter, schedule, all_interpreters=interpreters, workload_lookup=store
        )
        if result.status == ValidationStatus.REJECTED:
            continue
        if respect_auto_policy and not _auto_assignment_allows(result):
            continue
        key = _candidate_sort_key(job, interpreter, result.status, store)
        if best_key is None or key < best_key:
            best_key = key
            best = (interpreter, result)
    return best


def _manual_assignment_rejection(job: Job, interpreter: Interpreter, result: ValidationResult) -> list[str]:
    reasons = result.reasons or ["Manual assignment failed validation."]
    return [
        f"Confirmed assignment to {interpreter.name} was not overwritten by auto-assignment, "
        f"but it is no longer valid: {reason}"
        for reason in reasons
    ]


def run_auto_assignment(store: PlanningStore, *, preserve_manual: bool = True) -> None:
    """Populate `store.assignments` / `store.unassigned_reasons` from
    scratch. Deterministic given the same jobs and interpreters.

    Persists once at the end (if `store.persist` is enabled), not per job —
    ~100 in-memory writes followed by a single durable sync, not 100.
    """
    if preserve_manual:
        store.reset_auto_assignments()
    else:
        store.reset_assignments()

    interpreters = list(store.interpreters.values())
    invalid_manual_job_ids: set[str] = set()

    if preserve_manual:
        for job_id, interpreter_id in list(store.assignments.items()):
            if store.assignment_source.get(job_id) not in PLANNER_OWNED_SOURCES:
                continue
            job = store.jobs.get(job_id)
            interpreter = store.interpreters.get(interpreter_id)
            if job is None or interpreter is None:
                store.unassign(job_id)
                continue
            result = validate_assignment(
                job,
                interpreter,
                store.schedule_for(interpreter_id, exclude_job_id=job_id),
                all_interpreters=interpreters,
                workload_lookup=store,
            )
            if result.status == ValidationStatus.REJECTED:
                store.mark_unassigned(job_id, _manual_assignment_rejection(job, interpreter, result))
                invalid_manual_job_ids.add(job_id)

    jobs = sorted(store.jobs.values(), key=lambda j: _job_sort_key(j, interpreters))

    for job in jobs:
        if job.job_id in invalid_manual_job_ids:
            continue
        if store.assignment_source.get(job.job_id) in PLANNER_OWNED_SOURCES:
            continue
        best = best_candidate_for(job, store, respect_auto_policy=True)
        if best is None:
            review_candidate = best_candidate_for(job, store)
            if review_candidate is not None and review_candidate[1].status == ValidationStatus.WARNING:
                interpreter, result = review_candidate
                store.mark_unassigned(job.job_id, _warning_needs_decision(job, interpreter, result))
            else:
                store.mark_unassigned(job.job_id, _explain_unassigned_with_coverage(job, store))
        else:
            interpreter, _result = best
            store.assign(job.job_id, interpreter.interpreter_id, source="auto")

    store.persist_now()
