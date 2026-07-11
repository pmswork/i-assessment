from app import reliability
from app.reliability import EventType


def test_no_history_is_neutral_not_penalised(tmp_path):
    db_path = tmp_path / "reliability.db"

    result = reliability.score("INT-01", db_path=db_path)

    assert result.has_history is False
    assert result.points == 0.0
    assert result.sample_size == 0


def test_completed_job_increases_points(tmp_path):
    db_path = tmp_path / "reliability.db"

    reliability.record_event("INT-01", "J001", EventType.COMPLETED, db_path=db_path)
    result = reliability.score("INT-01", db_path=db_path)

    assert result.has_history is True
    assert result.completed == 1
    assert result.points > 0


def test_no_show_decreases_points_more_than_late_cancellation():
    # Uses two separate interpreters in the same db so both can be compared
    # from a single set of recorded events.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "reliability.db"
        reliability.record_event("INT-NOSHOW", "J001", EventType.NO_SHOW, db_path=db_path)
        reliability.record_event("INT-LATE", "J002", EventType.LATE_CANCELLATION, db_path=db_path)

        no_show_points = reliability.score("INT-NOSHOW", db_path=db_path).points
        late_points = reliability.score("INT-LATE", db_path=db_path).points

        assert no_show_points < late_points < 0


def test_events_for_returns_recorded_events_for_that_interpreter_only(tmp_path):
    db_path = tmp_path / "reliability.db"
    reliability.record_event("INT-01", "J001", EventType.ACCEPTED, db_path=db_path)
    reliability.record_event("INT-01", "J001", EventType.COMPLETED, db_path=db_path)
    reliability.record_event("INT-02", "J002", EventType.NO_SHOW, db_path=db_path)

    events = reliability.events_for("INT-01", db_path=db_path)

    assert len(events) == 2
    assert all(e.job_id == "J001" for e in events)


def test_points_for_matches_score_points(tmp_path):
    db_path = tmp_path / "reliability.db"
    reliability.record_event("INT-01", "J001", EventType.COMPLETED, db_path=db_path)

    assert reliability.points_for("INT-01", db_path=db_path) == reliability.score("INT-01", db_path=db_path).points


def test_default_db_path_is_monkeypatched_by_the_isolation_fixture():
    # This relies on the autouse fixture in conftest.py — if it stopped
    # working, this would silently start reading/writing the real
    # reliability.db used by the running app, which is exactly the
    # cross-test pollution the fixture exists to prevent.
    assert "reliability_test.db" in str(reliability.DEFAULT_DB_PATH)
