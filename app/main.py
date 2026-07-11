"""FastAPI planner UI.

Thin web layer only: every decision about whether an assignment is valid
lives in `rules.py`. Routes here just call into `rules`/`scheduler` and
render the result — this keeps business logic testable without spinning
up a server (see tests/).

Design goal for this UI: the system should make most decisions itself.
Auto-assignment runs first and handles the large majority of jobs; the
manual-assignment screen is meant to be the last resort for the jobs it
couldn't place, so it filters out anything that fails the basic
language/sworn requirement up front and leads with a short "what do we
have here" summary rather than a blank form.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import csv_io
from . import db as db_module
from . import reliability
from . import settings as settings_module
from .coverage import coverage_gauge, coverage_label, coverage_stats
from .data_loader import load_interpreters, load_jobs
from .models import BlacklistEntry, Interpreter, Job
from .reliability import EventType
from .rules import ValidationStatus, is_qualified, validate_assignment
from .scheduler import best_candidate_for, run_auto_assignment
from .store import PlanningStore

BASE_DIR = Path(__file__).resolve().parent.parent

app = FastAPI(title="ELAN Interpreter Planner")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def _bootstrap_store() -> PlanningStore:
    """Load persisted state from app.db if this isn't a first run;
    otherwise seed from the supplied CSVs and run auto-assignment (which
    persists everything it touches — see scheduler.run_auto_assignment —
    so the very next restart will load from the database instead)."""
    if db_module.has_data():
        loaded_settings = db_module.load_settings()
        if loaded_settings is not None:
            settings_module.set_current(loaded_settings)

        bootstrapped = PlanningStore(db_module.load_jobs(), db_module.load_interpreters(), persist=True)
        assignments, sources, reasons = db_module.load_assignments()
        bootstrapped.assignments = assignments
        bootstrapped.assignment_source = sources
        bootstrapped.unassigned_reasons = reasons
        bootstrapped.blacklist_entries = db_module.load_blacklist_entries()
        return bootstrapped

    seeded = PlanningStore(
        load_jobs(BASE_DIR / "jobs.csv"),
        load_interpreters(BASE_DIR / "interpreters.csv"),
        persist=True,
    )
    run_auto_assignment(seeded)
    return seeded


store = _bootstrap_store()
db_module.save_settings(settings_module.get())  # ensure the settings table always reflects the active values


def _get_job_or_404(job_id: str) -> Job:
    job = store.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


def _get_interpreter_or_404(interpreter_id: str) -> Interpreter:
    interpreter = store.interpreters.get(interpreter_id)
    if interpreter is None:
        raise HTTPException(status_code=404, detail=f"Interpreter {interpreter_id} not found")
    return interpreter


_JOB_TOKEN_RE = r"J[A-Za-z0-9-]+"


def _linkify_references(text: str) -> Markup:
    """Turn job ids and interpreter names inside a plain-text reason into
    links to their detail pages, so a planner reading e.g. "Tight commute
    home: after J045 (...), Oksana Kovalenko's working window ..." can jump
    straight to the job or the interpreter being talked about.

    Reasons stay plain strings everywhere in the business layer (rules.py /
    scheduler.py never emit HTML); this is purely a presentation concern,
    applied as a Jinja filter at render time. The text is HTML-escaped
    FIRST, then only exact, currently-known job ids and interpreter names
    are wrapped in <a> tags — an unknown token that merely looks like a job
    id is left as text, and nothing user-controlled can smuggle markup in.
    """
    escaped = str(escape(text))
    # Names are matched against their escaped form so a name containing an
    # HTML-special character would still line up with the escaped text.
    names_escaped = {str(escape(i.name)): i.interpreter_id for i in store.interpreters.values()}
    name_parts = [re.escape(name) for name in sorted(names_escaped, key=len, reverse=True)]
    pattern = "|".join([*name_parts, _JOB_TOKEN_RE]) if name_parts else _JOB_TOKEN_RE

    def _replace(match: re.Match) -> str:
        token = match.group(0)
        interpreter_id = names_escaped.get(token)
        if interpreter_id is not None:
            return f'<a href="/interpreters/{quote(interpreter_id)}">{token}</a>'
        if token in store.jobs:
            return f'<a href="/jobs/{quote(token)}">{token}</a>'
        return token

    return Markup(re.sub(rf"\b(?:{pattern})\b", _replace, escaped))


templates.env.filters["linkify"] = _linkify_references

# Planner-facing labels for the assignment sources (see store.py for the
# lifecycle). Registered as a filter so every template names them the same
# way: {{ row.source|source_label }}.
_SOURCE_LABELS = {"auto": "provisional", "auto_confirmed": "auto-confirmed", "manual": "confirmed"}
templates.env.filters["source_label"] = lambda source: _SOURCE_LABELS.get(source, source)


_REASONS_PREVIEW_COUNT = 2
try:
    AMSTELVEEN_TZ = ZoneInfo("Europe/Amsterdam")
except ZoneInfoNotFoundError:
    AMSTELVEEN_TZ = None


def _amstelveen_today():
    if AMSTELVEEN_TZ is None:
        return datetime.now().date()
    return datetime.now(AMSTELVEEN_TZ).date()


def _job_row(job, today=None):
    if today is None:
        today = _amstelveen_today()
    interpreter = store.assigned_interpreter(job.job_id)
    stats = coverage_stats(job, list(store.interpreters.values()))
    gauge = coverage_gauge(stats)
    reasons = store.unassigned_reasons.get(job.job_id, [])
    days_until = (job.date - today).days
    is_urgent = interpreter is None and days_until <= settings_module.get().urgent_unassigned_days
    return {
        "job": job,
        "interpreter": interpreter,
        "source": store.assignment_source.get(job.job_id),
        "reasons_preview": reasons[:_REASONS_PREVIEW_COUNT],
        "reasons_more_count": max(0, len(reasons) - _REASONS_PREVIEW_COUNT),
        "coverage_label": coverage_label(stats),
        "coverage_gauge": gauge,
        "coverage_within": stats.within_radius,
        "days_until": days_until,
        "is_urgent": is_urgent,
    }


@app.get("/")
def index(request: Request, status: str = "all", sort: str = "date"):
    today = _amstelveen_today()
    rows = [_job_row(j, today=today) for j in store.jobs_sorted()]
    assigned_count = sum(1 for r in rows if r["interpreter"] is not None)
    unassigned_count = len(rows) - assigned_count

    # Filter options mirror the actual statuses a row can have: needs
    # decision (unassigned), provisional (auto), auto-confirmed, confirmed
    # (manual). "confirmed" alone covers both planner-confirmed kinds.
    if status == "needs_decision":
        rows = [row for row in rows if row["interpreter"] is None]
    elif status == "provisional":
        rows = [row for row in rows if row["source"] == "auto"]
    elif status == "auto_confirmed":
        rows = [row for row in rows if row["source"] == "auto_confirmed"]
    elif status == "confirmed":
        rows = [row for row in rows if row["source"] in ("manual", "auto_confirmed")]
    else:
        status = "all"

    sorters = {
        "date": lambda row: (row["job"].date, row["job"].start_time, row["job"].job_id),
        "job": lambda row: row["job"].job_id,
        "client": lambda row: (row["job"].client.casefold(), row["job"].date, row["job"].start_time),
        "language": lambda row: (row["job"].language.casefold(), row["job"].date, row["job"].start_time),
        "status": lambda row: (0 if row["interpreter"] is None else 1, row["job"].date, row["job"].start_time),
        "coverage": lambda row: (row["coverage_within"], row["job"].date, row["job"].start_time),
    }
    if sort not in sorters:
        sort = "date"
    rows = sorted(rows, key=sorters[sort])

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "assigned_count": assigned_count,
            "unassigned_count": unassigned_count,
            "total_count": len(store.jobs),
            "displayed_count": len(rows),
            "status_filter": status,
            "sort": sort,
            "coverage_radius_km": settings_module.get().coverage_radius_km,
            "urgent_unassigned_days": settings_module.get().urgent_unassigned_days,
        },
    )


@app.post("/auto-assign")
def auto_assign():
    run_auto_assignment(store)
    return RedirectResponse("/", status_code=303)


def _candidate_row(job, interpreter):
    """Everything the job-detail candidate list needs about one qualified
    interpreter: quick facts for the info tooltip and their reliability
    track record, so a planner can judge a candidate without leaving the
    page."""
    rel = reliability.score(interpreter.interpreter_id)
    blacklist_reasons = store.blacklist_reasons(interpreter.interpreter_id, job.client)
    return {
        "interpreter": interpreter,
        "workload_min": store.workload_minutes(interpreter.interpreter_id),
        "reliability": rel,
        "blacklist_reasons": blacklist_reasons,
        "is_blacklisted": bool(blacklist_reasons),
        "info": (
            f"{interpreter.language}{' (sworn)' if interpreter.sworn else ''} · "
            f"€{interpreter.rate_eur_per_hour:.0f}/h · home: {interpreter.home_city} · "
            f"currently booked {store.workload_minutes(interpreter.interpreter_id)} min"
        ),
    }


def _planner_questions_for(result) -> list[str]:
    if result is None or result.status != ValidationStatus.WARNING:
        return []

    questions: list[str] = []
    for reason in result.reasons:
        lower = reason.lower()
        if "tight travel" in lower or "not enough travel" in lower or "tight commute" in lower:
            questions.append("Can you confirm you can still make this timing in practice, including realistic travel and arrival buffer?")
        elif "remote job" in lower and "transit" in lower:
            questions.append("Can you confirm whether you can take the remote call properly from where you will be between appointments?")
        elif "cheaper" in lower:
            questions.append("Is there a reason to prefer this interpreter despite the cheaper available alternative?")
        elif "workload" in lower:
            questions.append("Can you confirm this workload is still reasonable for the interpreter that day?")
        elif "long one-way distance" in lower:
            questions.append("Can you confirm the interpreter is willing to travel this distance for the appointment?")
        elif "court hearing" in lower:
            questions.append("Can you confirm the interpreter has enough buffer if the hearing runs longer than planned?")
        elif "preparation" in lower:
            questions.append("Can you confirm the interpreter has enough preparation time before the hearing?")

    if not questions:
        questions.append("Can you confirm with the interpreter that this assignment is still workable despite the warning above?")

    deduped: list[str] = []
    for question in questions:
        if question not in deduped:
            deduped.append(question)
    return deduped


def _god_mode_enabled() -> bool:
    return settings_module.get().auto_assign_risk_level == 3


def _god_mode_can_assign(result) -> bool:
    if result is None or result.status != ValidationStatus.REJECTED or not _god_mode_enabled():
        return False
    return not any("already booked" in reason.lower() or "overlaps" in reason.lower() for reason in result.reasons)


@app.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str):
    return _render_job_detail(request, job_id)


def _render_job_detail(
    request: Request,
    job_id: str,
    *,
    checked_interpreter_id: str | None = None,
    result=None,
):
    job = _get_job_or_404(job_id)
    current = store.assigned_interpreter(job_id)
    reasons = store.unassigned_reasons.get(job_id, [])
    qualified = [i for i in store.interpreters_sorted() if is_qualified(job, i)]
    candidates = [_candidate_row(job, i) for i in qualified]

    suggestion = None
    if current is None:
        best = best_candidate_for(job, store)
        if best is not None:
            best_interpreter, best_result = best
            suggestion = {"interpreter": best_interpreter, "result": best_result}

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "current_interpreter": current,
            "current_source": store.assignment_source.get(job_id),
            "reasons": reasons,
            "candidates": candidates,
            "no_qualified_interpreters": len(qualified) == 0,
            "suggestion": suggestion,
            "checked_interpreter_id": checked_interpreter_id,
            "result": result,
            "planner_questions": _planner_questions_for(result),
            "god_mode_can_assign": _god_mode_can_assign(result),
            "ValidationStatus": ValidationStatus,
        },
    )


@app.post("/jobs/{job_id}/validate")
def validate(request: Request, job_id: str, interpreter_id: str = Form(...)):
    job = _get_job_or_404(job_id)
    interpreter = _get_interpreter_or_404(interpreter_id)
    schedule = store.schedule_for(interpreter_id, exclude_job_id=job_id)
    result = validate_assignment(
        job, interpreter, schedule, all_interpreters=list(store.interpreters.values()), workload_lookup=store
    )
    return _render_job_detail(request, job_id, checked_interpreter_id=interpreter_id, result=result)


@app.post("/jobs/{job_id}/assign")
def assign(
    request: Request,
    job_id: str,
    interpreter_id: str = Form(...),
    confirm_warning: str | None = Form(None),
    confirm_god_mode: str | None = Form(None),
):
    job = _get_job_or_404(job_id)
    interpreter = _get_interpreter_or_404(interpreter_id)
    schedule = store.schedule_for(interpreter_id, exclude_job_id=job_id)
    result = validate_assignment(
        job, interpreter, schedule, all_interpreters=list(store.interpreters.values()), workload_lookup=store
    )
    if result.status == ValidationStatus.REJECTED:
        if _god_mode_can_assign(result) and confirm_god_mode == "1":
            store.assign(job_id, interpreter_id, source="manual")
            store.persist_now()
            reliability.record_event(interpreter_id, job_id, EventType.ACCEPTED)
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        # Defense in depth: never persist a rejected assignment, even if a
        # client somehow posts here directly without checking first.
        return _render_job_detail(request, job_id, checked_interpreter_id=interpreter_id, result=result)
    if result.status == ValidationStatus.WARNING and confirm_warning != "1":
        result.reasons.insert(0, "Warning assignments require explicit planner confirmation.")
        return _render_job_detail(request, job_id, checked_interpreter_id=interpreter_id, result=result)

    store.assign(job_id, interpreter_id, source="manual")
    store.persist_now()
    reliability.record_event(interpreter_id, job_id, EventType.ACCEPTED)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/confirm")
def confirm_assignment(job_id: str):
    """Promote a provisional (auto) assignment to planner-confirmed.

    Auto-assignment output is a *proposal* — in the real workflow nothing
    is agreed with the interpreter until a planner has actually spoken to
    them. Confirming records that the interpreter said yes: the assignment
    is re-labelled "confirmed" (stored as source="manual", the same tier as
    a hand-picked assignment), an ACCEPTED reliability event is logged, and
    from then on re-running auto-assignment will never move it (only
    provisional assignments get replanned — see scheduler.py)."""
    _get_job_or_404(job_id)
    interpreter_id = store.assignments.get(job_id)
    if interpreter_id is not None and store.assignment_source.get(job_id) == "auto":
        store.assign(job_id, interpreter_id, source="manual")
        store.persist_now()
        reliability.record_event(interpreter_id, job_id, EventType.ACCEPTED)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.post("/jobs/{job_id}/unassign")
def unassign(job_id: str):
    _get_job_or_404(job_id)
    store.unassign(job_id)
    store.persist_now()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


_OUTCOME_EVENTS = {
    "completed": EventType.COMPLETED,
    "no_show": EventType.NO_SHOW,
    "late_cancellation": EventType.LATE_CANCELLATION,
}


@app.post("/jobs/{job_id}/outcome")
def record_outcome(job_id: str, outcome: str = Form(...)):
    """Close the loop on a job that already happened: did the assigned
    interpreter actually complete it, no-show, or cancel late? This is what
    feeds the reliability score — see reliability.py."""
    _get_job_or_404(job_id)
    interpreter_id = store.assignments.get(job_id)
    event_type = _OUTCOME_EVENTS.get(outcome)
    if interpreter_id is not None and event_type is not None:
        reliability.record_event(interpreter_id, job_id, event_type)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/interpreters/{interpreter_id}")
def interpreter_detail(request: Request, interpreter_id: str):
    interpreter = _get_interpreter_or_404(interpreter_id)
    schedule = sorted(store.schedule_for(interpreter_id), key=lambda j: j.start_dt)
    rel = reliability.score(interpreter_id)
    events = reliability.events_for(interpreter_id)
    clients = sorted({job.client for job in store.jobs.values()}, key=str.casefold)
    return templates.TemplateResponse(
        request,
        "interpreter_detail.html",
        {
            "interpreter": interpreter,
            "schedule": schedule,
            "workload_min": store.workload_minutes(interpreter_id),
            "reliability": rel,
            "events": events,
            "blacklist_entries": store.blacklist_for(interpreter_id),
            "clients": clients,
        },
    )


@app.post("/interpreters/{interpreter_id}/blacklist")
def add_blacklist_entry(
    interpreter_id: str,
    scope: str = Form(...),
    client: str = Form(""),
    reason: str = Form(""),
):
    _get_interpreter_or_404(interpreter_id)
    if scope not in ("global", "client"):
        return RedirectResponse(f"/interpreters/{interpreter_id}", status_code=303)
    client = client.strip()
    if scope == "client" and not client:
        return RedirectResponse(f"/interpreters/{interpreter_id}", status_code=303)
    store.add_blacklist_entry(
        BlacklistEntry(interpreter_id=interpreter_id, scope=scope, client=client, reason=reason)
    )
    store.persist_now()
    return RedirectResponse(f"/interpreters/{interpreter_id}", status_code=303)


@app.post("/interpreters/{interpreter_id}/blacklist/delete")
def delete_blacklist_entry(
    interpreter_id: str,
    scope: str = Form(...),
    client: str = Form(""),
):
    store.delete_blacklist_entry(interpreter_id, scope, client)
    store.persist_now()
    return RedirectResponse(f"/interpreters/{interpreter_id}", status_code=303)


@app.get("/interpreters")
def interpreters_index(request: Request):
    rows = []
    for interpreter in store.interpreters_sorted():
        rows.append(
            {
                "interpreter": interpreter,
                "schedule": sorted(store.schedule_for(interpreter.interpreter_id), key=lambda j: j.start_dt),
                "workload_min": store.workload_minutes(interpreter.interpreter_id),
                "reliability": reliability.score(interpreter.interpreter_id),
            }
        )
    return templates.TemplateResponse(request, "interpreters.html", {"rows": rows})


# ---------------------------------------------------------------------------
# Settings — planner-adjustable thresholds (see settings.py)
# ---------------------------------------------------------------------------


def _settings_fields(error: str | None = None):
    current = settings_module.get()
    return {
        "fields": [
            {
                "name": name,
                "value": getattr(current, name),
                "label": settings_module.FIELD_INFO[name].label,
                "help_text": settings_module.FIELD_INFO[name].help_text,
                "options": settings_module.FIELD_INFO[name].options,
            }
            for name in settings_module.field_names()
        ],
        "error": error,
    }


@app.get("/settings")
def settings_page(request: Request, saved: bool = False):
    return templates.TemplateResponse(request, "settings.html", {**_settings_fields(), "saved": saved})


@app.post("/settings")
def update_settings(
    request: Request,
    auto_assign_risk_level: int = Form(...),
    average_speed_kmh: float = Form(...),
    fixed_overhead_min: float = Form(...),
    travel_buffer_min: float = Form(...),
    long_distance_km: float = Form(...),
    workload_imbalance_threshold_min: float = Form(...),
    coverage_radius_km: float = Form(...),
    coverage_bar_cap: int = Form(...),
    urgent_unassigned_days: int = Form(...),
):
    try:
        settings_module.update(
            auto_assign_risk_level=auto_assign_risk_level,
            average_speed_kmh=average_speed_kmh,
            fixed_overhead_min=fixed_overhead_min,
            travel_buffer_min=travel_buffer_min,
            long_distance_km=long_distance_km,
            workload_imbalance_threshold_min=workload_imbalance_threshold_min,
            coverage_radius_km=coverage_radius_km,
            coverage_bar_cap=coverage_bar_cap,
            urgent_unassigned_days=urgent_unassigned_days,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "settings.html", {**_settings_fields(error=str(exc)), "saved": False}, status_code=400
        )

    db_module.save_settings(settings_module.get())
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Admin — CSV import (validated, atomic) / export, and manual data edits.
#
# This is for fixing mistakes and onboarding new interpreters/jobs mid-
# session, not the primary workflow — the app is meant to run itself from
# the supplied CSVs (see main.py module docstring). Import replaces the
# whole jobs list (or the whole interpreter roster) atomically: either
# every row validates and the whole file is applied, or none of it is,
# with specific row-numbered reasons either way (see csv_io.py).
# ---------------------------------------------------------------------------

_import_feedback: dict[str, list[str]] = {"jobs": [], "interpreters": []}

_AVAILABILITY_DAY1 = datetime.strptime(
    csv_io.AVAILABILITY_COLUMNS[0].removeprefix("availability_"), "%Y-%m-%d"
).date()
_AVAILABILITY_DAY2 = datetime.strptime(
    csv_io.AVAILABILITY_COLUMNS[1].removeprefix("availability_"), "%Y-%m-%d"
).date()


@app.get("/admin")
def admin_dashboard(request: Request, deleted: str | None = None):
    jobs_rows = [{"job": j, "interpreter": store.assigned_interpreter(j.job_id)} for j in store.jobs_sorted()]
    interpreter_rows = [
        {"interpreter": i, "workload_min": store.workload_minutes(i.interpreter_id)}
        for i in store.interpreters_sorted()
    ]
    delete_messages = {
        "jobs": "Deleted all jobs.",
        "interpreters": "Deleted all interpreters.",
    }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "jobs_rows": jobs_rows,
            "interpreter_rows": interpreter_rows,
            "job_import_errors": _import_feedback["jobs"],
            "interpreter_import_errors": _import_feedback["interpreters"],
            "admin_notice": delete_messages.get(deleted),
        },
        # The admin page is where destructive bulk actions live; a stale
        # cached copy (old form markup, old buttons) is exactly how a click
        # ends up doing something different from what's on screen. Never
        # let the browser serve this page from cache.
        headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/auto-confirm")
def admin_auto_confirm_provisional():
    """Bulk-promote every provisional (auto) assignment to "auto-confirmed"
    on behalf of the interpreters — for demos and for workflows where the
    agency confirms schedules wholesale instead of calling each interpreter.
    Auto-confirmed assignments become planner-owned (auto-assignment reruns
    won't move them), but deliberately log NO reliability ACCEPTED events:
    nobody individually said yes, and fabricating track record would skew
    the ranking that reliability scores feed (see store.auto_confirm_provisional)."""
    store.auto_confirm_provisional()
    store.persist_now()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/import/jobs")
async def admin_import_jobs(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    result = csv_io.parse_jobs_csv(text)
    _import_feedback["jobs"] = result.errors
    if result.ok:
        _import_feedback["interpreters"] = []
        store.replace_jobs(result.jobs)
        store.persist_now()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/import/interpreters")
async def admin_import_interpreters(file: UploadFile = File(...)):
    text = (await file.read()).decode("utf-8", errors="replace")
    result = csv_io.parse_interpreters_csv(text)
    _import_feedback["interpreters"] = result.errors
    if result.ok:
        _import_feedback["jobs"] = []
        store.replace_interpreters(result.interpreters)
        store.persist_now()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/export/jobs.csv")
def admin_export_jobs():
    return PlainTextResponse(
        csv_io.jobs_to_csv(list(store.jobs.values())),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs.csv"},
    )


@app.get("/admin/export/interpreters.csv")
def admin_export_interpreters():
    return PlainTextResponse(
        csv_io.interpreters_to_csv(list(store.interpreters.values())),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=interpreters.csv"},
    )


def _job_values_from(job: Job | None) -> dict:
    if job is None:
        return {
            "job_id": "", "date": "", "start_time": "", "end_time": "", "language": "",
            "sworn_required": False, "modality": "on-site", "client": "", "address": "", "city": "",
            "lat": "", "lon": "",
        }
    return {
        "job_id": job.job_id, "date": job.date.isoformat(), "start_time": job.start_time.strftime("%H:%M"),
        "end_time": job.end_time.strftime("%H:%M"), "language": job.language, "sworn_required": job.sworn_required,
        "modality": job.modality, "client": job.client, "address": job.address, "city": job.city,
        "lat": "" if job.lat is None else job.lat, "lon": "" if job.lon is None else job.lon,
    }


@app.get("/admin/jobs/new")
def admin_new_job_form(request: Request):
    return templates.TemplateResponse(
        request, "admin_job_form.html", {"values": _job_values_from(None), "is_new": True, "errors": []}
    )


@app.get("/admin/jobs/{job_id}/edit")
def admin_edit_job_form(request: Request, job_id: str):
    return templates.TemplateResponse(
        request, "admin_job_form.html",
        {"values": _job_values_from(_get_job_or_404(job_id)), "is_new": False, "errors": []},
    )


def _save_job_form(
    request: Request,
    *,
    is_new: bool,
    job_id: str,
    date_str: str,
    start_time_str: str,
    end_time_str: str,
    language: str,
    sworn_required_raw: str | None,
    modality: str,
    client: str,
    address: str,
    city: str,
    lat_str: str,
    lon_str: str,
):
    errors: list[str] = []
    job_id = job_id.strip()
    if not is_new and job_id not in store.jobs:
        # Editing something that no longer exists (deleted in another tab,
        # or a hand-typed URL) must not quietly create a new job.
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job_id:
        errors.append("Job ID is required.")
    elif is_new and job_id in store.jobs:
        errors.append(f"Job ID '{job_id}' already exists.")

    job_date = start_time = end_time = None
    try:
        job_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        errors.append("Date must be in YYYY-MM-DD format.")
    try:
        start_time = datetime.strptime(start_time_str, "%H:%M").time()
        end_time = datetime.strptime(end_time_str, "%H:%M").time()
    except ValueError:
        errors.append("Start/end time must be in HH:MM format.")

    duration_min = 0
    if start_time and end_time:
        duration_min = (end_time.hour * 60 + end_time.minute) - (start_time.hour * 60 + start_time.minute)
        if duration_min <= 0:
            errors.append("End time must be after start time.")

    language = language.strip()
    if not language:
        errors.append("Language is required.")
    if modality not in ("on-site", "remote"):
        errors.append("Modality must be 'on-site' or 'remote'.")
    client = client.strip()
    if not client:
        errors.append("Client is required.")

    lat = lon = None
    try:
        lat = float(lat_str) if lat_str.strip() else None
        if lat is not None and not (-90 <= lat <= 90):
            errors.append("Latitude must be between -90 and 90.")
    except ValueError:
        errors.append("Latitude must be a number.")
    try:
        lon = float(lon_str) if lon_str.strip() else None
        if lon is not None and not (-180 <= lon <= 180):
            errors.append("Longitude must be between -180 and 180.")
    except ValueError:
        errors.append("Longitude must be a number.")

    if modality == "on-site" and (lat is None or lon is None):
        errors.append("On-site jobs need a latitude and longitude.")

    raw_values = {
        "job_id": job_id, "date": date_str, "start_time": start_time_str, "end_time": end_time_str,
        "language": language, "sworn_required": bool(sworn_required_raw), "modality": modality,
        "client": client, "address": address, "city": city, "lat": lat_str, "lon": lon_str,
    }

    if errors:
        return templates.TemplateResponse(
            request, "admin_job_form.html", {"values": raw_values, "is_new": is_new, "errors": errors},
            status_code=400,
        )

    job = Job(
        job_id=job_id, date=job_date, start_time=start_time, duration_min=duration_min, end_time=end_time,
        language=language, sworn_required=bool(sworn_required_raw), modality=modality, client=client,
        address=address.strip(), city=city.strip(), lat=lat, lon=lon,
    )
    store.upsert_job(job)
    store.persist_now()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/jobs/new")
def admin_create_job(
    request: Request,
    job_id: str = Form(...),
    date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    language: str = Form(...),
    sworn_required: str | None = Form(None),
    modality: str = Form(...),
    client: str = Form(...),
    address: str = Form(""),
    city: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
):
    return _save_job_form(
        request, is_new=True, job_id=job_id, date_str=date, start_time_str=start_time, end_time_str=end_time,
        language=language, sworn_required_raw=sworn_required, modality=modality, client=client,
        address=address, city=city, lat_str=lat, lon_str=lon,
    )


@app.post("/admin/jobs/{job_id}/edit")
def admin_update_job(
    request: Request,
    job_id: str,
    date: str = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    language: str = Form(...),
    sworn_required: str | None = Form(None),
    modality: str = Form(...),
    client: str = Form(...),
    address: str = Form(""),
    city: str = Form(""),
    lat: str = Form(""),
    lon: str = Form(""),
):
    return _save_job_form(
        request, is_new=False, job_id=job_id, date_str=date, start_time_str=start_time, end_time_str=end_time,
        language=language, sworn_required_raw=sworn_required, modality=modality, client=client,
        address=address, city=city, lat_str=lat, lon_str=lon,
    )


@app.post("/admin/jobs/delete-all")
def admin_delete_all_jobs():
    store.replace_jobs([])
    store.persist_now()
    return RedirectResponse("/admin?deleted=jobs", status_code=303)


@app.post("/admin/jobs/{job_id}/delete")
def admin_delete_job(job_id: str):
    store.delete_job(job_id)
    store.persist_now()
    return RedirectResponse("/admin", status_code=303)


def _interpreter_values_from(interpreter: Interpreter | None) -> dict:
    if interpreter is None:
        return {
            "interpreter_id": "", "name": "", "language": "", "sworn": False,
            "home_city": "", "home_lat": "", "home_lon": "", "rate_eur_per_hour": "",
            "avail_day1_start": "08:00", "avail_day1_end": "18:00",
            "avail_day2_start": "08:00", "avail_day2_end": "18:00",
        }
    day1_window = interpreter.availability.get(_AVAILABILITY_DAY1)
    day2_window = interpreter.availability.get(_AVAILABILITY_DAY2)
    return {
        "interpreter_id": interpreter.interpreter_id, "name": interpreter.name, "language": interpreter.language,
        "sworn": interpreter.sworn, "home_city": interpreter.home_city, "home_lat": interpreter.home_lat,
        "home_lon": interpreter.home_lon, "rate_eur_per_hour": interpreter.rate_eur_per_hour,
        "avail_day1_start": day1_window[0].strftime("%H:%M") if day1_window else "",
        "avail_day1_end": day1_window[1].strftime("%H:%M") if day1_window else "",
        "avail_day2_start": day2_window[0].strftime("%H:%M") if day2_window else "",
        "avail_day2_end": day2_window[1].strftime("%H:%M") if day2_window else "",
    }


@app.get("/admin/interpreters/new")
def admin_new_interpreter_form(request: Request):
    return templates.TemplateResponse(
        request, "admin_interpreter_form.html",
        {
            "values": _interpreter_values_from(None), "is_new": True, "errors": [],
            "day1": _AVAILABILITY_DAY1, "day2": _AVAILABILITY_DAY2,
        },
    )


@app.get("/admin/interpreters/{interpreter_id}/edit")
def admin_edit_interpreter_form(request: Request, interpreter_id: str):
    return templates.TemplateResponse(
        request, "admin_interpreter_form.html",
        {
            "values": _interpreter_values_from(_get_interpreter_or_404(interpreter_id)),
            "is_new": False, "errors": [],
            "day1": _AVAILABILITY_DAY1, "day2": _AVAILABILITY_DAY2,
        },
    )


def _parse_availability_field(start_str: str, end_str: str, field_label: str, errors: list[str]):
    start_str = start_str.strip()
    end_str = end_str.strip()
    if not start_str and not end_str:
        return None
    try:
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        if end <= start:
            errors.append(f"{field_label}: end time must be after start time.")
            return None
        return (start, end)
    except ValueError:
        errors.append(f"{field_label}: times must be HH:MM, or both left blank for unavailable that day.")
        return None


def _save_interpreter_form(
    request: Request,
    *,
    is_new: bool,
    interpreter_id: str,
    name: str,
    language: str,
    sworn_raw: str | None,
    home_city: str,
    home_lat_str: str,
    home_lon_str: str,
    rate_str: str,
    avail_day1_start: str,
    avail_day1_end: str,
    avail_day2_start: str,
    avail_day2_end: str,
):
    errors: list[str] = []
    interpreter_id = interpreter_id.strip()
    if not is_new and interpreter_id not in store.interpreters:
        # Same rule as jobs: editing a roster entry that no longer exists
        # must not quietly create a new interpreter.
        raise HTTPException(status_code=404, detail=f"Interpreter {interpreter_id} not found")
    if not interpreter_id:
        errors.append("Interpreter ID is required.")
    elif is_new and interpreter_id in store.interpreters:
        errors.append(f"Interpreter ID '{interpreter_id}' already exists.")

    name = name.strip()
    if not name:
        errors.append("Name is required.")
    language = language.strip()
    if not language:
        errors.append("Language is required.")
    home_city = home_city.strip()
    if not home_city:
        errors.append("Home city is required.")

    home_lat = home_lon = rate = None
    try:
        home_lat = float(home_lat_str)
        if not (-90 <= home_lat <= 90):
            errors.append("Home latitude must be between -90 and 90.")
    except ValueError:
        errors.append("Home latitude must be a number.")
    try:
        home_lon = float(home_lon_str)
        if not (-180 <= home_lon <= 180):
            errors.append("Home longitude must be between -180 and 180.")
    except ValueError:
        errors.append("Home longitude must be a number.")
    try:
        rate = float(rate_str)
        if rate <= 0:
            errors.append("Rate must be positive.")
    except ValueError:
        errors.append("Rate must be a number.")

    day1_window = _parse_availability_field(
        avail_day1_start, avail_day1_end, f"Availability {_AVAILABILITY_DAY1}", errors
    )
    day2_window = _parse_availability_field(
        avail_day2_start, avail_day2_end, f"Availability {_AVAILABILITY_DAY2}", errors
    )

    raw_values = {
        "interpreter_id": interpreter_id, "name": name, "language": language, "sworn": bool(sworn_raw),
        "home_city": home_city, "home_lat": home_lat_str, "home_lon": home_lon_str,
        "rate_eur_per_hour": rate_str,
        "avail_day1_start": avail_day1_start, "avail_day1_end": avail_day1_end,
        "avail_day2_start": avail_day2_start, "avail_day2_end": avail_day2_end,
    }

    if errors:
        return templates.TemplateResponse(
            request, "admin_interpreter_form.html",
            {
                "values": raw_values, "is_new": is_new, "errors": errors,
                "day1": _AVAILABILITY_DAY1, "day2": _AVAILABILITY_DAY2,
            },
            status_code=400,
        )

    availability = {}
    if day1_window:
        availability[_AVAILABILITY_DAY1] = day1_window
    if day2_window:
        availability[_AVAILABILITY_DAY2] = day2_window

    interpreter = Interpreter(
        interpreter_id=interpreter_id, name=name, language=language, sworn=bool(sworn_raw),
        home_city=home_city, home_lat=home_lat, home_lon=home_lon, rate_eur_per_hour=rate,
        availability=availability,
    )
    store.upsert_interpreter(interpreter)
    store.persist_now()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/interpreters/new")
def admin_create_interpreter(
    request: Request,
    interpreter_id: str = Form(...),
    name: str = Form(...),
    language: str = Form(...),
    sworn: str | None = Form(None),
    home_city: str = Form(...),
    home_lat: str = Form(...),
    home_lon: str = Form(...),
    rate_eur_per_hour: str = Form(...),
    avail_day1_start: str = Form(""),
    avail_day1_end: str = Form(""),
    avail_day2_start: str = Form(""),
    avail_day2_end: str = Form(""),
):
    return _save_interpreter_form(
        request, is_new=True, interpreter_id=interpreter_id, name=name, language=language, sworn_raw=sworn,
        home_city=home_city, home_lat_str=home_lat, home_lon_str=home_lon, rate_str=rate_eur_per_hour,
        avail_day1_start=avail_day1_start, avail_day1_end=avail_day1_end,
        avail_day2_start=avail_day2_start, avail_day2_end=avail_day2_end,
    )


@app.post("/admin/interpreters/{interpreter_id}/edit")
def admin_update_interpreter(
    request: Request,
    interpreter_id: str,
    name: str = Form(...),
    language: str = Form(...),
    sworn: str | None = Form(None),
    home_city: str = Form(...),
    home_lat: str = Form(...),
    home_lon: str = Form(...),
    rate_eur_per_hour: str = Form(...),
    avail_day1_start: str = Form(""),
    avail_day1_end: str = Form(""),
    avail_day2_start: str = Form(""),
    avail_day2_end: str = Form(""),
):
    return _save_interpreter_form(
        request, is_new=False, interpreter_id=interpreter_id, name=name, language=language, sworn_raw=sworn,
        home_city=home_city, home_lat_str=home_lat, home_lon_str=home_lon, rate_str=rate_eur_per_hour,
        avail_day1_start=avail_day1_start, avail_day1_end=avail_day1_end,
        avail_day2_start=avail_day2_start, avail_day2_end=avail_day2_end,
    )


@app.post("/admin/interpreters/delete-all")
def admin_delete_all_interpreters():
    store.replace_interpreters([])
    store.persist_now()
    return RedirectResponse("/admin?deleted=interpreters", status_code=303)


@app.post("/admin/interpreters/{interpreter_id}/delete")
def admin_delete_interpreter(interpreter_id: str):
    store.delete_interpreter(interpreter_id)
    store.persist_now()
    return RedirectResponse("/admin", status_code=303)
