"""Route-level smoke tests for the FastAPI app.

`app.main` builds one module-level `store` from the real CSVs (or
app.db, if one already exists) at import time — there's no dependency
injection for a test-only store. That initial bootstrap happens during
test collection, before tests/conftest.py's autouse fixtures apply, so it
is *not* isolated from a developer's real `app.db`/`reliability.db`.
Tests here therefore either stick to read-only assertions, or start with
`POST /auto-assign` to reset to a known, reproducible baseline before
mutating anything, so tests don't depend on execution order or on what's
in those files at collection time.

Everything that happens *during* a test (route handlers calling
`store.persist_now()`, `reliability.record_event()`, `settings.update()`,
etc.) runs after the fixtures have applied and so *is* fully isolated —
see PlanningStore's module docstring for why `persist_now()` specifically
was designed to re-resolve `db.DEFAULT_DB_PATH` on every call rather than
capture it once.
"""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_index_loads_and_shows_the_logo_and_coverage_column():
    response = client.get("/")

    assert response.status_code == 200
    assert "Logo_Elan" in response.text
    assert "Coverage" in response.text


def test_job_detail_hides_unqualified_interpreters_from_the_candidate_list():
    # J013 needs a sworn Polish interpreter; the roster's only Polish
    # interpreter (Piotr Nowak) isn't sworn, so the candidate table must be
    # empty and say so, not silently list him.
    response = client.get("/jobs/J013")

    assert response.status_code == 200
    assert "candidate-name" not in response.text
    assert "No interpreter on the roster supports" in response.text


def test_job_detail_only_lists_language_qualified_candidates():
    # J005 needs Arabic; Piotr Nowak (Polish) must not appear as a candidate.
    response = client.get("/jobs/J005")

    assert response.status_code == 200
    assert "Piotr Nowak" not in response.text


def test_validate_rejects_wrong_language_even_if_posted_directly():
    # Defense in depth at the route/rules level, not just template
    # filtering: even bypassing the UI's dropdown-equivalent, the server
    # must still reject a structurally invalid pairing.
    response = client.post("/jobs/J013/validate", data={"interpreter_id": "INT-08"})

    assert response.status_code == 200
    assert "rejected" in response.text.lower()


def test_rejected_assignment_is_never_persisted_via_direct_post():
    client.post("/jobs/J013/assign", data={"interpreter_id": "INT-08"})

    detail = client.get("/jobs/J013")
    assert "Currently assigned" not in detail.text
    assert "Assigned to" not in detail.text


def test_manual_assign_then_outcome_flow_updates_reliability_profile():
    client.post("/auto-assign")  # known baseline
    client.post("/jobs/J001/unassign")

    validate_resp = client.post("/jobs/J001/validate", data={"interpreter_id": "INT-02"})
    assert validate_resp.status_code == 200

    assign_resp = client.post(
        "/jobs/J001/assign",
        data={"interpreter_id": "INT-02", "confirm_warning": "1"},
        follow_redirects=False,
    )
    assert assign_resp.status_code == 303

    detail = client.get("/jobs/J001")
    assert "Karim Haddad" in detail.text
    assert "source-manual" in detail.text

    outcome_resp = client.post("/jobs/J001/outcome", data={"outcome": "completed"}, follow_redirects=False)
    assert outcome_resp.status_code == 303

    profile = client.get("/interpreters/INT-02")
    assert profile.status_code == 200
    assert "completed" in profile.text
    assert "J001" in profile.text


def test_outcome_on_an_unassigned_job_is_a_safe_no_op():
    client.post("/jobs/J013/unassign")

    response = client.post("/jobs/J013/outcome", data={"outcome": "completed"}, follow_redirects=False)

    assert response.status_code == 303  # redirects back without crashing


def test_interpreter_profile_page_loads_for_every_interpreter():
    for interpreter_id in ("INT-01", "INT-02", "INT-03", "INT-04", "INT-05", "INT-06", "INT-07", "INT-08"):
        response = client.get(f"/interpreters/{interpreter_id}")
        assert response.status_code == 200
        assert "Basic info" in response.text
        assert "Reliability" in response.text


def test_interpreters_index_loads_the_roster():
    response = client.get("/interpreters")
    assert response.status_code == 200
    assert "Interpreters" in response.text
    assert "INT-01" in response.text
    assert "Availability" in response.text


def test_warning_assignment_requires_explicit_confirmation():
    client.post("/auto-assign")
    client.post("/jobs/J001/unassign")

    response = client.post("/jobs/J001/assign", data={"interpreter_id": "INT-02"})

    assert response.status_code == 200
    assert "explicit planner confirmation" in response.text
    detail = client.get("/jobs/J001")
    assert "source-manual" not in detail.text
    assert "Assigned to <a href=\"/interpreters/INT-02\">Karim Haddad</a>" not in detail.text


def test_unknown_route_returns_404():
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_page_loads_and_shows_current_values():
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Coverage radius" in response.text


