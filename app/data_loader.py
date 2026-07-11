"""CSV ingestion for jobs.csv and interpreters.csv.

Deliberately tolerant of the two known data quirks documented in the
README (Polish has no sworn interpreter; J057 falls outside every
interpreter's working hours) — those are business-rule outcomes, not
parse errors, so loading never fails because of them.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from pathlib import Path

from .models import Interpreter, Job

DATE_COLUMNS = ("availability_2026-07-14", "availability_2026-07-15")


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _parse_window(value: str) -> tuple[time, time] | None:
    value = value.strip()
    if value in ("", "-", "—"):
        return None
    start_s, end_s = value.split("-")
    return _parse_time(start_s.strip()), _parse_time(end_s.strip())


def load_interpreters(path: str | Path) -> list[Interpreter]:
    interpreters: list[Interpreter] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            availability: dict[date, tuple[time, time]] = {}
            for col in DATE_COLUMNS:
                window = _parse_window(row[col])
                if window is not None:
                    day = _parse_date(col.removeprefix("availability_"))
                    availability[day] = window
            interpreters.append(
                Interpreter(
                    interpreter_id=row["interpreter_id"],
                    name=row["name"],
                    language=row["language"],
                    sworn=row["sworn"].strip().lower() == "yes",
                    home_city=row["home_city"],
                    home_lat=float(row["home_lat"]),
                    home_lon=float(row["home_lon"]),
                    rate_eur_per_hour=float(row["rate_eur_per_hour"]),
                    availability=availability,
                )
            )
    return interpreters


def load_jobs(path: str | Path) -> list[Job]:
    jobs: list[Job] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lat = float(row["lat"]) if row["lat"].strip() else None
            lon = float(row["lon"]) if row["lon"].strip() else None
            jobs.append(
                Job(
                    job_id=row["job_id"],
                    date=_parse_date(row["date"]),
                    start_time=_parse_time(row["start_time"]),
                    duration_min=int(row["duration_min"]),
                    end_time=_parse_time(row["end_time"]),
                    language=row["language"],
                    sworn_required=row["sworn_required"].strip().lower() == "yes",
                    modality=row["modality"].strip(),
                    client=row["client"],
                    address=row["address"],
                    city=row["city"],
                    lat=lat,
                    lon=lon,
                )
            )
    return jobs
