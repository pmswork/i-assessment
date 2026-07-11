# Interpreter Planning — Technical Assessment

Welcome! This is a take-home assignment. It's built around a real problem
we deal with every day at ELAN: getting the right interpreter to the right job.

## Scenario

Public-sector clients — municipalities, courts,
hospitals, the immigration service — book interpreters for appointments across
the Netherlands. Some appointments are on location, some are remote (phone or
video). Some require a sworn (beëdigd) interpreter, e.g. court hearings.

You are given two days of bookings and our interpreter roster:

- [`jobs.csv`](jobs.csv) — 110 booking requests for Tue 14 & Wed 15 July 2026
- [`interpreters.csv`](interpreters.csv) — our 8 interpreters

### `jobs.csv`

| column | meaning |
|---|---|
| `job_id` | unique id |
| `date`, `start_time`, `duration_min`, `end_time` | when the appointment takes place |
| `language` | language needed (all jobs are Dutch ↔ this language) |
| `sworn_required` | `yes` = only a sworn interpreter may take this job |
| `modality` | `on-site` or `remote` |
| `client`, `address`, `city`, `lat`, `lon` | where (empty for remote jobs) |

### `interpreters.csv`

| column | meaning |
|---|---|
| `interpreter_id`, `name` | who |
| `language` | the language they interpret (with Dutch) |
| `sworn` | whether they are a sworn interpreter |
| `home_city`, `home_lat`, `home_lon` | where they live |
| `rate_eur_per_hour` | what they cost us |
| `availability_…` | working window per day (`—` = not available that day) |

## The assignment

Build a small planning application, in **Python and/or JavaScript/TypeScript** (any frameworks/libraries you like), with two capabilities:

### 1. Auto-assignment

Upload (or otherwise ingest) the two CSV files, and produce a proposed schedule: which interpreter takes which job.

It's possible that not every job is assignable. For every job you cannot place, the plan
must say **why not** — a reason a human planner could act on, not just "unassigned".

### 2. Manual assignment with validation

In practice, we often hold a first-come-first-serve type of auction for interpreters where the first interpreter to accept a job gets assigned. 
Sometimes those interpreters just hit accept without checking whether or not they can actually serve the job.
Build a simple web interface where a planner can **manually assign an interpreter to a job**. 
The app must validate the manual assignment:

- if it's fine → accept it
- if it's questionable → warn, but allow the planner to proceed
- if it can't work → reject it, and say why

What counts as "fine", "questionable", or "can't work" is **yours to define and defend**.

## Deliverables

1. A repository we can run locally (include setup instructions — a
   `docker compose up` or a couple of shell commands, nothing exotic).
2. The output of your auto-assignment on the provided data (file or screen).
3. A short write-up (README section is fine) covering:
   - the rules you enforce, and **why** you chose them
   - what you'd tackle next with more time
   - anything you found questionable or ambiguous in the data, and how you
     decided to handle it

## What we look at

- **Judgment** — did you model the problem the business actually has?
- **Decisions** — where the assignment is ambiguous, did you make a call and explain it?
- **Working software** — a small thing that runs beats a big thing that almost runs.
- **Code** we can read.

We do *not* grade: algorithmic sophistication or completeness of edge-case handling — flag what you'd do rather than doing it all.

## Practicalities

- Timebox: depending on your toolset and approach this could be **2–6 focused hours** of work. Please don't sink your whole weekend into gold-plating; we'd rather see where you chose to spend limited time.
- The data is synthetic but shaped like our real workload.
- If you want additional data, infrastructure, or clarification — **ask**. You might not get an answer that solves your question, but it shows your thought process.
- LLM/AI tooling: use whatever you normally use. We care about the result and whether you can defend every decision in it. We explicitly encourage the use of claude code, cursor or other tools
- You'll walk us through your solution in the follow-up conversation

This is an arbitrarily large assignment with ambiguous instructions and data by design.
We don't expect a production grade solution, but a MVP that works for the important core principles.
You don't have to be "done" to successfully master this assignment.

Good luck and have fun!

---

# Solution

Everything below documents what was actually built: how to run it, how it's
put together, the rules it enforces and why, and what was deliberately left
out.

## Quick start

Requires Python 3.11+ (built and tested on 3.12). No Node, no Docker, no
database server to install — one local SQLite file, `app.db`, is
auto-created on first run for everything that needs to survive a restart
(see "Data model / database schema"); the planning logic itself runs
in-memory.

```bash
cd i-assessment
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux

pip install -r requirements.txt

# 1. Produce the auto-assignment output for the supplied CSVs (deliverable #2)
python run_auto_assignment.py
# -> prints a summary to the console and writes output/auto_assignment_result.csv

# 2. Run the tests
pytest -q

# 3. Start the planner web app
uvicorn app.main:app --reload
# -> http://127.0.0.1:8000
```

Main pages:

