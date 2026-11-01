"""Mark closed postings as skipped without opening a browser, before assist.sh queues jobs.

For each job the assist queue would try (CAPTCHA jobs and email-alert jobs) whose link is on an
employer's own site, fetch the page; HTTP 404/410, or a page that plainly says the posting is closed,
is recorded as `skipped`. Pages that need a login (Handshake, LinkedIn, Indeed, WayUp) are never
fetched, and anything unclear is left for the agent. Conservative on purpose: a false "closed" would
lose a real application.

  python3 scripts/stale_check.py [--dry-run]
"""

import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import DATA, is_blocked_site, load_applications, needs_assist
from record import record

CLOSED = re.compile(
    r"no longer accepting applications|(job|position|posting|role|listing) (is|has been|was) "
    r"(no longer (available|open|active)|closed|filled|removed|taken down)|"
    r"(job|position|posting) (has )?expired|this (job|position|posting) (is )?closed|"
    r"we('re| are) no longer (hiring|accepting)|job (listing )?not found|"
    r"the (job|position) you('re| are) looking for (is no longer|has been|could not)", re.I)


def closed_reason(url):
    """A reason string if the posting at url is plainly closed, else None."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (job-applier; personal use)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            final, body = r.geturl(), r.read(300_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}" if e.code in (404, 410) else None
    except Exception:
        return None
    if re.search(r"[?&]error=true", final):   # Greenhouse sends closed jobs back to the board
        return "redirected to the job board with an error"
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    m = CLOSED.search(text)
    return f'the page says "{m.group(0)}"' if m else None


def candidates():
    latest = {}
    for r in load_applications():
        latest[r["id"]] = r
    out = {i: r["url"] for i, r in latest.items() if needs_assist(r)}
    try:
        leads = {l["id"]: l for l in json.loads((DATA / "leads.json").read_text())}
    except (FileNotFoundError, ValueError):
        leads = {}
    for i, r in latest.items():
        if i in leads and r["status"] == "skipped" and "not found" in r["reason"].lower():
            out[i] = leads[i].get("alert_url") or ""
    return {i: u for i, u in out.items() if u.startswith("http") and not is_blocked_site(u)}, latest


def main():
    dry = "--dry-run" in sys.argv
    todo, latest = candidates()
    with ThreadPoolExecutor(8) as pool:
        results = dict(zip(todo, pool.map(closed_reason, todo.values())))
    n = 0
    for i, why in results.items():
        if not why:
            continue
        r = latest[i]
        n += 1
        print(f"  closed: {r['company']}: {r['title']} ({why})", file=sys.stderr)
        if not dry:
            record(i, "skipped", r["company"], r["title"], todo[i],
                   f"The posting is closed ({why}); checked automatically without opening a browser.")
    print(f"stale check: {n} of {len(todo)} employer link(s) closed", file=sys.stderr)


if __name__ == "__main__":
    main()
