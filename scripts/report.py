"""Print a markdown summary of the latest run and refresh data/NEEDS_YOU.md, data/APPLIED.md and
data/LEADS.md."""

import json
import sys
import urllib.parse

from common import DATA, load_applications
from discover import job_id_from_url


def cell(text):
    return (text or "").replace("|", "\\|").replace("\n", " ")


def table(rows, cols):
    head = "| " + " | ".join(c for c, _ in cols) + " |\n|" + "---|" * len(cols) + "\n"
    return head + "".join("| " + " | ".join(cell(str(f(r))) for _, f in cols) + " |\n" for r in rows)


def latest_by_id(rows):
    latest = {}
    for r in rows:
        latest[r["id"]] = r
    return sorted(latest.values(), key=lambda r: r["at"], reverse=True)


def main():
    rows = load_applications()
    try:
        run_start = (DATA / ".run_started_at").read_text().strip()
    except FileNotFoundError:
        run_start = ""
    this_run = [r for r in rows if r["at"] >= run_start] if run_start else []

    link = lambda r: f"[{r['title']}]({r['url']})"
    cols = [
        ("Status", lambda r: r["status"]),
        ("Company", lambda r: r["company"]),
        ("Role", link),
        ("Fit", lambda r: r.get("fit") if r.get("fit") is not None else ""),
        ("Reason", lambda r: r["reason"]),
    ]

    counts_only = "--counts-only" in sys.argv  # for public logs: no company names or reasons
    print("## Job applier run\n")
    if not this_run:
        print("No jobs were processed this run.\n")
    else:
        counts = {}
        for r in this_run:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(", ".join(f"**{n}** {s}" for s, n in sorted(counts.items())) + "\n")
        if not counts_only:
            print(table(this_run, cols))

    current = latest_by_id(rows)
    needs = [r for r in current if r["status"] == "needs_human"]
    (DATA / "NEEDS_YOU.md").write_text(
        "# Applications that need you\n\n"
        "The agent stopped on these (CAPTCHA, account required, or a question it couldn't answer "
        "truthfully from your profile). Finish them by hand, or add the missing answer to "
        "config/profile.yaml.\n\n"
        + (table(needs, cols[1:] + [("Notes", lambda r: r.get("notes", ""))]) if needs else "_Nothing right now._\n")
    )
    applied = [r for r in current if r["status"] == "submitted"]
    (DATA / "APPLIED.md").write_text(
        f"# Submitted applications ({len(applied)})\n\n"
        + (table(applied, [("Date", lambda r: r["at"][:10])] + cols[1:]) if applied else "_None yet._\n")
    )
    write_leads(current)
    if needs:
        print(f"\n{len(needs)} application(s) waiting on you: see data/NEEDS_YOU.md in the private repo")


def write_leads(current):
    """Every job from the alert emails, with what became of it, so the ones the agent can't
    apply to (LinkedIn Easy Apply, Handshake-only, ...) can be done by hand."""
    path = DATA / "leads.json"
    leads = json.loads(path.read_text()) if path.exists() else []
    by_id = {r["id"]: r for r in current}
    by_url = {job_id_from_url(r["url"]): r for r in current if r.get("url", "").startswith("http")}
    groups = {"Not applied: do these yourself": [], "Not tried yet": [], "Applied": [],
              "Skipped (fit, eligibility or closed)": []}
    for lead in leads:
        r = by_id.get(lead["id"]) or (by_url.get(job_id_from_url(lead["url"])) if lead.get("url") else None)
        if not r or r["status"] == "requeued":
            group = "Not tried yet"
        elif r["status"] == "submitted":
            group = "Applied"
        elif r["status"] == "needs_human" or "not found" in r.get("reason", "").lower():
            group = "Not applied: do these yourself"
        else:
            group = "Skipped (fit, eligibility or closed)"
        groups[group].append((lead, r))

    def links_cell(lead):
        out = []
        if lead.get("url"):
            out.append(f"[employer]({lead['url']})")
        if lead.get("alert_url"):
            out.append(f"[from email]({lead['alert_url']})")
        q = urllib.parse.quote_plus(f"{lead['company']} {lead['title']}")
        out.append(f"[LinkedIn search](https://www.linkedin.com/jobs/search/?keywords={q})")
        return " · ".join(out)

    cols = [
        ("Found", lambda x: x[0]["found_at"][:10]),
        ("Company", lambda x: x[0]["company"]),
        ("Role", lambda x: x[0]["title"]),
        ("Location", lambda x: x[0].get("location", "")),
        ("Links", lambda x: links_cell(x[0])),
        ("Outcome", lambda x: f"{x[1]['status']}: {x[1]['reason']}" if x[1] else ""),
    ]
    text = (f"# Every job from your alert emails ({len(leads)})\n\n"
            "LinkedIn, Handshake, WayUp and Indeed alerts. The agent applies on the employer's own "
            "site when it can find the job there; the first section is the ones it couldn't, to "
            "do by hand. The agent never opens these links.\n")
    for name, items in groups.items():
        items.sort(key=lambda x: x[0]["found_at"], reverse=True)
        text += f"\n## {name} ({len(items)})\n\n" + (table(items, cols) if items else "_None._\n")
    (DATA / "LEADS.md").write_text(text)


if __name__ == "__main__":
    main()
