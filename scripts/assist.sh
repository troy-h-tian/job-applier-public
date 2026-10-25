#!/usr/bin/env bash
# Assist mode, run on the candidate's Mac while they (or someone helping them) are at the screen:
# retry jobs that stopped on a CAPTCHA or bot check, in a visible browser. The agent refills each
# form; when a CAPTCHA, security code or Workday verification comes up, a dialog asks for that
# step to be done by hand, then the agent finishes. Results are recorded and pushed to the
# private data repo like a normal run.
#
#   scripts/assist.sh --list              # show the jobs waiting on a CAPTCHA or bot check
#   scripts/assist.sh                     # work through up to 15 of them
#   scripts/assist.sh --max 5             # just the first 5
#   scripts/assist.sh ashby:5353... ...   # just these job ids
set -euo pipefail
cd "$(dirname "$0")/.."

git -C private pull -q --rebase
list=$(python3 - "$@" <<'PY'
import json, re, sys
sys.path.insert(0, "scripts")
from common import QUEUE, load_applications

args, max_jobs, list_only = sys.argv[1:], 15, False
if "--list" in args:
    args.remove("--list")
    list_only = True
if "--max" in args:
    i = args.index("--max")
    max_jobs = int(args[i + 1])
    del args[i:i + 2]

latest = {}
for r in load_applications():
    latest[r["id"]] = r
ids = args or [i for i, r in latest.items() if r["status"] == "needs_human"
               and re.search(r"captcha|bot check|cloudflare", r["reason"] + r["notes"], re.I)]
if list_only:
    for i in ids:
        r = latest[i]
        print(f"{r['at'][:10]}  {r['company']}: {r['title']} ({i})", file=sys.stderr)
    print(f"{len(ids)} job(s) waiting on a CAPTCHA or bot check.", file=sys.stderr)
    print("list")
    sys.exit()
queue = []
for i in ids[:max_jobs]:
    r = latest.get(i)
    if not r:
        sys.exit(f"Unknown job id: {i}")
    # The notes hold the answers written last time, so the agent can reuse them.
    queue.append({"id": i, "company": r["company"], "title": r["title"], "url": r["url"],
                  "description": "", "previous_attempt": r["reason"] + " " + r["notes"]})
QUEUE.write_text(json.dumps(queue, indent=2))
print(f"{len(queue)} job(s) queued:", ", ".join(q["company"] for q in queue), file=sys.stderr)
print(len(queue))
PY
)
[ "$list" = list ] || [ "$list" = 0 ] && exit 0

mkdir -p data/screenshots data/logs
( umask 077; printf "WORKDAY_PASSWORD='%s'\n" "${WORKDAY_PASSWORD:-}" > data/.secrets.env )
date -u +%Y-%m-%dT%H:%M:%S+00:00 > data/.run_started_at
# Same tools as a normal run, but with a visible browser and the assist dialogs turned on.
python3 - <<'PY'
import json
cfg = json.load(open("mcp.json"))
cfg["mcpServers"]["playwright"]["args"].remove("--headless")
cfg["mcpServers"]["applier"]["env"]["APPLIER_ASSIST"] = "1"
json.dump(cfg, open("data/.mcp-assist.json", "w"), indent=2)
PY

export MCP_TOOL_TIMEOUT=900000
prompt="$(cat prompts/apply.md)

## Run context
- Mode: submit, **assist mode**: the candidate is watching the browser window and will do any
  CAPTCHA, security code or account verification by hand when you ask (see the tools).
- These jobs were tried before and stopped at a CAPTCHA or bot check. Each queue entry's
  \`previous_attempt\` has the answers written last time: reuse them rather than rewriting.
- Resume PDF to upload (absolute path): ${PWD}/resume/resume.pdf
- Unofficial transcript PDF (absolute path): ${PWD}/resume/transcript_unofficial.pdf
- Official transcript PDF (absolute path): ${PWD}/resume/transcript_official.pdf
- Writing sample PDF (absolute path): ${PWD}/resume/writing_sample.pdf
- Jobs in queue: ${list}
- Current UTC time: $(date -u +%Y-%m-%dT%H:%MZ)"

log="data/logs/$(date -u +%Y-%m-%d).log"
echo "### $(date -u +%H:%MZ) assist" >> "$log"
claude -p "$prompt" \
  --model "${CLAUDE_MODEL:-sonnet}" \
  --mcp-config data/.mcp-assist.json --strict-mcp-config \
  --permission-mode dontAsk \
  --allowedTools "mcp__playwright" "mcp__applier" "Read" \
  --no-session-persistence | tee -a "$log" || echo "Claude ended with an error"
rm -f data/.mcp-assist.json data/.secrets.env

python3 scripts/report.py
git -C private add -A data
git -C private commit -q -m "Assist run $(date -u +%Y-%m-%dT%H:%MZ)" || exit 0
git -C private pull -q --rebase && git -C private push -q
