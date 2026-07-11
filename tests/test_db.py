from app import db, settings
from app.models import BlacklistEntry
from app.store import PlanningStore
from tests.factories import AMSTERDAM, make_interpreter, make_job


def test_has_data_is_false_for_a_fresh_database():
    # tests/conftest.py's isolated_app_db fixture points db.DEFAULT_DB_PATH
    # at a fresh temp file for every test, so the default (no explicit
    # db_path) is already isolated here.
    assert db.has_data() is False


def test_sync_store_round_trips_jobs_interpreters_and_assignments():
    interpreter = make_interpreter()
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore([job], [interpreter], persist=True)
    store.assign(job.job_id, interpreter.interpreter_id, source="manual")

    store.persist_now()

    assert db.has_data() is True
    assert db.load_jobs() == [job]
    assert db.load_interpreters() == [interpreter]
    assignments, sources, reasons = db.load_assignments()
    assert assignments == {job.job_id: interpreter.interpreter_id}
    assert sources == {job.job_id: "manual"}
    assert reasons == {}


def test_sync_store_round_trips_unassigned_reasons():
    job = make_job()
    store = PlanningStore([job], [], persist=True)
    store.mark_unassigned(job.job_id, ["no interpreter supports this language"])

    store.persist_now()

    _assignments, _sources, reasons = db.load_assignments()
    assert reasons == {job.job_id: ["no interpreter supports this language"]}


def test_sync_store_round_trips_blacklist_entries():
    interpreter = make_interpreter()
    store = PlanningStore([], [interpreter], persist=True)
    entry = BlacklistEntry(
        interpreter_id=interpreter.interpreter_id,
        scope="client",
        client="Test Client",
        reason="Client requested another interpreter",
    )
    store.add_blacklist_entry(entry)

    store.persist_now()

    assert db.load_blacklist_entries() == [entry]


def test_sync_store_is_a_full_replace_not_an_append():
    job_a = make_job(job_id="J-A")
    store = PlanningStore([job_a], [], persist=True)
    store.persist_now()

    job_b = make_job(job_id="J-B")
    store.jobs = {job_b.job_id: job_b}
    store.persist_now()

    loaded = db.load_jobs()
    assert [j.job_id for j in loaded] == ["J-B"]


def test_persist_now_is_a_no_op_without_persist_enabled():
    store = PlanningStore([make_job()], [make_interpreter()])  # persist defaults to False
    store.persist_now()  # must not raise, must not write anything
    assert db.has_data() is False


def test_deleting_an_interpreter_cascades_to_their_availability_and_assignments():
    interpreter = make_interpreter()
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore([job], [interpreter], persist=True)
    store.assign(job.job_id, interpreter.interpreter_id)
    store.persist_now()

    store.delete_interpreter(interpreter.interpreter_id)
    store.persist_now()

    assert db.load_interpreters() == []
    assignments, _sources, _reasons = db.load_assignments()
    assert assignments == {}


def test_explicit_db_path_overrides_the_default(tmp_path):
    # db.py's public functions still accept an explicit db_path for direct
    # callers (used by the CLI-style scripts and by tests that want two
    # separate databases in the same test) — verify that override works
    # independently of the autouse default-path isolation.
    custom_path = tmp_path / "custom.db"
    job = make_job()
    store = PlanningStore([job], [], persist=True)

    db.sync_store(store, db_path=custom_path)

    assert db.has_data(custom_path) is True
    assert db.has_data() is False  # default (isolated) db untouched
    assert db.load_jobs(custom_path) == [job]


def test_settings_round_trip():
    custom = settings.Settings(coverage_radius_km=75.0, coverage_bar_cap=5)

    db.save_settings(custom)
    loaded = db.load_settings()

    assert loaded == custom


def test_load_settings_returns_none_when_table_is_empty():
    assert db.load_settings() is None
