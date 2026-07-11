"""CSV import/export for jobs and interpreters — the data layer behind the
Admin tab.

Import is intentionally atomic and validated up front: every row is
checked before anything is applied, and a single bad row rejects the
*whole* file with a specific, row-numbered reason, rather than silently
skipping bad rows or leaving the dataset half-updated. This mirrors the
"never persist a rejected assignment" defensiveness already used
elsewhere in this app (see `rules.py` / `main.py`'s `/assign` route) —
better to reject loudly with a precise reason than accept partially-wrong
operational data.

The expected columns match the two CSVs the app ships with (see README);
this is not a generic CSV importer. In particular, interpreter
availability columns are fixed to the two dates in the supplied dataset
(`availability_2026-07-14`, `availability_2026-07-15`) — a real multi-day
roster tool would need a date-agnostic schema instead (see README "what
I'd build next").
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, time

from .models import Interpreter, Job

AVAILABILITY_COLUMNS = ("availability_2026-07-14", "availability_2026-07-15")

JOB_COLUMNS = [
    "job_id", "date", "start_time", "duration_min", "end_time", "language",
    "sworn_required", "modality", "client", "address", "city", "lat", "lon",
]
INTERPRETER_COLUMNS = [
    "interpreter_id", "name", "language", "sworn", "home_city", "home_lat",
    "home_lon", "rate_eur_per_hour", *AVAILABILITY_COLUMNS,
]


@dataclass
class ImportResult:
    jobs: list[Job] = field(default_factory=list)
    interpreters: list[Interpreter] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _require_columns(fieldnames, expected: list[str], kind: str) -> list[str]:
    missing = [c for c in expected if c not in (fieldnames or [])]
    if missing:
        return [f"Missing required column(s) for {kind}: {', '.join(missing)}."]
    return []


def _parse_bool(value: str, field_name: str) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "1"):
        return True
    if v in ("no", "false", "0"):
        return False
    raise ValueError(f"{field_name} must be yes/no")


def _parse_availability_window(value: str) -> tuple[time, time] | None:
    value = value.strip()
    if value in ("", "-", "—"):
        return None
    try:
        start_s, end_s = value.split("-")
        return (
            datetime.strptime(start_s.strip(), "%H:%M").time(),
            datetime.strptime(end_s.strip(), "%H:%M").time(),
        )
    except ValueError as exc:
        raise ValueError(f"invalid availability window '{value}' (expected HH:MM-HH:MM)") from exc


def parse_jobs_csv(text: str) -> ImportResult:
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text))
    result.errors.extend(_require_columns(reader.fieldnames, JOB_COLUMNS, "jobs.csv"))
    if result.errors:
        return result

    seen_ids: set[str] = set()
    for line_no, row in enumerate(reader, start=2):  # header is line 1
        try:
            job_id = row["job_id"].strip()
            if not job_id:
                raise ValueError("job_id is required")
            if job_id in seen_ids:
                raise ValueError(f"duplicate job_id '{job_id}'")
            seen_ids.add(job_id)

            job_date = datetime.strptime(row["date"].strip(), "%Y-%m-%d").date()
            start_time = datetime.strptime(row["start_time"].strip(), "%H:%M").time()
            end_time = datetime.strptime(row["end_time"].strip(), "%H:%M").time()
            duration_min = int(row["duration_min"])
            if duration_min <= 0:
                raise ValueError("duration_min must be positive")

            language = row["language"].strip()
            if not language:
                raise ValueError("language is required")

            sworn_required = _parse_bool(row["sworn_required"], "sworn_required")
            modality = row["modality"].strip()
            if modality not in ("on-site", "remote"):
                raise ValueError("modality must be 'on-site' or 'remote'")

            client = row["client"].strip()
            if not client:
                raise ValueError("client is required")
            address = row.get("address", "").strip()
            city = row.get("city", "").strip()

            lat_raw = row.get("lat", "").strip()
            lon_raw = row.get("lon", "").strip()
            lat = float(lat_raw) if lat_raw else None
            lon = float(lon_raw) if lon_raw else None
            if modality == "on-site" and (lat is None or lon is None):
                raise ValueError("on-site jobs need lat/lon")
            if lat is not None and not (-90 <= lat <= 90):
                raise ValueError("lat out of range (-90..90)")
            if lon is not None and not (-180 <= lon <= 180):
                raise ValueError("lon out of range (-180..180)")

            result.jobs.append(
                Job(
                    job_id=job_id, date=job_date, start_time=start_time, duration_min=duration_min,
                    end_time=end_time, language=language, sworn_required=sworn_required, modality=modality,
                    client=client, address=address, city=city, lat=lat, lon=lon,
                )
            )
        except (ValueError, KeyError) as exc:
            result.errors.append(f"row {line_no}: {exc}")

    if result.errors:
        result.jobs = []
    return result


def parse_interpreters_csv(text: str) -> ImportResult:
    result = ImportResult()
    reader = csv.DictReader(io.StringIO(text))
    result.errors.extend(_require_columns(reader.fieldnames, INTERPRETER_COLUMNS, "interpreters.csv"))
    if result.errors:
        return result

    seen_ids: set[str] = set()
    for line_no, row in enumerate(reader, start=2):
        try:
            interpreter_id = row["interpreter_id"].strip()
            if not interpreter_id:
                raise ValueError("interpreter_id is required")
            if interpreter_id in seen_ids:
                raise ValueError(f"duplicate interpreter_id '{interpreter_id}'")
            seen_ids.add(interpreter_id)

            name = row["name"].strip()
            if not name:
                raise ValueError("name is required")
            language = row["language"].strip()
            if not language:
                raise ValueError("language is required")
            sworn = _parse_bool(row["sworn"], "sworn")
            home_city = row["home_city"].strip()
            if not home_city:
                raise ValueError("home_city is required")

            home_lat = float(row["home_lat"])
            home_lon = float(row["home_lon"])
            if not (-90 <= home_lat <= 90):
                raise ValueError("home_lat out of range (-90..90)")
            if not (-180 <= home_lon <= 180):
                raise ValueError("home_lon out of range (-180..180)")

            rate = float(row["rate_eur_per_hour"])
            if rate <= 0:
                raise ValueError("rate_eur_per_hour must be positive")

            availability: dict = {}
            for col in AVAILABILITY_COLUMNS:
                window = _parse_availability_window(row[col])
                if window is not None:
                    day = datetime.strptime(col.removeprefix("availability_"), "%Y-%m-%d").date()
                    availability[day] = window

            result.interpreters.append(
                Interpreter(
                    interpreter_id=interpreter_id, name=name, language=language, sworn=sworn,
                    home_city=home_city, home_lat=home_lat, home_lon=home_lon,
                    rate_eur_per_hour=rate, availability=availability,
                )
            )
        except (ValueError, KeyError) as exc:
            result.errors.append(f"row {line_no}: {exc}")

    if result.errors:
        result.interpreters = []
    return result


def jobs_to_csv(jobs: list[Job]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(JOB_COLUMNS)
    for j in sorted(jobs, key=lambda j: (j.date, j.start_time, j.job_id)):
        writer.writerow(
            [
                j.job_id, j.date.isoformat(), j.start_time.strftime("%H:%M"), j.duration_min,
                j.end_time.strftime("%H:%M"), j.language, "yes" if j.sworn_required else "no",
                j.modality, j.client, j.address, j.city,
                "" if j.lat is None else j.lat, "" if j.lon is None else j.lon,
            ]
        )
    return buf.getvalue()


def interpreters_to_csv(interpreters: list[Interpreter]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(INTERPRETER_COLUMNS)
    for i in sorted(interpreters, key=lambda i: i.interpreter_id):
        row = [
            i.interpreter_id, i.name, i.language, "yes" if i.sworn else "no",
            i.home_city, i.home_lat, i.home_lon, i.rate_eur_per_hour,
        ]
        for col in AVAILABILITY_COLUMNS:
            day = datetime.strptime(col.removeprefix("availability_"), "%Y-%m-%d").date()
            window = i.availability.get(day)
            row.append(f"{window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')}" if window else "—")
        writer.writerow(row)
    return buf.getvalue()
