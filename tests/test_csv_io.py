from pathlib import Path

from app import csv_io

BASE_DIR = Path(__file__).resolve().parent.parent


def test_parses_the_supplied_jobs_csv_without_errors():
    text = (BASE_DIR / "jobs.csv").read_text(encoding="utf-8")
    result = csv_io.parse_jobs_csv(text)
    assert result.ok
    assert len(result.jobs) == 110


def test_parses_the_supplied_interpreters_csv_without_errors():
    text = (BASE_DIR / "interpreters.csv").read_text(encoding="utf-8")
    result = csv_io.parse_interpreters_csv(text)
    assert result.ok
    assert len(result.interpreters) == 8


def test_jobs_csv_missing_columns_is_rejected_with_a_specific_message():
    result = csv_io.parse_jobs_csv("job_id,date\nJ1,2026-07-14\n")
    assert not result.ok
    assert result.jobs == []
    assert "Missing required column" in result.errors[0]


def test_jobs_csv_bad_row_is_rejected_with_a_row_number():
    header = "job_id,date,start_time,duration_min,end_time,language,sworn_required,modality,client,address,city,lat,lon\n"
    bad_row = "J1,not-a-date,09:00,30,09:30,Arabic,no,remote,Client,,,,\n"
    result = csv_io.parse_jobs_csv(header + bad_row)
    assert not result.ok
    assert "row 2" in result.errors[0]


def test_jobs_csv_rejects_duplicate_job_ids():
    header = "job_id,date,start_time,duration_min,end_time,language,sworn_required,modality,client,address,city,lat,lon\n"
    rows = (
        "J1,2026-07-14,09:00,30,09:30,Arabic,no,remote,Client,,,,\n"
        "J1,2026-07-14,10:00,30,10:30,Arabic,no,remote,Client,,,,\n"
    )
    result = csv_io.parse_jobs_csv(header + rows)
    assert not result.ok
    assert "duplicate" in result.errors[0]


def test_onsite_job_without_coordinates_is_rejected():
    header = "job_id,date,start_time,duration_min,end_time,language,sworn_required,modality,client,address,city,lat,lon\n"
    row = "J1,2026-07-14,09:00,30,09:30,Arabic,no,on-site,Client,Street 1,City,,\n"
    result = csv_io.parse_jobs_csv(header + row)
    assert not result.ok
    assert "lat/lon" in result.errors[0]


def test_a_single_bad_row_rejects_the_whole_file_atomically():
    header = "job_id,date,start_time,duration_min,end_time,language,sworn_required,modality,client,address,city,lat,lon\n"
    good_row = "J1,2026-07-14,09:00,30,09:30,Arabic,no,remote,Client,,,,\n"
    bad_row = "J2,bad-date,09:00,30,09:30,Arabic,no,remote,Client,,,,\n"
    result = csv_io.parse_jobs_csv(header + good_row + bad_row)
    assert not result.ok
    assert result.jobs == []  # J1 was individually valid, but nothing is applied


def test_interpreters_csv_rejects_non_yes_no_sworn_value():
    header = (
        "interpreter_id,name,language,sworn,home_city,home_lat,home_lon,rate_eur_per_hour,"
        "availability_2026-07-14,availability_2026-07-15\n"
    )
    row = "INT-1,Test,Arabic,maybe,Amsterdam,52.37,4.89,50,08:00-18:00,08:00-18:00\n"
    result = csv_io.parse_interpreters_csv(header + row)
    assert not result.ok
    assert "sworn" in result.errors[0]


def test_jobs_csv_export_then_reimport_round_trips():
    text = (BASE_DIR / "jobs.csv").read_text(encoding="utf-8")
    original = csv_io.parse_jobs_csv(text)

    exported = csv_io.jobs_to_csv(original.jobs)
    reimported = csv_io.parse_jobs_csv(exported)

    assert reimported.ok
    assert sorted(j.job_id for j in reimported.jobs) == sorted(j.job_id for j in original.jobs)
    assert {j.job_id: j for j in reimported.jobs} == {j.job_id: j for j in original.jobs}


def test_interpreters_csv_export_then_reimport_round_trips():
    text = (BASE_DIR / "interpreters.csv").read_text(encoding="utf-8")
    original = csv_io.parse_interpreters_csv(text)

    exported = csv_io.interpreters_to_csv(original.interpreters)
    reimported = csv_io.parse_interpreters_csv(exported)

    assert reimported.ok
    assert {i.interpreter_id: i for i in reimported.interpreters} == {
        i.interpreter_id: i for i in original.interpreters
    }