- `http://127.0.0.1:8000/` - assignment board
- `http://127.0.0.1:8000/jobs/{job_id}` - job detail and manual validation
- `http://127.0.0.1:8000/interpreters` - interpreter roster
- `http://127.0.0.1:8000/interpreters/{interpreter_id}` - interpreter profile, schedule, reliability
- `http://127.0.0.1:8000/admin` - CSV import/export and job/interpreter CRUD
- `http://127.0.0.1:8000/settings` - planner-adjustable thresholds

**First run**: the web app loads `jobs.csv` and `interpreters.csv` from the
repo root, runs auto-assignment, and immediately persists everything to
`app.db` — the job list is populated the moment you open the page.
**Every run after that**: it loads from `app.db` instead (admin edits,
manual assignments, and settings changes all survive a restart). The
supplied CSVs are only ever read again if you explicitly re-import them
from the Admin tab, or if you delete `app.db` and restart. `run_auto_assignment.py`
(the CLI) always reads straight from the CSVs and never touches `app.db`,
so it's unaffected by whatever state the web app is in.

## Architecture

```
app/
  models.py       Interpreter / Job dataclasses — no framework dependencies
  data_loader.py  CSV -> models (strict/fail-fast; used at first-run seeding
                   and by the CLI)
  csv_io.py       CSV -> models for the Admin tab's import feature — same
                   data, but validates every row and collects errors instead
                   of raising, and serializes models back to CSV for export
  travel.py       haversine distance (km) + straight-line -> minutes conversion,
                   kept as two separate primitives (replaceable, no network calls)
  settings.py     planner-adjustable thresholds (see "Settings" below) — a
                   single mutable object every rule/scheduler/coverage
                   function reads at call time, so a change takes effect
                   immediately
  rules.py        THE business rules: check_hard_constraints, check_warnings,
                   validate_assignment, is_qualified — pure functions, fully
                   unit-testable
  coverage.py     resource-scarcity signal: qualified interpreters within the
                   configured radius of a job (see "Coverage indicator"
                   below) — informational, not a validation rule
  reliability.py  interpreter track record (see "Reliability scoring" below)
  db.py           the SQLite schema and read/write functions for jobs,
                   interpreters, assignments, and settings (see "Data model"
                   below) — reliability.py manages its own table in the same
                   file
  scheduler.py    greedy auto-assignment; calls rules.validate_assignment,
                   never re-implements a rule of its own
  store.py        in-memory PlanningStore: jobs, interpreters, current
                   assignments (+ auto/manual source), and the schedule/
                   workload lookups rules.py needs — writes through to db.py
                   when `persist=True`
  main.py         FastAPI routes + Jinja2 templates (thin — no business logic)
  templates/, static/
run_auto_assignment.py   CLI entry point for deliverable #2 (CSV in, never touches app.db)
tests/                   pytest, construct Interpreter/Job directly (tests/factories.py);
                          conftest.py isolates reliability.py and db.py from the real
                          app.db/reliability.db so test runs and a developer's local
                          manual testing can never cross-contaminate; integration tests
                          against the real CSVs; route-level tests via FastAPI's TestClient
```

The key design decision: **auto-assignment and manual assignment share one
validator** (`rules.validate_assignment`). The scheduler just calls it in a
loop and asks "is anyone ACCEPTED or WARNING?"; the web UI calls it once per
click. They cannot drift apart, and every reason a planner sees for a manual
rejection is generated by the same code that decided a job was unassignable
during auto-assignment.

## Assignment strategy

Jobs are sorted before being placed, most scarce/constrained first:

1. sworn jobs before non-sworn (sworn interpreters are the scarcest resource
   — only one per language, and one language, Polish, has none at all)
2. then by number of statically eligible interpreters (language + sworn
   match, ignoring time/travel) — the harder a job is to fill, the earlier
   it gets first pick of interpreters
3. then chronologically, then by `job_id` for a stable, deterministic order

For each job, every interpreter is run through `validate_assignment`.
Anyone REJECTED is dropped. Among the rest (ACCEPTED or WARNING), the best
candidate is chosen by, in order: no warnings beats warnings → higher
reliability score (`reliability.py` — see below; track record ranks ahead
of cost on purpose) → cheaper hourly rate → less travel added (minutes,
then km as a finer-grained tie-break between otherwise-equal legs) → lower
current workload → `interpreter_id` (tie-break only, for determinism).
This directly follows the brief's guidance to protect scarce resources and
not blindly optimize one metric — reliability, cost, travel/distance and
workload all factor into the tie-break chain rather than one dominating,
and none of them are ever allowed to override a hard constraint (REJECTED
candidates are dropped before any of this ranking runs).

This is a single greedy left-to-right pass, not a global optimum (see
Trade-offs). Running it twice on the same input always produces the same
schedule — verified by a test.

When a planner re-runs auto-assignment from the board, manual assignments
are preserved. The automatic pass clears and rebuilds only its own `auto`
placements, fills the remaining open jobs, and never silently replaces a
human override. If a manual assignment has become impossible after an admin
edit, it is moved back to "needs decision" with an explicit reason instead
of being replaced behind the planner's back.

