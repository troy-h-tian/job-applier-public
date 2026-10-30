"""Stop a run before it spends more of the Claude subscription than the candidate wants to give it.

Claude Code doesn't expose how full the 5-hour and weekly usage bars are, so this estimates them
from data/usage.jsonl: the API-equivalent cost of every batch in the trailing 5 hours and 7 days.
Exit status 3 means a cap would be passed by the next batch; run.sh then stops.

Caps are in API-equivalent USD (env vars, 0 disables one):
  USAGE_CAP_5H_USD    default 0 (off); the subscription limit was hit at 14-36 in the 5h window
  USAGE_CAP_WEEK_USD  default 0 (off); the weekly limit was hit at roughly 170-200
The candidate's own Claude use shares the same bars but isn't in the file, so set caps
below those limits to leave room for it. NEXT_BATCH_USD (default 5) is the headroom a batch needs before it may start.
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


def main():
    now = dt.datetime.now(dt.timezone.utc)
    need = float(os.environ.get("NEXT_BATCH_USD", "5"))
    caps = {
        "5h": (5, float(os.environ.get("USAGE_CAP_5H_USD", "0"))),
        "week": (168, float(os.environ.get("USAGE_CAP_WEEK_USD", "0"))),
    }
    over = False
    for name, (hours, cap) in caps.items():
        used = spent(hours, now)
        note = ""
        if cap > 0 and used + need > cap:
            over, note = True, "  <- would pass the cap"
        print(f"  usage {name}: ${used:.2f} of ${cap:.2f} (API-equivalent){note}", file=sys.stderr)
    sys.exit(3 if over else 0)


if __name__ == "__main__":
    main()