def test_settings_update_persists_and_redirects():
    response = client.post(
        "/settings",
        data={
            "average_speed_kmh": "50", "fixed_overhead_min": "10", "travel_buffer_min": "20",
            "long_distance_km": "40", "workload_imbalance_threshold_min": "120",
            "coverage_radius_km": "50", "coverage_bar_cap": "3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?saved=1"

    page = client.get("/settings?saved=1")
    assert "Settings updated" in page.text
    assert 'value="50.0"' in page.text


def test_settings_update_rejects_invalid_value():
    response = client.post(
        "/settings",
        data={
            "average_speed_kmh": "-5", "fixed_overhead_min": "10", "travel_buffer_min": "20",
            "long_distance_km": "40", "workload_imbalance_threshold_min": "120",
            "coverage_radius_km": "50", "coverage_bar_cap": "3",
        },
    )
    assert response.status_code == 400
    assert "must be at least" in response.text


# ---------------------------------------------------------------------------
# Admin — dashboard, job/interpreter CRUD, CSV import/export
# ---------------------------------------------------------------------------


def test_admin_dashboard_loads_with_jobs_and_interpreters():
    response = client.get("/admin")
    assert response.status_code == 200
    assert "J001" in response.text
    assert "INT-01" in response.text


def test_admin_job_create_edit_delete_flow():
    create = client.post(
        "/admin/jobs/new",
        data={
            "job_id": "J-ADMIN-TEST", "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00",
            "language": "Arabic", "modality": "remote", "client": "Test Client",
            "address": "", "city": "", "lat": "", "lon": "",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    dashboard = client.get("/admin")
    assert "J-ADMIN-TEST" in dashboard.text

    edit = client.post(
        "/admin/jobs/J-ADMIN-TEST/edit",
        data={
            "date": "2026-07-14", "start_time": "09:00", "end_time": "11:00",  # duration changed
            "language": "Arabic", "modality": "remote", "client": "Test Client Updated",
            "address": "", "city": "", "lat": "", "lon": "",
        },
        follow_redirects=False,
    )
    assert edit.status_code == 303
    assert "Test Client Updated" in client.get("/admin").text

    delete = client.post("/admin/jobs/J-ADMIN-TEST/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert "J-ADMIN-TEST" not in client.get("/admin").text


def test_admin_job_create_rejects_onsite_without_coordinates():
    response = client.post(
        "/admin/jobs/new",
        data={
            "job_id": "J-ADMIN-BAD", "date": "2026-07-14", "start_time": "09:00", "end_time": "10:00",
            "language": "Arabic", "modality": "on-site", "client": "Test Client",
            "address": "Street 1", "city": "Utrecht", "lat": "", "lon": "",
        },
    )
    assert response.status_code == 400
    assert "latitude and longitude" in response.text
    assert "J-ADMIN-BAD" not in client.get("/admin").text


def test_admin_interpreter_create_edit_delete_flow():
    create = client.post(
        "/admin/interpreters/new",
        data={
            "interpreter_id": "INT-ADMIN-TEST", "name": "Admin Test Person", "language": "Arabic",
            "home_city": "Utrecht", "home_lat": "52.09", "home_lon": "5.12", "rate_eur_per_hour": "55",
            "avail_day1_start": "08:00", "avail_day1_end": "18:00",
            "avail_day2_start": "", "avail_day2_end": "",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303

    profile = client.get("/interpreters/INT-ADMIN-TEST")
    assert profile.status_code == 200
    assert "Admin Test Person" in profile.text

    edit = client.post(
        "/admin/interpreters/INT-ADMIN-TEST/edit",
        data={
            "name": "Admin Test Person Updated", "language": "Arabic", "sworn": "on",
            "home_city": "Utrecht", "home_lat": "52.09", "home_lon": "5.12", "rate_eur_per_hour": "60",
            "avail_day1_start": "08:00", "avail_day1_end": "18:00",
            "avail_day2_start": "", "avail_day2_end": "",
        },
        follow_redirects=False,
    )
    assert edit.status_code == 303
    updated_profile = client.get("/interpreters/INT-ADMIN-TEST")
    assert "Admin Test Person Updated" in updated_profile.text
    assert "sworn" in updated_profile.text.lower()

    delete = client.post("/admin/interpreters/INT-ADMIN-TEST/delete", follow_redirects=False)
    assert delete.status_code == 303
    assert "INT-ADMIN-TEST" not in client.get("/admin").text


def test_admin_interpreter_create_rejects_duplicate_id():
    response = client.post(
        "/admin/interpreters/new",
        data={
            "interpreter_id": "INT-01", "name": "Duplicate", "language": "Arabic",
            "home_city": "Utrecht", "home_lat": "52.09", "home_lon": "5.12", "rate_eur_per_hour": "55",
            "avail_day1_start": "", "avail_day1_end": "", "avail_day2_start": "", "avail_day2_end": "",
        },
    )
    assert response.status_code == 400
    assert "already exists" in response.text


def test_admin_export_jobs_csv_downloads_current_data():
    response = client.get("/admin/export/jobs.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "job_id,date,start_time" in response.text
    assert "J001" in response.text


def test_admin_export_interpreters_csv_downloads_current_data():
    response = client.get("/admin/export/interpreters.csv")
    assert response.status_code == 200
    assert "interpreter_id,name,language" in response.text
    assert "INT-01" in response.text


def test_admin_import_rejects_invalid_csv_and_leaves_data_unchanged():
    bad_csv = b"job_id,date\nJ1,2026-07-14\n"
    response = client.post(
        "/admin/import/jobs",
        files={"file": ("jobs.csv", io.BytesIO(bad_csv), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303  # always redirects; the rejection shows on /admin

    dashboard = client.get("/admin")
    assert "Missing required column" in dashboard.text
    assert "J001" in dashboard.text  # original data untouched


def test_admin_import_jobs_round_trips_the_original_csv():
    from pathlib import Path

    original = (Path(__file__).resolve().parent.parent / "jobs.csv").read_bytes()
    response = client.post(
        "/admin/import/jobs",
        files={"file": ("jobs.csv", io.BytesIO(original), "text/csv")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    dashboard = client.get("/admin")
    assert "import rejected" not in dashboard.text
    assert "J001" in dashboard.text
    assert "J110" in dashboard.text
