"""Small builders so tests can construct Interpreters/Jobs tersely."""

from __future__ import annotations

from datetime import date, time

from app.models import Interpreter, Job

DAY = date(2026, 7, 14)

# Roughly Amsterdam / Rotterdam / Utrecht, far enough apart that travel time
# is non-trivial with the 45 km/h + 10 min assumption in app/travel.py.
AMSTERDAM = (52.3731, 4.8925)
ROTTERDAM = (51.9244, 4.4778)
UTRECHT = (52.0907, 5.1216)


def make_interpreter(
    interpreter_id="INT-T1",
    name="Test Interpreter",
    language="Arabic",
    sworn=False,
    home=AMSTERDAM,
    rate=50.0,
    window=(time(8, 0), time(18, 0)),
    day=DAY,
) -> Interpreter:
    availability = {day: window} if window is not None else {}
    return Interpreter(
        interpreter_id=interpreter_id,
        name=name,
        language=language,
        sworn=sworn,
        home_city="Test City",
        home_lat=home[0],
        home_lon=home[1],
        rate_eur_per_hour=rate,
        availability=availability,
    )


def make_job(
    job_id="J-T1",
    day=DAY,
    start=time(9, 0),
    end=time(10, 0),
    language="Arabic",
    sworn_required=False,
    modality="on-site",
    location=AMSTERDAM,
) -> Job:
    duration = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    lat, lon = (location if modality == "on-site" and location else (None, None))
    return Job(
        job_id=job_id,
        date=day,
        start_time=start,
        duration_min=duration,
        end_time=end,
        language=language,
        sworn_required=sworn_required,
        modality=modality,
        client="Test Client",
        address="Test Address 1",
        city="Test City",
        lat=lat,
        lon=lon,
    )
