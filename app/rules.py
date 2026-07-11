"""Business rules for assigning interpreters to jobs.

This module is the single source of truth for what makes an assignment
valid. Both the auto-assignment scheduler (`scheduler.py`) and the manual
assignment endpoint in `main.py` call `validate_assignment` so the two
flows can never disagree about what is allowed.

Hard constraints (`check_hard_constraints`) make an assignment impossible
and always REJECT it:
  - language mismatch
  - sworn required but interpreter not sworn
  - job falls outside the interpreter's availability window for that date
  - the interpreter already has an overlapping job
  - travel between two of the interpreter's on-site jobs that day cannot
    physically fit in the gap between them

Soft constraints (`check_warnings`) never block anything by themselves;
they WARN so a planner can make an informed override:
  - travel between on-site jobs is technically possible but tight
  - a remote job is wedged into a gap that's mostly needed for travel
    between two on-site jobs
  - the commute from/to home for the first/last on-site job of the day is
    tight or does not fit inside the stated working window (see README:
    we treat this as soft, not hard, because "availability" describes
    willingness to work, not a constraint on when someone may leave home)
  - a cheaper qualified, available interpreter exists for this job
  - assigning this job would leave the interpreter's workload far above
    other qualified, available interpreters
  - long one-way travel distance from home to an on-site job
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from . import settings
from .models import Interpreter, Job
from .travel import haversine_km, travel_minutes_for_distance


class ValidationStatus(str, Enum):
    ACCEPTED = "accepted"
    WARNING = "warning"
    REJECTED = "rejected"


@dataclass
class ValidationResult:
    status: ValidationStatus
    reasons: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status == ValidationStatus.REJECTED


def _overlaps(a: Job, b: Job) -> bool:
    return a.start_dt < b.end_dt and b.start_dt < a.end_dt


def check_hard_constraints(job: Job, interpreter: Interpreter, schedule: list[Job]) -> list[str]:
    """Return every hard-constraint violation. Empty list means feasible."""
    return check_hard_constraints_with_blacklist(job, interpreter, schedule)


def check_hard_constraints_with_blacklist(
    job: Job,
    interpreter: Interpreter,
    schedule: list[Job],
    *,
    blacklist_lookup=None,
) -> list[str]:
    """Return every hard-constraint violation. Empty list means feasible."""
    reasons: list[str] = []

    if interpreter.language != job.language:
        reasons.append(
            f"{interpreter.name} interprets {interpreter.language}, but this job needs {job.language}."
        )

    if job.sworn_required and not interpreter.sworn:
        reasons.append(
            f"This job requires a sworn interpreter and {interpreter.name} is not sworn."
        )

    if blacklist_lookup is not None and blacklist_lookup.is_blacklisted(interpreter.interpreter_id, job.client):
        blacklist_reasons = blacklist_lookup.blacklist_reasons(interpreter.interpreter_id, job.client)
        detail = "; ".join(blacklist_reasons) if blacklist_reasons else "blacklisted for this client"
        reasons.append(f"{interpreter.name} cannot be assigned to {job.client}: {detail}.")

    window = interpreter.availability_on(job.date)
    if window is None:
        reasons.append(f"{interpreter.name} is not available on {job.date.isoformat()}.")
    elif not (window[0] <= job.start_time and job.end_time <= window[1]):
        reasons.append(
            f"{interpreter.name}'s working hours on {job.date.isoformat()} are "
            f"{window[0].strftime('%H:%M')}–{window[1].strftime('%H:%M')}, which does not "
            f"cover this job's {job.start_time.strftime('%H:%M')}–{job.end_time.strftime('%H:%M')} slot."
        )

    for other in schedule:
        if other.job_id != job.job_id and _overlaps(job, other):
            reasons.append(
                f"{interpreter.name} is already booked on {other.job_id} "
                f"({other.start_time.strftime('%H:%M')}–{other.end_time.strftime('%H:%M')}), "
                f"which overlaps this job."
            )

    # Only worth checking travel feasibility once language/sworn/availability
    # are satisfied and there is no outright double-booking — otherwise the
    # timeline below is not a schedule the interpreter could ever work.
    if not reasons:
        for leg in _onsite_legs(job, interpreter, schedule):
            if leg["gap_min"] < leg["required_min"]:
                reasons.append(
                    f"Not enough travel time for {interpreter.name} between "
                    f"{leg['prev_label']} and {leg['next_label']}: needs ~{round(leg['required_min'])} "
                    f"min, only {round(leg['gap_min'])} min available."
                )

    return reasons


def check_warnings(
    job: Job,
    interpreter: Interpreter,
    schedule: list[Job],
    *,
    all_interpreters: list[Interpreter] | None = None,
    workload_lookup=None,
) -> list[str]:
    """Return soft-constraint notices. Assumes hard constraints already pass."""
    warnings: list[str] = []

    travel_buffer_min = settings.get().travel_buffer_min
    for leg in _onsite_legs(job, interpreter, schedule):
        if not leg["touches_job"]:
            continue
        buffer_min = leg["gap_min"] - leg["required_min"]
        if leg["is_home_leg"]:
            if buffer_min < 0:
                warnings.append(
                    f"Tight commute: reaching {leg['next_label']} needs ~{round(leg['required_min'])} min "
                    f"from home; only {round(leg['gap_min'])} min is available if {interpreter.name} "
                    f"leaves right at the start of their working window."
                )
        else:
            if 0 <= buffer_min < travel_buffer_min:
                warnings.append(
                    f"Tight travel for {interpreter.name} between {leg['prev_label']} and "
                    f"{leg['next_label']}: only {round(buffer_min)} min of buffer beyond the "
                    f"~{round(leg['required_min'])} min drive."
                )
            if leg["remote_between"]:
                ids = ", ".join(r.job_id for r in leg["remote_between"])
                warnings.append(
                    f"Remote job(s) {ids} sit between on-site jobs {leg['prev_label']} and "
                    f"{leg['next_label']} in a gap that's mostly needed for travel — confirm "
                    f"{interpreter.name} can actually take the call while in transit."
                )

    trailing = _trailing_home_leg(job, interpreter, schedule)
    if trailing is not None and trailing["buffer_min"] < 0:
        warnings.append(
            f"Tight commute home: after {trailing['last_label']}, getting home needs "
            f"~{round(trailing['required_min'])} min, which runs past the end of "
            f"{interpreter.name}'s working window ({trailing['window_end']})."
        )

    if job.is_on_site and job.location is not None:
        home_distance_km = _home_distance_km(interpreter, job)
        if home_distance_km > settings.get().long_distance_km:
            warnings.append(
                f"Long one-way distance from {interpreter.name}'s home in {interpreter.home_city} "
                f"to {job.city}: ~{round(home_distance_km)} km."
            )

    warnings.extend(_court_work_warnings(job, interpreter, schedule))

    if all_interpreters is not None and workload_lookup is not None:
        cheaper = _cheaper_alternative(job, interpreter, all_interpreters, workload_lookup)
        if cheaper is not None:
            warnings.append(
                f"{cheaper.name} is a qualified, available interpreter for this job at "
                f"€{cheaper.rate_eur_per_hour:.0f}/h, cheaper than {interpreter.name} at "
                f"€{interpreter.rate_eur_per_hour:.0f}/h."
            )

        imbalance = _workload_warning(job, interpreter, all_interpreters, workload_lookup)
        if imbalance is not None:
            warnings.append(imbalance)

    return warnings


def total_added_travel_minutes(job: Job, interpreter: Interpreter, schedule: list[Job]) -> float:
    """Sum of estimated travel minutes for the leg(s) that touch `job`, used
    by the scheduler to prefer candidates that add less driving."""
    return sum(leg["required_min"] for leg in _onsite_legs(job, interpreter, schedule) if leg["touches_job"])


def total_added_travel_km(job: Job, interpreter: Interpreter, schedule: list[Job]) -> float:
    """Sum of straight-line km for the leg(s) that touch `job` — a secondary,
    distance-based tie-break alongside `total_added_travel_minutes` (the two
    usually agree, but minutes include a fixed per-leg overhead that distance
    alone doesn't)."""
    return sum(leg["distance_km"] for leg in _onsite_legs(job, interpreter, schedule) if leg["touches_job"])


def validate_assignment(
    job: Job,
    interpreter: Interpreter,
    schedule: list[Job],
    *,
    all_interpreters: list[Interpreter] | None = None,
    workload_lookup=None,
    blacklist_lookup=None,
) -> ValidationResult:
    """Validate assigning `interpreter` to `job`.

    `schedule` must be the interpreter's OTHER current assignments (i.e.
    excluding `job` itself, even if it happens to already be assigned to
    them) so re-validating an existing assignment works the same way as
    validating a brand new one.
    """
    if blacklist_lookup is None:
        blacklist_lookup = workload_lookup
    hard_reasons = check_hard_constraints_with_blacklist(
        job, interpreter, schedule, blacklist_lookup=blacklist_lookup
    )
    if hard_reasons:
        return ValidationResult(ValidationStatus.REJECTED, hard_reasons)

    warnings = check_warnings(
        job, interpreter, schedule, all_interpreters=all_interpreters, workload_lookup=workload_lookup
    )
    if warnings:
        return ValidationResult(ValidationStatus.WARNING, warnings)

    return ValidationResult(ValidationStatus.ACCEPTED, [])


# ---------------------------------------------------------------------------
# Timeline / travel helpers
# ---------------------------------------------------------------------------


def _home_coord(interpreter: Interpreter) -> tuple[float, float]:
    return (interpreter.home_lat, interpreter.home_lon)


def _home_distance_km(interpreter: Interpreter, job: Job) -> float:
    return haversine_km(_home_coord(interpreter), job.location)


def _label(job: Job) -> str:
    place = job.city if job.is_on_site else "remote"
    return f"{job.job_id} ({place}, {job.start_time.strftime('%H:%M')}–{job.end_time.strftime('%H:%M')})"


def _is_court_work(job: Job) -> bool:
    text = " ".join([job.client, job.address, job.city]).lower()
    return any(marker in text for marker in ("rechtbank", "zitting", "court", "hearing"))


def _prep_gap_before(job: Job, interpreter: Interpreter, schedule: list[Job]) -> float | None:
    window = interpreter.availability_on(job.date)
    same_day_before = [
        other
        for other in schedule
        if other.date == job.date and other.job_id != job.job_id and other.end_dt <= job.start_dt
    ]
    if same_day_before:
        previous = max(same_day_before, key=lambda other: other.end_dt)
        return (job.start_dt - previous.end_dt).total_seconds() / 60
    if window is None:
        return None
    return (job.start_dt - datetime.combine(job.date, window[0])).total_seconds() / 60


def _court_work_warnings(job: Job, interpreter: Interpreter, schedule: list[Job]) -> list[str]:
    if not _is_court_work(job):
        return []

    warnings = [
        (
            "Court hearing / rechtbankwerk (zitting = hearing): hearings can run longer than planned. "
            f"Confirm {interpreter.name} has enough buffer after this job if the zitting overruns."
        )
    ]
    prep_gap = _prep_gap_before(job, interpreter, schedule)
    if prep_gap is None:
        warnings.append(
            f"Preparation time / voorbereidingstijd: confirm {interpreter.name} has reviewed the case material before the hearing."
        )
    elif prep_gap < 30:
        warnings.append(
            f"Preparation time / voorbereidingstijd: only ~{round(prep_gap)} min is free before this hearing. "
            f"Ask {interpreter.name} if that is enough preparation time."
        )
    else:
        warnings.append(
            f"Preparation time / voorbereidingstijd: ~{round(prep_gap)} min appears free before this hearing. "
            f"Confirm with {interpreter.name} that this is enough."
        )
    return warnings


def _day_timeline(job: Job, schedule: list[Job]) -> list[Job]:
    same_day = [j for j in schedule if j.date == job.date and j.job_id != job.job_id]
    timeline = same_day + [job]
    timeline.sort(key=lambda j: j.start_dt)
    return timeline


def _onsite_legs(job: Job, interpreter: Interpreter, schedule: list[Job]) -> list[dict]:
    """Consecutive on-site-to-on-site (or home-to-first-on-site) legs for the
    day, each annotated with whether the leg involves `job` and which
    remote jobs (if any) are sandwiched between the two on-site jobs."""
    timeline = _day_timeline(job, schedule)
    window = interpreter.availability_on(job.date)
    window_start = window[0] if window else job.start_time

    legs: list[dict] = []
    last_onsite: Job | None = None
    last_onsite_end: datetime | None = None
    remote_buffer: list[Job] = []

    for entry in timeline:
        if entry.is_on_site:
            if last_onsite is None:
                prev_loc = _home_coord(interpreter)
                prev_end = datetime.combine(job.date, window_start)
                prev_label = f"home ({interpreter.home_city})"
                is_home_leg = True
            else:
                prev_loc = last_onsite.location
                prev_end = last_onsite_end
                prev_label = _label(last_onsite)
                is_home_leg = False

            gap_min = (entry.start_dt - prev_end).total_seconds() / 60
            distance_km = haversine_km(prev_loc, entry.location)
            required_min = travel_minutes_for_distance(distance_km)
            legs.append(
                {
                    "prev_label": prev_label,
                    "next_label": _label(entry),
                    "gap_min": gap_min,
                    "distance_km": distance_km,
                    "required_min": required_min,
                    "is_home_leg": is_home_leg,
                    "remote_between": list(remote_buffer),
                    "touches_job": entry.job_id == job.job_id or last_onsite is job,
                }
            )
            last_onsite = entry
            last_onsite_end = entry.end_dt
            remote_buffer = []
        else:
            remote_buffer.append(entry)

    return legs


def _trailing_home_leg(job: Job, interpreter: Interpreter, schedule: list[Job]) -> dict | None:
    """Travel from the last on-site job of the day back home, only reported
    when `job` is that last on-site job (or one of the trailing remote jobs
    after it)."""
    timeline = _day_timeline(job, schedule)
    window = interpreter.availability_on(job.date)
    if window is None:
        return None

    last_onsite: Job | None = None
    last_onsite_end: datetime | None = None
    trailing_remote: list[Job] = []
    for entry in timeline:
        if entry.is_on_site:
            last_onsite = entry
            last_onsite_end = entry.end_dt
            trailing_remote = []
        else:
            trailing_remote.append(entry)

    if last_onsite is None:
        return None
    if job.job_id != last_onsite.job_id and job not in trailing_remote:
        return None

    required_min = travel_minutes_for_distance(haversine_km(last_onsite.location, _home_coord(interpreter)))
    window_end_dt = datetime.combine(job.date, window[1])
    gap_min = (window_end_dt - last_onsite_end).total_seconds() / 60
    return {
        "last_label": _label(last_onsite),
        "required_min": required_min,
        "buffer_min": gap_min - required_min,
        "window_end": window[1].strftime("%H:%M"),
    }


# ---------------------------------------------------------------------------
# Cost / workload helpers
# ---------------------------------------------------------------------------


def is_qualified(job: Job, interpreter: Interpreter) -> bool:
    """Static eligibility: language + sworn match only — no availability,
    overlap, or travel check. Used for scarcity/coverage signals (this
    module's cost/workload warnings, `scheduler.py`'s job ordering, and
    `coverage.py`'s job-list indicator) so they all agree on what "qualified"
    means."""
    if interpreter.language != job.language:
        return False
    if job.sworn_required and not interpreter.sworn:
        return False
    return True


def _cheaper_alternative(
    job: Job, interpreter: Interpreter, all_interpreters: list[Interpreter], workload_lookup
) -> Interpreter | None:
    candidates = [
        i
        for i in all_interpreters
        if i.interpreter_id != interpreter.interpreter_id
        and is_qualified(job, i)
        and i.rate_eur_per_hour < interpreter.rate_eur_per_hour
    ]
    for candidate in sorted(candidates, key=lambda i: i.rate_eur_per_hour):
        schedule = workload_lookup.schedule_for(candidate.interpreter_id, exclude_job_id=job.job_id)
        if not check_hard_constraints_with_blacklist(
            job, candidate, schedule, blacklist_lookup=workload_lookup
        ):
            return candidate
    return None


def _workload_warning(
    job: Job,
    interpreter: Interpreter,
    all_interpreters: list[Interpreter],
    workload_lookup,
    threshold_min: float | None = None,
) -> str | None:
    if threshold_min is None:
        threshold_min = settings.get().workload_imbalance_threshold_min
    other_loads: list[float] = []
    for candidate in all_interpreters:
        if candidate.interpreter_id == interpreter.interpreter_id or not is_qualified(job, candidate):
            continue
        schedule = workload_lookup.schedule_for(candidate.interpreter_id, exclude_job_id=job.job_id)
        if check_hard_constraints_with_blacklist(
            job, candidate, schedule, blacklist_lookup=workload_lookup
        ):
            continue
        other_loads.append(workload_lookup.workload_minutes(candidate.interpreter_id, exclude_job_id=job.job_id))

    if not other_loads:
        return None

    this_load_after = (
        workload_lookup.workload_minutes(interpreter.interpreter_id, exclude_job_id=job.job_id) + job.duration_min
    )
    lightest = min(other_loads)
    if this_load_after - lightest >= threshold_min:
        return (
            f"{interpreter.name} would have {round(this_load_after)} min booked in total, well above "
            f"the least-loaded qualified alternative (~{round(lightest)} min). Consider balancing workload."
        )
    return None
