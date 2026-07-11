from datetime import time

from app.rules import ValidationStatus, validate_assignment
from app.store import PlanningStore
from tests.factories import AMSTERDAM, ROTTERDAM, UTRECHT, make_interpreter, make_job


def test_clean_assignment_is_accepted():
    job = make_job()
    interpreter = make_interpreter()
    result = validate_assignment(job, interpreter, schedule=[])
    assert result.status == ValidationStatus.ACCEPTED
    assert result.reasons == []


def test_language_mismatch_is_rejected():
    job = make_job(language="Arabic")
    interpreter = make_interpreter(language="Polish")
    result = validate_assignment(job, interpreter, schedule=[])
    assert result.status == ValidationStatus.REJECTED
    assert "Polish" in result.reasons[0] and "Arabic" in result.reasons[0]


def test_sworn_required_but_interpreter_not_sworn_is_rejected():
    job = make_job(sworn_required=True)
    interpreter = make_interpreter(sworn=False)
    result = validate_assignment(job, interpreter, schedule=[])
    assert result.status == ValidationStatus.REJECTED
    assert "sworn" in result.reasons[0].lower()


def test_job_outside_availability_window_is_rejected():
    job = make_job(start=time(7, 0), end=time(8, 0))
    interpreter = make_interpreter(window=(time(9, 0), time(18, 0)))
    result = validate_assignment(job, interpreter, schedule=[])
    assert result.status == ValidationStatus.REJECTED
    assert "working hours" in result.reasons[0]


def test_interpreter_not_available_that_day_is_rejected():
    job = make_job()
    interpreter = make_interpreter(window=None)
    result = validate_assignment(job, interpreter, schedule=[])
    assert result.status == ValidationStatus.REJECTED
    assert "not available" in result.reasons[0]


def test_overlapping_job_is_rejected():
    interpreter = make_interpreter()
    existing = make_job(job_id="J-existing", start=time(9, 0), end=time(10, 0))
    candidate = make_job(job_id="J-candidate", start=time(9, 30), end=time(10, 30))
    result = validate_assignment(candidate, interpreter, schedule=[existing])
    assert result.status == ValidationStatus.REJECTED
    assert "already booked" in result.reasons[0]


def test_back_to_back_onsite_jobs_far_apart_are_rejected_for_insufficient_travel():
    interpreter = make_interpreter(home=AMSTERDAM)
    existing = make_job(job_id="J-existing", start=time(9, 0), end=time(10, 0), location=AMSTERDAM)
    # Rotterdam is ~60km from Amsterdam; 15 minutes is nowhere near enough.
    candidate = make_job(job_id="J-candidate", start=time(10, 5), end=time(11, 0), location=ROTTERDAM)
    result = validate_assignment(candidate, interpreter, schedule=[existing])
    assert result.status == ValidationStatus.REJECTED
    assert "travel time" in result.reasons[0]


def test_tight_but_feasible_travel_is_a_warning():
    interpreter = make_interpreter(home=UTRECHT)
    existing = make_job(job_id="J-existing", start=time(9, 0), end=time(10, 0), location=AMSTERDAM)
    # Amsterdam -> Utrecht is a short-ish hop; give just a little more than the
    # bare minimum so it's feasible but flagged as tight.
    from app.travel import estimate_travel_minutes

    required = estimate_travel_minutes(AMSTERDAM, UTRECHT)
    gap_minutes = int(required) + 5
    start_minute = 10 * 60 + gap_minutes
    candidate = make_job(
        job_id="J-candidate",
        start=time(start_minute // 60, start_minute % 60),
        end=time((start_minute + 60) // 60, (start_minute + 60) % 60),
        location=UTRECHT,
    )
    result = validate_assignment(candidate, interpreter, schedule=[existing])
    assert result.status == ValidationStatus.WARNING
    assert any("tight" in r.lower() for r in result.reasons)


def test_cheaper_qualified_alternative_produces_warning():
    expensive = make_interpreter(interpreter_id="INT-EXP", rate=90.0)
    cheap = make_interpreter(interpreter_id="INT-CHEAP", rate=40.0)
    job = make_job()
    store = PlanningStore(jobs=[job], interpreters=[expensive, cheap])

    result = validate_assignment(
        job, expensive, schedule=[], all_interpreters=[expensive, cheap], workload_lookup=store
    )
    assert result.status == ValidationStatus.WARNING
    assert any("cheaper" in r.lower() for r in result.reasons)


def test_no_cheaper_alternative_when_it_would_also_be_rejected():
    expensive = make_interpreter(interpreter_id="INT-EXP", rate=90.0)
    # Cheap interpreter exists but doesn't speak the language, so is not a
    # real alternative and must not trigger the cheaper-alternative warning.
    cheap_wrong_language = make_interpreter(interpreter_id="INT-CHEAP", rate=40.0, language="Polish")
    job = make_job(language="Arabic")
    store = PlanningStore(jobs=[job], interpreters=[expensive, cheap_wrong_language])

    result = validate_assignment(
        job, expensive, schedule=[], all_interpreters=[expensive, cheap_wrong_language], workload_lookup=store
    )
    assert result.status == ValidationStatus.ACCEPTED


def test_workload_imbalance_warning():
    busy = make_interpreter(interpreter_id="INT-BUSY", rate=50.0)
    idle = make_interpreter(interpreter_id="INT-IDLE", rate=50.0)
    new_job = make_job(job_id="J-new", start=time(15, 0), end=time(16, 0))

    # Give `busy` several hours of existing work that doesn't overlap the
    # new job, so both interpreters are otherwise equally eligible.
    filler_jobs = [
        make_job(job_id=f"J-filler-{i}", start=time(9 + i, 0), end=time(9 + i, 45))
        for i in range(4)
    ]
    store = PlanningStore(jobs=filler_jobs + [new_job], interpreters=[busy, idle])
    for fj in filler_jobs:
        store.assign(fj.job_id, busy.interpreter_id)

    schedule = store.schedule_for(busy.interpreter_id, exclude_job_id=new_job.job_id)
    result = validate_assignment(
        new_job, busy, schedule, all_interpreters=[busy, idle], workload_lookup=store
    )
    assert result.status == ValidationStatus.WARNING
    assert any("workload" in r.lower() for r in result.reasons)
