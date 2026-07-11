from datetime import time
from pathlib import Path

from dataclasses import replace

from app import reliability, settings
from app.data_loader import load_interpreters, load_jobs
from app.models import BlacklistEntry
from app.reliability import EventType
from app.scheduler import best_candidate_for, run_auto_assignment
from app.store import PlanningStore
from tests.factories import AMSTERDAM, ROTTERDAM, make_interpreter, make_job

BASE_DIR = Path(__file__).resolve().parent.parent
GRONINGEN = (53.2194, 6.5665)


def test_every_job_is_assigned_or_has_a_reason_on_the_real_dataset():
    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")
    store = PlanningStore(jobs, interpreters)
    run_auto_assignment(store)

    assert len(store.assignments) + len(store.unassigned_reasons) == len(jobs)
    for job in jobs:
        if job.job_id not in store.assignments:
            reasons = store.unassigned_reasons[job.job_id]
            assert reasons, f"{job.job_id} has no actionable reason"
            assert all(reasons)  # no empty-string reasons


def test_no_interpreter_is_double_booked_on_the_real_dataset():
    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")
    store = PlanningStore(jobs, interpreters)
    run_auto_assignment(store)

    for interpreter in interpreters:
        schedule = sorted(store.schedule_for(interpreter.interpreter_id), key=lambda j: j.start_dt)
        for a, b in zip(schedule, schedule[1:]):
            assert a.end_dt <= b.start_dt, f"{interpreter.interpreter_id} double-booked: {a.job_id}/{b.job_id}"


def test_auto_assignment_is_deterministic():
    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")

    store_a = PlanningStore(jobs, interpreters)
    run_auto_assignment(store_a)
    store_b = PlanningStore(jobs, interpreters)
    run_auto_assignment(store_b)

    assert store_a.assignments == store_b.assignments


def test_polish_sworn_job_is_unassignable_because_no_sworn_polish_interpreter_exists():
    # This is a known gap in the supplied roster (see README): J013 needs a
    # sworn Polish interpreter but the only Polish interpreter isn't sworn.
    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")
    store = PlanningStore(jobs, interpreters)
    run_auto_assignment(store)

    assert "J013" not in store.assignments
    assert "no sworn polish interpreter" in store.unassigned_reasons["J013"][0].lower()


def test_scheduler_prefers_cheaper_interpreter_when_both_are_clean():
    cheap = make_interpreter(interpreter_id="INT-CHEAP", rate=40.0)
    expensive = make_interpreter(interpreter_id="INT-EXP", rate=90.0)
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[cheap, expensive])

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == "INT-CHEAP"


def test_scheduler_skips_blacklisted_interpreter():
    blocked = make_interpreter(interpreter_id="INT-BLOCKED", rate=40.0)
    backup = make_interpreter(interpreter_id="INT-BACKUP", rate=90.0)
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[blocked, backup])
    store.add_blacklist_entry(
        BlacklistEntry(
            interpreter_id=blocked.interpreter_id,
            scope="client",
            client=job.client,
            reason="Client asked not to send again",
        )
    )

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == "INT-BACKUP"


def test_scheduler_leaves_job_unassigned_with_reason_when_no_language_match():
    interpreter = make_interpreter(language="Polish")
    job = make_job(language="Arabic")
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert job.job_id not in store.assignments
    assert "no interpreter" in store.unassigned_reasons[job.job_id][0].lower()


def test_scheduler_prefers_less_travel_when_rate_and_status_tie():
    # Same rate, same qualification, no existing bookings for either — the
    # only thing that differs is how far each interpreter's home is from
    # the job, so the added-travel tie-break should decide it.
    near = make_interpreter(interpreter_id="INT-NEAR", rate=50.0, home=AMSTERDAM)
    far = make_interpreter(interpreter_id="INT-FAR", rate=50.0, home=ROTTERDAM)
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore(jobs=[job], interpreters=[near, far])

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == "INT-NEAR"


def test_unassigned_reason_includes_coverage_note_when_no_qualified_interpreter_is_nearby():
    # Groningen is outside the default 100km coverage radius.
    # A narrow availability window guarantees this job stays unassigned for
    # an unrelated (availability) reason, so this also checks the coverage
    # note is *added alongside* the existing reason, not a replacement.
    far = make_interpreter(home=GRONINGEN, window=(time(9, 0), time(9, 30)))
    job = make_job(modality="on-site", location=AMSTERDAM, start=time(10, 0), end=time(11, 0))
    store = PlanningStore(jobs=[job], interpreters=[far])

    run_auto_assignment(store)

    assert job.job_id not in store.assignments
    reasons = store.unassigned_reasons[job.job_id]
    assert any("working hours" in r for r in reasons)
    assert any("within 100 km" in r for r in reasons)


def test_no_coverage_note_when_no_qualified_interpreter_exists_at_all():
    # Zero qualified interpreters (wrong language) is already a clear,
    # distinct reason — the coverage note would be redundant noise here.
    interpreter = make_interpreter(language="Polish")
    job = make_job(language="Arabic", modality="on-site", location=AMSTERDAM)
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    reasons = store.unassigned_reasons[job.job_id]
    assert not any("within 100 km" in r for r in reasons)


