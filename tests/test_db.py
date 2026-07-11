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
    custom = settings.Settings(auto_assign_risk_level=1, coverage_radius_km=75.0, coverage_bar_cap=5)

    db.save_settings(custom)
    loaded = db.load_settings()

    assert loaded == custom


def test_load_settings_fills_new_fields_from_defaults_when_database_is_older():
    custom = settings.Settings(coverage_radius_km=75.0, coverage_bar_cap=5)
    db.save_settings(custom)
    with db._connection(None) as conn:
        conn.execute("DELETE FROM settings WHERE key = 'auto_assign_risk_level'")
        conn.execute("DELETE FROM settings WHERE key = 'urgent_unassigned_days'")

    loaded = db.load_settings()

    assert loaded is not None
    assert loaded.coverage_radius_km == 75.0
    assert loaded.auto_assign_risk_level == settings.Settings().auto_assign_risk_level
    assert loaded.urgent_unassigned_days == settings.Settings().urgent_unassigned_days


def test_load_settings_returns_none_when_table_is_empty():
    assert db.load_settings() is None


def test_emptied_database_still_counts_as_initialized():
    # A planner who deletes all jobs via Admin and restarts must NOT get the
    # CSV seed resurrected: once settings (written on every startup) or any
    # roster data exist, the database counts as initialized even with zero
    # jobs. See db.has_data.
    db.save_settings(settings.Settings())

    assert db.has_data() is True

    store = PlanningStore([], [], persist=True)
    store.persist_now()  # empty jobs + empty roster, settings still present

    assert db.has_data() is True


def test_legacy_assignments_check_constraint_is_migrated(tmp_path):
    import sqlite3

    legacy_path = tmp_path / "legacy.db"

    # Build a database with today's full schema, then swap the assignments
    # table for the pre-'auto_confirmed' version (old CHECK constraint) and
    # seed one row — exactly what a database from the previous release
    # looks like.
    interpreter = make_interpreter()
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore([job], [interpreter], persist=True)
    store.assign(job.job_id, interpreter.interpreter_id, source="auto")
    db.sync_store(store, db_path=legacy_path)

    conn = sqlite3.connect(legacy_path)
    conn.executescript(
        """
        DROP TABLE assignments;
        CREATE TABLE assignments (
            job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
            interpreter_id TEXT NOT NULL REFERENCES interpreters(interpreter_id) ON DELETE CASCADE,
            source TEXT NOT NULL CHECK (source IN ('auto', 'manual'))
        );
        """
    )
    conn.execute(
        "INSERT INTO assignments (job_id, interpreter_id, source) VALUES (?, ?, 'auto')",
        (job.job_id, interpreter.interpreter_id),
    )
    conn.commit()
    conn.close()

    # Opening the legacy database through db.py must rebuild the CHECK so
    # 'auto_confirmed' rows can be written, keeping the existing row...
    with db._connection(legacy_path) as conn:
        conn.execute(
            "UPDATE assignments SET source = 'auto_confirmed' WHERE job_id = ?", (job.job_id,)
        )
    _assignments, sources, _reasons = db.load_assignments(legacy_path)
    assert sources == {job.job_id: "auto_confirmed"}

    # ...while still rejecting genuinely invalid source values.
    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        with db._connection(legacy_path) as conn:
            conn.execute(
                "UPDATE assignments SET source = 'nonsense' WHERE job_id = ?", (job.job_id,)
            )


def test_auto_confirmed_source_round_trips():
    interpreter = make_interpreter()
    job = make_job(modality="on-site", location=AMSTERDAM)
    store = PlanningStore([job], [interpreter], persist=True)
    store.assign(job.job_id, interpreter.interpreter_id, source="auto")
    store.auto_confirm_provisional()

    store.persist_now()

    _assignments, sources, _reasons = db.load_assignments()
    assert sources == {job.job_id: "auto_confirmed"}
