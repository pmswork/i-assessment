# Interpreter Planning — Technical Assessment

Welcome! This is a weekend take-home assignment. It's built around a real problem
we deal with every day at ELAN: getting the right interpreter to the right job.

## Scenario

We run an interpreting agency. Public-sector clients — municipalities, courts,
hospitals, the immigration service — book interpreters for appointments across
the Netherlands. Some appointments are on location, some are remote (phone or
video). Some require a **sworn** (beëdigd) interpreter, e.g. court hearings.

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

Build a small planning application, in **Python and/or JavaScript/TypeScript**
(any frameworks/libraries you like), with two capabilities:

### 1. Auto-assignment

Upload (or otherwise ingest) the two CSV files, and produce a proposed plan:
which interpreter takes which job.

It's possible that not every job is assignable. For every job you cannot place, the plan
must say **why not** — a reason a human planner could act on, not just
"unassigned".

### 2. Manual assignment with validation

In practice, planners often override the machine: a client asks for a specific
interpreter, or plans change mid-day. Build a simple web interface where a
planner can **manually assign an interpreter to a job**. The app must validate
the manual assignment:

- if it's fine → accept it
- if it's questionable → warn, but allow the planner to proceed
- if it can't work → reject it, and say why

What counts as "fine", "questionable", or "can't work" is **yours to define
and defend**.

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
- **Decisions** — where the assignment is ambiguous (it is, on purpose),
  did you make a call and explain it?
- **Working software** — a small thing that runs beats a big thing that almost
  runs. We will run it.
- **Code** we can read.

We do *not* grade: visual polish, algorithmic sophistication (a sensible
heuristic beats a solver you can't explain), or completeness of edge-case
handling — flag what you'd do rather than doing it all.

## Practicalities

- Timebox: this is designed for roughly **6–10 focused hours**. Please don't
  sink your whole weekend into gold-plating; we'd rather see where you chose
  to spend limited time.
- The data is synthetic but shaped like our real workload. Coordinates are
  city centroids; addresses are fictional.
- If you want additional data, infrastructure, or clarification — **ask**.
  Asking good questions is part of the job. (For example: we can provide a
  travel-time matrix between cities on request.)
- LLM/AI tooling: use whatever you normally use. We care about the result and
  whether you can defend every decision in it — you'll walk us through your
  solution in the follow-up conversation.

Good luck — we're looking forward to seeing how you think.