def test_auto_assignment_totals_unchanged_on_real_dataset():
    # Regression guard: the distance/coverage additions are meant to be
    # tie-breaks and extra context only, not to change who gets assigned.
    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")
    store = PlanningStore(jobs, interpreters)
    run_auto_assignment(store)

    assert len(store.assignments) == 96
    assert len(store.unassigned_reasons) == 14


def test_run_auto_assignment_tags_the_assignment_source_as_auto():
    interpreter = make_interpreter()
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert store.assignment_source[job.job_id] == "auto"


def test_careful_auto_assignment_leaves_warning_candidates_for_review():
    settings.update(auto_assign_risk_level=0)
    interpreter = make_interpreter(window=(time(4, 0), time(23, 0)))
    job = make_job(modality="on-site", location=GRONINGEN)
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert job.job_id not in store.assignments
    assert any("planner review" in reason for reason in store.unassigned_reasons[job.job_id])


def test_balanced_auto_assignment_allows_interpreter_choice_warnings():
    settings.update(auto_assign_risk_level=1)
    interpreter = make_interpreter(window=(time(4, 0), time(23, 0)))
    job = make_job(modality="on-site", location=GRONINGEN)
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == interpreter.interpreter_id


def test_balanced_auto_assignment_leaves_non_commute_warnings_for_review():
    settings.update(auto_assign_risk_level=1)
    interpreter = make_interpreter()
    job = replace(make_job(), client="Rechtbank Rotterdam - zitting")
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert job.job_id not in store.assignments
    assert any("planner review" in reason for reason in store.unassigned_reasons[job.job_id])


def test_flexible_auto_assignment_allows_any_warning_candidate():
    settings.update(auto_assign_risk_level=2)
    interpreter = make_interpreter()
    job = replace(make_job(), client="Rechtbank Rotterdam - zitting")
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == interpreter.interpreter_id


def test_auto_assignment_preserves_manual_assignment_on_rerun():
    manual = make_interpreter(interpreter_id="INT-MANUAL", rate=90.0)
    automatic = make_interpreter(interpreter_id="INT-AUTO", rate=40.0)
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[manual, automatic])
    store.assign(job.job_id, manual.interpreter_id, source="manual")

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == manual.interpreter_id
    assert store.assignment_source[job.job_id] == "manual"


def test_auto_assignment_marks_invalid_manual_assignment_instead_of_overwriting_it():
    manual = make_interpreter(interpreter_id="INT-MANUAL", language="Polish")
    automatic = make_interpreter(interpreter_id="INT-AUTO", language="Arabic")
    job = make_job(language="Arabic")
    store = PlanningStore(jobs=[job], interpreters=[manual, automatic])
    store.assign(job.job_id, manual.interpreter_id, source="manual")

    run_auto_assignment(store)

    assert job.job_id not in store.assignments
    reasons = store.unassigned_reasons[job.job_id]
    assert any("not overwritten" in reason for reason in reasons)
    assert any("Polish" in reason and "Arabic" in reason for reason in reasons)


def test_scheduler_prefers_higher_reliability_score_over_a_tie_on_everything_else():
    # Same rate, same home (so travel ties too) — only their reliability
    # history differs, which should be enough to decide it. This is the
    # "supersedes first-come-first-serve" behaviour: track record outranks
    # cost/travel ties, not just interpreter_id.
    reliable = make_interpreter(interpreter_id="INT-RELIABLE", rate=50.0, home=AMSTERDAM)
    unproven = make_interpreter(interpreter_id="INT-UNPROVEN", rate=50.0, home=AMSTERDAM)
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore(jobs=[job], interpreters=[reliable, unproven])

    reliability.record_event("INT-RELIABLE", "J-prior-1", EventType.COMPLETED)
    reliability.record_event("INT-RELIABLE", "J-prior-2", EventType.COMPLETED)
    reliability.record_event("INT-UNPROVEN", "J-prior-3", EventType.NO_SHOW)

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == "INT-RELIABLE"


def test_reliability_cannot_change_the_outcome_when_only_one_candidate_qualifies():
    # "If there's only one person, obviously the score data shouldn't be
    # used to make that decision" — it can't be, structurally: there's
    # nothing to tie-break against. A no-show history doesn't block the
    # only qualified interpreter from being picked.
    only_option = make_interpreter(rate=50.0, home=AMSTERDAM)
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore(jobs=[job], interpreters=[only_option])

    reliability.record_event(only_option.interpreter_id, "J-prior", EventType.NO_SHOW)

    run_auto_assignment(store)

    assert store.assignments[job.job_id] == only_option.interpreter_id


def test_best_candidate_for_finds_a_warning_tier_option_without_assigning_it():
    interpreter = make_interpreter()
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[interpreter])

    best = best_candidate_for(job, store)

    assert best is not None
    assert best[0].interpreter_id == interpreter.interpreter_id
    assert job.job_id not in store.assignments  # purely informational, no side effect
