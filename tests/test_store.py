from app.store import PlanningStore
from tests.factories import make_interpreter, make_job


def test_upsert_job_adds_a_new_job():
    store = PlanningStore([], [])
    job = make_job()
    store.upsert_job(job)
    assert store.jobs[job.job_id] == job


def test_upsert_job_replacing_an_assigned_job_drops_its_assignment():
    interpreter = make_interpreter()
    job = make_job()
    store = PlanningStore([job], [interpreter])
    store.assign(job.job_id, interpreter.interpreter_id)

    edited = make_job(start=job.start_time)  # same id by default from factory
    store.upsert_job(edited)

    assert job.job_id not in store.assignments


def test_delete_job_removes_job_and_its_assignment():
    interpreter = make_interpreter()
    job = make_job()
    store = PlanningStore([job], [interpreter])
    store.assign(job.job_id, interpreter.interpreter_id)

    store.delete_job(job.job_id)

    assert job.job_id not in store.jobs
    assert job.job_id not in store.assignments


def test_delete_interpreter_frees_their_assigned_jobs():
    interpreter = make_interpreter()
    job = make_job()
    store = PlanningStore([job], [interpreter])
    store.assign(job.job_id, interpreter.interpreter_id)

    store.delete_interpreter(interpreter.interpreter_id)

    assert interpreter.interpreter_id not in store.interpreters
    assert job.job_id not in store.assignments  # freed, not left dangling


def test_replace_jobs_drops_assignments_for_jobs_that_no_longer_exist():
    interpreter = make_interpreter()
    kept_job = make_job(job_id="J-KEEP")
    removed_job = make_job(job_id="J-REMOVE")
    store = PlanningStore([kept_job, removed_job], [interpreter])
    store.assign(kept_job.job_id, interpreter.interpreter_id)
    store.assign(removed_job.job_id, interpreter.interpreter_id)

    store.replace_jobs([kept_job])

    assert set(store.jobs) == {"J-KEEP"}
    assert kept_job.job_id in store.assignments
    assert removed_job.job_id not in store.assignments


def test_replace_interpreters_drops_assignments_for_interpreters_that_no_longer_exist():
    kept = make_interpreter(interpreter_id="INT-KEEP")
    removed = make_interpreter(interpreter_id="INT-REMOVE")
    job_a = make_job(job_id="J-A")
    job_b = make_job(job_id="J-B")
    store = PlanningStore([job_a, job_b], [kept, removed])
    store.assign(job_a.job_id, kept.interpreter_id)
    store.assign(job_b.job_id, removed.interpreter_id)

    store.replace_interpreters([kept])

    assert set(store.interpreters) == {"INT-KEEP"}
    assert job_a.job_id in store.assignments
    assert job_b.job_id not in store.assignments
