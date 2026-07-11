"""CLI: run auto-assignment on jobs.csv / interpreters.csv and print/save
the resulting schedule. This is the deliverable #3 output generator.

Usage:
    python run_auto_assignment.py
    python run_auto_assignment.py --out output/auto_assignment_result.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.data_loader import load_interpreters, load_jobs  # noqa: E402
from app.scheduler import run_auto_assignment  # noqa: E402
from app.store import PlanningStore  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a codepage that mangles en-dashes

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(BASE_DIR / "output" / "auto_assignment_result.csv"))
    args = parser.parse_args()

    jobs = load_jobs(BASE_DIR / "jobs.csv")
    interpreters = load_interpreters(BASE_DIR / "interpreters.csv")
    store = PlanningStore(jobs, interpreters)
    run_auto_assignment(store)

    rows = store.jobs_sorted()
    assigned = sum(1 for j in rows if j.job_id in store.assignments)
    unassigned = len(rows) - assigned

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["job_id", "date", "start_time", "language", "sworn_required", "modality", "status", "interpreter_id", "interpreter_name", "reasons"])
        for job in rows:
            interpreter_id = store.assignments.get(job.job_id)
            if interpreter_id:
                interpreter = store.interpreters[interpreter_id]
                writer.writerow([
                    job.job_id, job.date, job.start_time.strftime("%H:%M"), job.language,
                    job.sworn_required, job.modality, "assigned", interpreter_id, interpreter.name, "",
                ])
            else:
                reasons = " | ".join(store.unassigned_reasons.get(job.job_id, []))
                writer.writerow([
                    job.job_id, job.date, job.start_time.strftime("%H:%M"), job.language,
                    job.sworn_required, job.modality, "unassigned", "", "", reasons,
                ])

    print(f"{assigned}/{len(rows)} jobs assigned, {unassigned} unassigned.")
    print(f"Full schedule written to {out_path}")
    print()
    print("Unassigned jobs and reasons:")
    for job in rows:
        if job.job_id not in store.assignments:
            reasons = store.unassigned_reasons.get(job.job_id, [])
            print(f"  {job.job_id} ({job.date} {job.start_time.strftime('%H:%M')}, {job.language}"
                  f"{'/sworn' if job.sworn_required else ''}): {' | '.join(reasons)}")

    print()
    print("Workload per interpreter (assigned minutes):")
    for interpreter in store.interpreters_sorted():
        minutes = store.workload_minutes(interpreter.interpreter_id)
        job_count = len(store.schedule_for(interpreter.interpreter_id))
        print(f"  {interpreter.interpreter_id} {interpreter.name}: {job_count} jobs, {minutes} min")


if __name__ == "__main__":
    main()
