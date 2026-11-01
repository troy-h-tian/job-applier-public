"""Stop a run before it spends more of the Claude subscription than the candidate wants to give it.

Claude Code doesn't expose how full the 5-hour and weekly usage bars are, so this estimates them
from data/usage.jsonl: the API-equivalent cost of every batch in the trailing 5 hours and 7 days.
Exit status 3 means a cap would be passed by the next batch. scripts/agent.py checks it before
every Claude session and skips Claude when it's capped (Codex and Gemini are not counted).

Caps are in API-equivalent USD (0 disables one), from the first of: the USAGE_CAP_5H_USD /
USAGE_CAP_WEEK_USD environment variables, a temporary override in data/usage_caps.json
({"until": "2026-11-07T00:00:00+00:00", "5h": 20, "week": 320}, ignored once `until` has passed),
or the defaults:
  5h    default 10    (the limit was hit at 14-36 in the 5h window)
  week  default 120   (the weekly limit was hit at roughly 170-200)
Only the pipeline's own sessions are in the file, not the candidate's personal Claude use, which
shares the same bars, so the defaults leave room for it. NEXT_BATCH_USD (default 5) is the headroom a batch needs before it may start.
"""

import datetime as dt
import json
import os
import sys

from common import DATA

USAGE = DATA / "usage.jsonl"


def spent(hours, now):
    total = 0.0
    if not USAGE.exists():
        return total
    for line in USAGE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        at = dt.datetime.fromisoformat(row["at"])
        if now - dt.timedelta(hours=hours) < at <= now:
            total += row.get("cost_usd") or 0
    return total


def load_override(now):
    try:
        o = json.loads((DATA / "usage_caps.json").read_text())
        return o if now < dt.datetime.fromisoformat(o["until"]) else {}
    except (FileNotFoundError, ValueError, KeyError):
        return {}


def capped():
    """True if the next Claude batch would pass a usage cap; prints where each cap stands."""
    now = dt.datetime.now(dt.timezone.utc)
    need = float(os.environ.get("NEXT_BATCH_USD", "5"))
    override = load_override(now)
    caps = {
        "5h": (5, float(os.environ.get("USAGE_CAP_5H_USD") or override.get("5h", 10))),
        "week": (168, float(os.environ.get("USAGE_CAP_WEEK_USD") or override.get("week", 120))),
    }
    over = False
    for name, (hours, cap) in caps.items():
        used = spent(hours, now)
        note = ""
        if cap > 0 and used + need > cap:
            over, note = True, "  <- would pass the cap"
        print(f"  usage {name}: ${used:.2f} of ${cap:.2f} (API-equivalent){note}", file=sys.stderr)
    return over


def main():
    sys.exit(3 if capped() else 0)


if __name__ == "__main__":
    main()
