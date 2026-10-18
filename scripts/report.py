"""Print a markdown summary of the latest run and refresh data/NEEDS_YOU.md and data/APPLIED.md."""

import sys

from common import DATA, load_applications


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
    if needs:
        print(f"\n{len(needs)} application(s) waiting on you: see data/NEEDS_YOU.md in the private repo")


if __name__ == "__main__":
    main()