### Result on the supplied dataset

```
96/110 jobs assigned, 14 unassigned.
```

Every one of the 14 unassigned jobs has a specific, actionable reason
(never a generic "no match"), e.g.:

- `J013` — *"This job requires a sworn Polish interpreter, but no sworn
  Polish interpreter exists on the roster."* (a genuine data gap, see below)
- `J057` — *"All 3 qualified Arabic interpreter(s) are outside their
  working hours ... for the 19:30–20:30 slot."* (job starts after every
  interpreter's window closes)
- `J017`, `J054`, `J055`, `J062`, `J065`, `J071`, `J081`, `J094` — *"...
  cannot reach this job in time from a neighbouring appointment
  (insufficient travel time)."*
- `J010`, `J033`, `J082`, `J097` — *"... already booked on another job that
  overlaps this time slot."*

Full per-job output: run `python run_auto_assignment.py` or see
`output/auto_assignment_result.csv`.

## Reliability scoring — track record beats "first to say yes"

The brief for this feature (added after the initial MVP) was explicit that
in practice, jobs are often filled through a first-come-first-served
accept/decline auction, and that this rewards speed, not dependability.
`reliability.py` tracks the outcomes that should matter instead:

- an **ACCEPTED** event is logged when a planner confirms a manual
  assignment
- **COMPLETED** / **NO_SHOW** / **LATE_CANCELLATION** events are logged
  when a planner records what actually happened, from the job detail page
- each outcome carries points (`COMPLETED +3`, `NO_SHOW -6`,
  `LATE_CANCELLATION -4`, `DECLINED -1`, `ACCEPTED 0`) that sum to a score
  per interpreter, shown on their profile page (`/interpreters/{id}`)

That score is the scheduler's second tie-break, ranked **above cost** (see
"Assignment strategy" above) — a proven interpreter should win a tie
against a cheaper-but-unproven one. It cannot override a hard constraint,
and — by construction, not a special case — it cannot change anything
when only one interpreter is eligible for a job in the first place: there's
no one to tie-break against. History still gets recorded either way,
exactly as asked, in case it becomes useful once there's more competition
for that language.

**Storage**: a single SQLite file (`reliability.db`, auto-created, gitignored)
— no ORM, no migrations. It's deliberately separate from the in-memory
`PlanningStore`: assignments reset on restart, but reliability history
persists across restarts, since it represents real-world track record, not
session state.

Current implementation note: the persistence layer has since been unified
around `app.db`. `PlanningStore` still keeps the hot scheduling state in
memory for speed, but jobs, interpreters, assignments, settings,
unassigned reasons, and reliability events are written through to SQLite
and survive a restart.

**Honest scope limit**: this app doesn't have a real multi-interpreter
"broadcast a job, first to accept wins" auction UI (yet), so there is
nothing to measure response time against today. The event schema has a
`response_seconds` field and the scoring formula already has a (currently
inert, always-zero) fast-response bonus ready for it — see "what I'd build
next". What's real today is the outcome tracking (completed vs. no-show vs.
late-cancelled) and its effect on ranking, which is the core of what was
asked: reward good outcomes over just being fast to accept.

## Data model / database schema

Everything durable lives in one SQLite file, `app.db` (`db.py`):

```
interpreters(interpreter_id PK, name, language, sworn, home_city, home_lat,
             home_lon, rate_eur_per_hour)
interpreter_availability(interpreter_id FK→interpreters, day, window_start,
             window_end, PK(interpreter_id, day))
jobs(job_id PK, date, start_time, duration_min, end_time, language,
             sworn_required, modality, client, address, city, lat, lon)
assignments(job_id PK FK→jobs, interpreter_id FK→interpreters, source)
unassigned_reasons(job_id PK FK→jobs, reasons_json)
reliability_events(id PK, interpreter_id, job_id, event_type, occurred_at,
             response_seconds)               -- owned by reliability.py
settings(key PK, value)                       -- owned by settings.py
```

**Why this shape, for this MVP specifically:**

- **Speed.** The scheduling/validation hot path (`rules.py`, `scheduler.py`)
  never queries this database — it reads the in-memory `PlanningStore` dict,
  which is faster than any database roundtrip could be at this data volume
  (8 interpreters, ~100 jobs). `app.db` exists purely for durability:
  `PlanningStore.persist_now()` writes the *whole current state* through in
  one transaction, called explicitly after a batch of changes (once at the
  end of auto-assignment, once per manual admin action) rather than on
  every individual mutation — a deliberate trade-off explained in `db.py`'s
  module docstring. Indexes exist on the columns actually filtered/joined
  on (`language`, `date`, assignment `interpreter_id`) — at tens to low
  hundreds of rows they don't measurably matter yet, but they cost nothing
  and document the intended access pattern.
