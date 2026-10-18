"""Record the outcome of one job. The agent does this through the record_outcome MCP tool
(scripts/applier_mcp.py); this CLI is for fixing things up by hand.

Examples:
  python3 scripts/record.py --id gh:123 --status submitted --company Stripe \
      --title "Software Engineer" --url https://... --reason "Applied by hand"
  python3 scripts/record.py --id wd:acme:R123 --status requeued --company "Acme" \
      --title "Software Engineer Intern" --url https://... --reason "Added address to profile"
"""

import argparse
import datetime as dt
import json

from common import APPLICATIONS, DATA, counted_today, effective_mode, limit, load_applications, load_search_config

STATUSES = ["submitted", "dry_run", "skipped", "needs_human", "error"]
# Only set by hand (this CLI), never by the agent: makes a finished job eligible again.
MANUAL_STATUSES = ["requeued"]


def record(id, status, company, title, url, reason, fit=None, notes=""):
    """Append one outcome and return a message saying how many applications remain this run."""
    if status not in STATUSES + MANUAL_STATUSES:
        raise ValueError(f"status must be one of {STATUSES + MANUAL_STATUSES}")
    run_start = _run_start()  # unset when run by hand outside a run
    if run_start and any(r["id"] == id for r in load_applications() if r["at"] >= run_start):
        return f"{id} was already recorded this run; not recording it again. Move on to the next job."

    row = {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "id": id,
        "status": status,
        "company": company,
        "title": title,
        "url": url,
        "fit": fit,
        "reason": reason,
        "notes": notes,
    }
    DATA.mkdir(exist_ok=True)
    with APPLICATIONS.open("a") as f:
        f.write(json.dumps(row) + "\n")

    cfg = load_search_config()
    rows = load_applications()
    per_day, per_run = limit(cfg, "max_applications_per_day"), limit(cfg, "max_applications_per_run")
    caps = []
    if per_day is not None:
        caps.append(per_day - counted_today(rows, effective_mode(cfg)))
    if per_run is not None:
        caps.append(per_run - sum(1 for r in rows if r["status"] in ("submitted", "dry_run") and r["at"] >= run_start))
    if not caps:
        return f"Recorded {status} for {id}. No application limit: continue with the next job."
    left = max(0, min(caps))
    msg = f"Recorded {status} for {id}. Applications remaining this run: {left}"
    if left == 0:
        msg += "\nApplication limit reached: record nothing further and finish the run now."
    return msg


def _run_start():
    try:
        return (DATA / ".run_started_at").read_text().strip()
    except FileNotFoundError:
        return ""


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--id", required=True, help="job id from data/queue.json")
    p.add_argument("--status", required=True, choices=STATUSES + MANUAL_STATUSES)
    p.add_argument("--company", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--fit", type=int, help="fit score 0-100")
    p.add_argument("--reason", required=True, help="one sentence: why this outcome")
    p.add_argument("--notes", default="")
    a = p.parse_args()
    print(record(a.id, a.status, a.company, a.title, a.url, a.reason, a.fit, a.notes))


if __name__ == "__main__":
    main()