- **Security, scoped honestly to what a local, single-user, no-auth MVP can
  claim.** Every query is parameterized (no SQL injection surface, ever —
  see `db.py`). Foreign keys and `CHECK` constraints enforce basic
  integrity at the database layer, not just in Python (a rate ≤ 0 or a
  `modality` outside `{on-site, remote}` can't reach the table even if a
  future caller forgets to validate). CSV import (`csv_io.py`) validates
  every row before any write and applies atomically — one bad row rejects
  the whole file, never a half-imported dataset. What this does **not**
  provide — authentication, authorization, encryption at rest, audit
  logging — mirrors the brief's own instruction to skip auth/production
  hardening for this assessment; see "known limitations".
- **One file, several owners.** `db.py` owns the jobs/interpreters/
  assignments/settings tables; `reliability.py` manages its own
  `reliability_events` table in the same file (it predates `db.py` and
  keeps its own connection helper, since its access pattern — one small
  table, read on every scheduler ranking call — doesn't need the full-
  resync machinery the other tables use). One physical database, not one
  Python module, so there's still a single coherent schema to reason about
  or back up.
- **Full resync, not targeted upserts.** `sync_store()` deletes and
  reinserts the whole dataset in one transaction rather than diffing and
  upserting individual rows. Simple to reason about and correct — no
  partial-update bugs — and cheap enough at this scale. It would not scale
  to a real multi-tenant deployment with a much larger roster/job volume,
  which would want targeted writes instead; flagged, not hidden (see "what
  I'd build next").

## Settings

The Settings tab (`/settings`) exposes every threshold this app makes a
judgment call with, so a planner can tune them instead of trusting a
hardcoded number baked in at build time:

| Setting | Used for |
|---|---|
| Average travel speed (km/h) | straight-line distance → estimated travel minutes (`travel.py`) |
| Fixed overhead per trip (min) | added to every travel estimate for parking/walking in |
| Tight-travel warning buffer (min) | how much slack beyond the bare minimum is still a warning, not a clean accept |
| Long-distance warning threshold (km) | one-way home-to-job distance that triggers a warning |
| Workload imbalance threshold (min) | how far above the least-loaded alternative triggers a warning |
| Coverage radius (km) | straight-line radius used by the Coverage indicator |
| Coverage gauge cap (count) | nearby-interpreter count that reads as "full" on the gauge |

Every rule/scheduler/coverage function reads `settings.get()` at call
time — never a value cached from import or from the start of a request —
so a save on the Settings tab applies to the very next `/validate` call or
auto-assignment run, no restart needed. Changes are validated (everything
here is a positive, physically meaningful quantity) and persisted to the
`settings` table in `app.db`, so they survive a restart too.

## Admin

The Admin tab (`/admin`) is a deliberate escape hatch, not the primary
workflow — the app is meant to run itself from the supplied CSVs (see
"Manual assignment is the fallback" below, which applies here too: most of
the time a planner should never need this page). It covers:

- **Import**: upload a replacement `jobs.csv` or `interpreters.csv`.
  Validated row-by-row with specific, row-numbered reasons
  (`csv_io.parse_jobs_csv` / `parse_interpreters_csv`); a single bad row
  rejects the *whole* file and leaves the current data untouched — never a
  half-applied import.
- **Export**: download the current jobs/interpreters as CSV, in the same
  format the app ingests — so an export can always be re-imported, and
  external tools (spreadsheets) can round-trip through the app.
- **Edit / delete**: a plain form per job or interpreter. Editing a job
  drops its existing assignment (the new data might not be compatible with
  it — re-assigning is a deliberate decision, not something to carry over
  silently). Deleting an interpreter frees whatever they were assigned to
  rather than leaving a dangling reference.

Every admin mutation calls `store.persist_now()`, so it's durable across a
restart exactly like a manual assignment is.

## Hard constraints (reject, always)

| # | Rule | Why hard |
|---|------|----------|
| 1 | Interpreter's language must match the job's language | Not negotiable — they can't interpret a language they don't speak. |
| 2 | `sworn_required = yes` → interpreter must be sworn | Legal requirement for court/official hearings; a non-sworn interpreter's output isn't admissible. |
| 3 | Job time must fall entirely within the interpreter's stated availability window for that date | They told us when they work; scheduling outside it isn't something a planner can silently override. |
| 4 | No overlapping jobs for the same interpreter | A person can't be in two appointments at once, on-site or remote. |
| 5 | Travel between two of the interpreter's on-site jobs that day must physically fit in the gap | If the estimated drive time exceeds the gap, the assignment is not just risky, it's impossible. |

Constraint 5 is evaluated on the interpreter's whole day, not just the pair
being validated: on-site jobs are chained chronologically (skipping over
remote jobs, which don't require travel), so inserting a new job re-checks
the legs it actually touches.

## Warnings (soft — proceed allowed, with an explanation)

| Rule | Why soft, not hard |
|------|--------------------|
| Travel between on-site jobs is feasible but tight (gap is within 15 min of the bare minimum required) | 15 minutes is an arbitrary-but-documented buffer for the estimate being wrong, not a hard physical limit. |
| A remote job is wedged into a gap that's mostly needed for travel between two on-site jobs | Technically fits on paper, but in practice the interpreter would be trying to take a call while driving. Flag it, let a human judge whether that's realistic for this client. |
| Commute from home to the first on-site job, or from the last on-site job back home, is tight or doesn't fit the working window | We only know when an interpreter is *willing to work*, not when they leave their house. Treating this as hard would incorrectly block otherwise-fine assignments for interpreters who simply commute before their stated window opens. |
| A cheaper qualified, available interpreter exists for this job | Never blocks — sometimes the pricier interpreter is a deliberate choice (relationship with the client, specialism) — but the planner should see the cost trade-off. |
| Assigning this job would leave the interpreter far more loaded (≥120 min) than the least-loaded other qualified, available interpreter | Workload balance is an operational preference, not a constraint — some days are just uneven. |
| Long one-way distance from home to an on-site job (> 40 km) | Feasible and possibly not even tight on time, but expensive/inefficient and worth a second look. |

None of these ever block a save; they're surfaced so the planner makes an
informed override, per the brief.

## Travel-time approximation

No routing API is available, so travel time is estimated from straight-line
(haversine) distance:

```
travel_minutes = distance_km / 45 km/h * 60  +  10 min fixed overhead
```

45 km/h is a deliberately conservative average for the Netherlands (a mix
of city streets, provincial roads, and short highway hops between the towns
in this dataset — real point-to-point average speeds by car in NL are
usually higher, but straight-line distance already undercounts actual route
length, so a lower speed partly compensates). The 10-minute overhead covers
parking/walking in (or dialling in, for the rare case this is used near a
remote leg).

`travel.py` keeps two separate, explicit primitives rather than one
opaque function: `haversine_km(a, b)` for the raw straight-line distance,
and `travel_minutes_for_distance(distance_km)` for the speed-based minutes
conversion (`estimate_travel_minutes` is a convenience wrapper over both).
Splitting them means anything that only cares about distance — like the
coverage indicator below — doesn't have to pull in the speed assumption at
all. **To be explicit: none of this is real routing.** No routing API, no
network call, no road network. It's a documented, replaceable
approximation — swap `travel_minutes_for_distance` for a real routing
service's response and every caller (rules, scheduler, coverage) keeps
working unchanged.

## Coverage indicator

The job list has a **Coverage** column: a signal-strength-style gauge (3
small bars, like a phone signal or battery icon) plus a compact number,
e.g. `2/3 ≤50km` or `3 qualified (remote)`. It answers a narrower question
than full validation: *"of the interpreters who are language/sworn-
qualified for this job, how many live within the coverage radius
(straight-line) of it?"*

This went through one visible revision: the first version was a single
continuous fill bar, which read as ambiguous at a glance (it wasn't clear
what "full" meant, or that it was counting people at all). The gauge
version is deliberately literal — discrete bars that light up one at a
time, colour-coded red/amber/green — because that's a shape people already
recognize from phone signal and battery icons, so it needs no legend. The
precise number next to it is what actually carries the information; the
gauge just makes 100 rows scannable at a glance.

- For **on-site** jobs: `within_radius / qualified_total`, both computed
  from home coordinates to the job's coordinates via the same
  `haversine_km` used for travel estimates. Zero lit bars (red/empty) with
  a nonzero `qualified_total` means qualified interpreters exist but none
  of them are nearby — travel distance, not scheduling, is the likely
  constraint for this job.
- For **remote** jobs: distance doesn't apply (there's no location to be
  near), so the gauge/count reflect the qualified count instead.
- If **zero** interpreters are qualified at all (wrong language, or sworn
  required with none sworn), the column shows a greyed, hatched "N/A"
  gauge and `0 qualified` rather than a confusing `0/0` — that's a
  different, already-flagged problem (see the Polish-sworn gap below), not
  a coverage gap.
- The gauge caps its *display* at 3 bars (`coverage.COVERAGE_BAR_CAP`,
  configurable in Settings) — 5 nearby qualified interpreters and 3 both
  show as "full"; it's a quick-scan indicator, not a precise count. The
  adjacent label always shows the real number.

This is deliberately a **simple, static, approximate signal** — it ignores
availability, existing bookings, and exact travel time (those are what
`rules.py`'s full hard-constraint check is for). It's meant to answer "is
this job in a thin part of the map for this language?" at a glance, not to
replace validation. The radius and gauge cap are planner-adjustable in
Settings, not hardcoded (see "Settings" above).

The same signal feeds into `scheduler._explain_unassigned`: when a job
ends up unassigned *and* has zero qualified interpreters within 50 km, the
reported reasons include an extra note calling that out — alongside, not
instead of, the concrete blocking reason (e.g. "already booked" or
"outside working hours"), so a planner sees both the immediate cause and
the underlying scarcity.

## Manual assignment is the fallback, not the default path

The product goal here is autonomy: auto-assignment should handle the large
majority of jobs on its own, and a human should only ever be pulled in for
the genuinely hard cases. A few concrete things follow from that on the
job-detail page:

- **The candidate list only shows interpreters who meet the basic
  language/sworn requirement.** A Polish job never lists an Arabic
  interpreter — there's no reason to make a planner scroll past options
  that can only ever be REJECTED. If literally nobody meets the basic
  requirement (the Polish-sworn gap being the concrete example in this
  dataset), the panel says so directly instead of rendering an empty table.
- **A "Situation" callout leads the page**, not a form. For an already-
  assigned job it's one line ("Assigned to X, auto/manual — no action
  needed"). For an unassigned job it leads with the concrete blocking
  reason(s), then a **suggested override** if one exists: the same ranking
  `run_auto_assignment` uses (`scheduler.best_candidate_for`), computed
  live against current state, so it can surface a WARNING-tier option a
  planner might reasonably choose to override — with the specific
  trade-off named, not just a name. If nobody qualifies at all, or nobody
  is currently placeable, it says that plainly instead of showing a
  hopeful-looking empty form.
- **Interpreter names are links, with a quick-info mark next to them.**
  Clicking a name opens `/interpreters/{id}`: basic info, current
  schedule, and full reliability history. Hovering the "i" mark shows the
  same basic facts (language, rate, home city, current workload) inline,
  for the common case where a planner just needs a fast gut-check, not a
  full page load.
- **Auto vs. manual is visible everywhere an assignment is shown** (job
  list badge, job detail, interpreter profile's schedule), so a planner
  scanning the board can immediately tell which jobs the system placed
  itself and which were human overrides.
- **Job rows are clickable — no separate "review" link.** The whole row
  (a "stretched link" from the job-id cell, see `style.css`) opens the job
  detail page; interpreter-name links inside the row stay independently
  clickable on top of it. One fewer thing to visually parse per row, and
  one less place to precisely aim a click.
- **Every blocking reason names the specific interpreter and the specific
  shortfall, never a generic summary.** This was a direct fix: the reason
  cascade used to collapse everyone who failed the same *category* of
  check into one sentence ("all qualified interpreters are already
  booked"), discarding detail `rules.check_hard_constraints` had already
  computed. It now surfaces that detail as-is — e.g. *"Not enough travel
  time for Amira El-Sayed between J012 (Amsterdam, 10:00–10:45) and J017
  (Rotterdam, 11:00–12:00): needs ~86 min, only 15 min available"* — so a
  planner sees exactly who was considered and exactly why each one didn't
  work, on both the job-detail page and (truncated to 2, with a "+N more"
  link) the job list.

## Assumptions and how ambiguity was resolved

- **`availability_<date>` = "—" or blank means not working that day at
  all.** Applied literally; e.g. `INT-05` only has a morning window
  (08:00–14:00) on 2026-07-15.
- **The stated availability window is when an interpreter is willing to
  *work*, not when they leave home.** This resolves the "hard vs soft"
  question for home commute (see Warnings table above) — treated as soft.
- **Same-city jobs share identical `lat/lon`** in the supplied data (the
  CSV gives city-level, not per-address, coordinates). This means travel
  time between two jobs in the same city is always estimated as the fixed
  10-minute overhead, even though the actual street addresses differ. This
  under-estimates true intra-city travel; documented as a known
  approximation, not silently "fixed" by inventing per-address geocoding.
- **Remote jobs have no location and require no travel**, but still occupy
  the interpreter's calendar (no double-booking) and can be sandwiched
  awkwardly between two on-site jobs — handled as a warning, not a block
  (see Warnings table).
- **Validating a job that's already assigned excludes that job from its
  own "current schedule"** so re-checking an existing assignment behaves
  identically to checking a brand new one (used when a planner reopens an
  assigned job to reassign it).
- **In-memory state, reset on restart.** No persistence layer was built —
  see Known limitations.

Current implementation note: this is no longer true. The MVP now persists
jobs, interpreters, assignments, settings, unassigned reasons, and
reliability events to `app.db`; scheduling still runs in memory during a
request and syncs after mutations.

## Data I found questionable

- **`J013` requires a sworn Polish interpreter, but the roster has exactly
  one Polish interpreter (`INT-08`, Piotr Nowak) and he is not sworn.**
  This isn't a scheduling conflict, it's a structural gap — no auto-
  assignment strategy can fix it. The app reports it as a specific,
  distinct reason at both the roster level (an `_explain_unassigned` check
  that runs *before* checking availability/overlap/travel) and it will
  also correctly REJECT any manual attempt to force Piotr onto that job.
  In real life this is a staffing/roster problem to flag to management, not
  a planning bug.
- **`J057` starts at 19:30**, half an hour after the latest interpreter
  availability window closes (18:00, for everyone). Either this is a data
  entry error (an out-of-hours emergency booking that should have an
  on-call interpreter, which doesn't exist in this roster) or a genuine gap
  in coverage. Handled as a normal "outside working hours" hard constraint
  rather than special-cased, since nothing in the data says it should be
  treated differently.
- **`INT-07`'s day-1 window starts at 13:00** while every other interpreter
  starts at 08:00 — taken at face value (maybe a part-time contract), but
  it does mean Turkish coverage before 13:00 on day 1 rests entirely on the
  one sworn Turkish interpreter (`INT-06`), which is part of why several
  early Turkish on-site jobs end up unassigned or tightly packed.
- **Duplicate/near-duplicate remote job blocks** (e.g. many `Jeugdzorg –
  beeldbellen` entries with different times) look like legitimate distinct
  bookings rather than data errors, so all are treated as independent jobs.

## Trade-offs

- **Greedy, not globally optimal.** A single left-to-right pass can leave a
  job unassigned that a full re-optimization (e.g. min-cost flow across the
  whole schedule) might have squeezed in by reshuffling earlier picks. The
  brief explicitly asks for something "operationally plausible and easy to
  explain" over algorithmic sophistication, so greedy-with-good-tie-breaks
  was chosen deliberately over a solver.
- **Straight-line + flat speed for travel, not a routing engine.** Cheap,
  deterministic, replaceable — but can be wrong in either direction (rivers,
  highways, one-way systems aren't modelled).
- **In-memory store instead of a database.** Correct for a stateless demo
  session; wrong for a real multi-planner, multi-shift tool (see below).
- **Server-rendered forms, no client-side JS.** Keeps the app dependency-
  free and easy to read end-to-end, at the cost of a full page reload per
  "Check" / "Confirm" click.
- **Reliability scoring is deliberately shipped without the invitation/
  accept-decline flow it would eventually plug into.** Building a
  multi-interpreter broadcast-and-race UI just to have somewhere to record
  a response time would have been scope creep for what was flagged as a
  maybe-not-MVP feature; instead the ledger, scoring, and scheduler
  integration are real and tested, and the response-time field is
  deliberately inert until that flow exists (see "what I'd build next").

## Known limitations

- No concurrency control: two planners assigning the same job at once could
  race (last write wins), and `sync_store()`'s full-table-replace approach
  means two *simultaneous* admin edits could clobber each other rather than
  merge. Fine for a single-user demo, not for production.
- No database migrations framework — `db.py` only ever runs
  `CREATE TABLE IF NOT EXISTS`, so a schema change during development means
  deleting `app.db` and re-seeding from the CSVs, not an upgrade path. Fine
  for an MVP with one schema version; a real deployment would need
  Alembic or hand-written migrations.
- `sync_store()`'s full delete-and-reinsert-everything strategy (see "Data
  model" above) is simple and correct at this data volume but would not
  scale to a much larger roster/job count, which would want targeted
  upserts instead.
- Inserting a new assignment re-validates the legs it touches, but does not
  retroactively re-check whether it silently breaks a *different*,
  already-assigned job's travel feasibility later that day (e.g. shifting
  what used to be a direct A→C gap into A→B, B→C is handled; a brand new
  on-site job squeezed in between two jobs where an already-scheduled
  *remote* job sits is not re-verified). Flagged rather than fixed, per the
  brief's "flag what you'd do rather than doing it all."
- No authentication/roles — any visitor can assign/unassign, edit/delete
  jobs and interpreters, or change settings, as expected for an internal
  planning MVP per the brief (no auth requested). The Admin tab in
  particular would need at least a confirmation step and a permission
  model before this could be anything but a single-trusted-user tool — see
  "what I'd build next".
- CSV import (`csv_io.py`) is fixed to the two dates in the supplied
  dataset (`availability_2026-07-14`, `availability_2026-07-15`), matching
  the CSVs the app ships with — not a generic, date-agnostic importer. A
  real multi-week roster tool would need a different interpreter-
  availability schema (see "what I'd build next").
- Distance/travel/workload/coverage thresholds now live in Settings
  instead of hardcoded constants (see "Settings" above) — but their
  *default values* and the reliability point deltas (`+3` completed, `-6`
  no-show, `-4` late-cancellation, `-1` declined) are still reasonable,
  documented guesses, not derived from real operational or cost data.
- The coverage indicator counts interpreters within a fixed radius, not
  "reachable within N minutes" — a fast motorway 55 km away and a slow
  40 km cross-town route are treated as "in" vs. "out" the same crude way
  travel-time estimation is elsewhere in this MVP.
- **Reliability scoring has no real invitation/accept-decline auction to
  measure response time against**, so `response_seconds` is always `None`
  today and that part of the formula is inert. What's real is outcome
  tracking (completed / no-show / late-cancellation) feeding the scheduler
  tie-break — see "Reliability scoring" above for the honest breakdown of
  what's wired up vs. what's schema-ready-but-unused.
- Auto-assignment doesn't log an ACCEPTED reliability event for jobs it
  places itself (only manual confirmations and recorded outcomes do) —
  see "what I'd build next".
- The Admin tab's delete buttons use a single native browser `confirm()`
  dialog to guard against accidental data loss — the only client-side JS
  in the app. Everything else is plain server-rendered forms.

## What I'd build next with more time

1. **A real invitation/accept-decline flow** — broadcast a job to qualified
   interpreters and record who accepts and how fast. This is the missing
   piece that would make `response_seconds` (already in the reliability
   schema) real instead of always-null, and would let the reliability score
   actually replace a first-come-first-served auction rather than just
   out-ranking it inside this app's own greedy assignment.
2. **Auto-log an ACCEPTED reliability event when auto-assignment places a
   job**, guarded against re-logging on every re-run (e.g. diff old vs.
   new assignments and only log genuinely new ones) — right now that
   signal only exists for manual confirmations.
3. **Re-validate the whole affected day**, not just the touched legs, when
   an assignment changes, and surface a warning if it silently invalidates
   a *different* existing assignment.
4. **A real routing/distance-matrix API** behind the same `travel.py`
   interface, with the haversine approximation kept as an offline fallback —
   this would also sharpen the coverage indicator from "within 50 km as the
   crow flies" to "reachable within N minutes by road."
5. **Basic auth + an audit log for the Admin tab** — who imported what,
   who edited/deleted which job or interpreter, and when. Not needed for a
   single-planner demo, but the first thing a real multi-user deployment
   would need before this tab could be trusted with real operational data.
6. **A date-agnostic availability schema**, so CSV import/admin edits
   aren't pinned to the two dates in the supplied dataset — needed before
   this could handle a real, rolling multi-week roster.
7. **Targeted upserts instead of full-table resync** in `db.py`, once the
   roster/job volume is large enough that rewriting everything on every
   save actually matters.

## Test results

```
$ pytest -q
........................................................................ [ 70%]
..............................                                           [100%]
102 passed in 4.19s
```

102 tests across eleven files:

| File | Covers |
|---|---|
| `test_travel.py` | haversine distance, minutes conversion |
| `test_rules.py` | every hard constraint individually, all warning types |
| `test_coverage.py` | coverage counting, the signal-gauge level/segment logic |
| `test_settings.py` | validated updates, reset, and that a change actually changes downstream behaviour (not just the stored value) |
| `test_reliability.py` | neutral-with-no-history, completed/no-show/late-cancellation point deltas, per-interpreter event isolation |
| `test_db.py` | full sync/load round-trip for jobs, interpreters, assignments, reasons, settings; explicit-path override; cascade-delete behaviour |
| `test_csv_io.py` | valid-file parsing, missing-column/bad-row/duplicate-id rejection, atomic all-or-nothing import, export → re-import round-trip |
| `test_store.py` | admin CRUD methods (upsert/delete/replace) and their cascade effects on assignments |
| `test_scheduler.py` | reliability and distance tie-breaks (incl. that a single-candidate job is structurally unaffected by reliability data), integration checks against the real CSVs (every job assigned or has a reason, nobody double-booked, deterministic across runs, the Polish-sworn gap surfaced, and a regression guard on total assigned/unassigned counts) |
| `test_main.py` | every route: qualified-only candidate filtering, rejected assignments never persisting even via direct POST, the manual-assign → outcome → profile flow, settings validation, and the full admin CRUD/import/export flow |

Current additions in this review pass: route coverage now includes the
interpreter roster (`/interpreters`) and invalid URL 404 behaviour; manual
warning assignments require an explicit confirmation flag before they can
persist; scheduler tests cover manual assignments surviving auto-runs and
invalid manual assignments becoming explicit human decisions instead of
being silently overwritten.

Two isolation fixtures in `tests/conftest.py` (`isolated_reliability_db`,
`isolated_app_db`) point every test at a fresh temp SQLite file instead of
the real `reliability.db`/`app.db` the running app uses — without them, a
developer's local manual testing (or test execution order) could leak into
the scheduler's reliability tie-break and make assertions about who gets
picked flaky. A third (`reset_settings`) resets the settings singleton
around every test for the same reason. This was tightened once already
during development: `PlanningStore` initially captured a concrete database
path once at construction time, which meant `app.main`'s module-level
store (built at import time) would have silently ignored the isolation
fixture for any route-triggered write during a test. It now re-resolves
`db.DEFAULT_DB_PATH` on every `persist_now()` call instead — see
`store.py`'s module docstring.

Also run and manually exercised end-to-end via the browser/`curl`/`httpx`:
clean accept, warning-with-confirm (persists), rejected (never persists,
even if posted directly to the confirm endpoint), settings update
affecting a live travel estimate, full admin create/edit/delete for both
jobs and interpreters, CSV import validation (both rejected and accepted),
and state surviving a full server restart.
