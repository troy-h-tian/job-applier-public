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
#
#   scripts/assist.sh --my-browser [--max N]   # the CAPTCHA jobs above, then the email-alert jobs the
#                                         # agent couldn't find on the employer's site (data/LEADS.md,
#                                         # "Not applied"), in batches of 15 until none are left or
#                                         # it's stopped, in the candidate's own running Chrome,
#                                         # already logged in to LinkedIn, Handshake, Indeed and WayUp. Needs the
#                                         # "Playwright Extension" in Chrome (Chrome asks to approve
#                                         # each connection). The candidate stays at the screen for
#                                         # CAPTCHAs, Duo and other verification.
#   scripts/assist.sh --my-browser --firefox   # a separate Firefox window with a saved profile
#                                         # (~/.job-applier-firefox) instead; log in to those sites
#                                         # in it on the first run. Untested.
set -euo pipefail
cd "$(dirname "$0")/.."
# Use the candidate's API key if they've saved one (kept outside the repo, never committed):
# ~/.config/job-applier/env containing ANTHROPIC_API_KEY=sk-ant-...  Otherwise `claude` uses its login.
if [ -f "$HOME/.config/job-applier/env" ]; then
  set -a; . "$HOME/.config/job-applier/env"; set +a
  echo "Using ANTHROPIC_API_KEY from ~/.config/job-applier/env" >&2
else
  unset ANTHROPIC_API_KEY
  echo "No ~/.config/job-applier/env: using the claude login (subscription)" >&2
fi
export MY_BROWSER=0
export USE_FIREFOX=0
case " $* " in *" --my-browser "*) MY_BROWSER=1 ;; esac
case " $* " in *" --firefox "*) USE_FIREFOX=1 ;; esac

git -C private pull -q --rebase
# Jobs already handed to the agent this session, so a job that stops on a CAPTCHA again isn't
# retried in the next batch.
export ATTEMPTED=data/.assist_attempted
: > "$ATTEMPTED"

build_queue() { python3 - "$@" <<'PY'
import json, re, sys
sys.path.insert(0, "scripts")
from common import QUEUE, load_applications

import os
args, max_jobs, list_only = sys.argv[1:], 15, False
my_browser = os.environ.get("MY_BROWSER") == "1"
for flag in ("--my-browser", "--firefox"):
    if flag in args:
        args.remove(flag)
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
lead_ids = set()
if my_browser and not args:
    # Then email-alert leads whose employer posting wasn't found: each is a job on LinkedIn,
    # Handshake, Indeed or WayUp. Newest first; the email's own link is used when it was kept.
    from common import DATA
    leads = {l["id"]: l for l in json.loads((DATA / "leads.json").read_text())}
    lead_ids = {i for i, r in latest.items()
                if i in leads and r["status"] == "skipped" and "not found" in r["reason"].lower()}
    ids += sorted(lead_ids, key=lambda i: latest[i]["at"], reverse=True)
    for i in lead_ids:
        latest[i] = {**latest[i], "url": leads[i].get("alert_url") or "", "notes": ""}
attempted = set(open(os.environ["ATTEMPTED"]).read().split()) if os.path.exists(os.environ["ATTEMPTED"]) else set()
ids = [i for i in ids if i not in attempted]
if list_only:
    for i in ids:
        r = latest[i]
        print(f"{r['at'][:10]}  {r['company']}: {r['title']} ({i})", file=sys.stderr)
    print(f"{len(ids)} job(s) waiting ({len(lead_ids)} of them email-alert jobs for your browser).", file=sys.stderr)
    print("list")
    sys.exit()
queue = []
for i in ids[:max_jobs]:
    r = latest.get(i)
    if not r:
        sys.exit(f"Unknown job id: {i}")
    # The notes hold the answers written last time, so the agent can reuse them.
    queue.append({"id": i, "company": r["company"], "title": r["title"], "url": r["url"],
                  "description": "", "previous_attempt": r["reason"] + " " + r["notes"],
                  **({"lead": True, "my_browser": True} if i in lead_ids else {})})
QUEUE.write_text(json.dumps(queue, indent=2))
with open(os.environ["ATTEMPTED"], "a") as f:
    f.write("".join(q["id"] + "\n" for q in queue))
print(f"{len(queue)} job(s) queued:", ", ".join(q["company"] for q in queue), file=sys.stderr)
print(len(queue))
PY
}

# Save results however the session ends (finished, Ctrl-C, or stopped), so it can be stopped any
# time and started again later.
save() {
  python3 scripts/report.py
  git -C private add -A data
  git -C private commit -q -m "Assist run $(date -u +%Y-%m-%dT%H:%MZ)" || return 0
  git -C private pull -q --rebase && git -C private push -q
}

list=$(build_queue "$@")
[ "$list" = list ] || [ "$list" = 0 ] && exit 0
trap 'rm -f data/.mcp-assist.json data/.secrets.env "$ATTEMPTED"; save' EXIT
trap 'exit 130' INT TERM

mkdir -p data/screenshots data/logs
( umask 077; printf "WORKDAY_PASSWORD='%s'\n" "${WORKDAY_PASSWORD:-}" > data/.secrets.env )
date -u +%Y-%m-%dT%H:%M:%S+00:00 > data/.run_started_at
# Same tools as a normal run, but with a visible browser and the assist dialogs turned on.
python3 - <<'PY'
import json, os
cfg = json.load(open("mcp.json"))
args = cfg["mcpServers"]["playwright"]["args"]
args.remove("--headless")
if os.environ.get("MY_BROWSER") == "1":
    # Drive the candidate's own Chrome (their logins and extensions) via the Playwright MCP Bridge.
    for flag in ("--isolated", "--viewport-size"):
        i = args.index(flag)
        del args[i:i + (2 if flag == "--viewport-size" else 1)]
    if os.environ.get("USE_FIREFOX") == "1":
        # Playwright's own Firefox build, with a profile kept outside the repo so logins persist.
        args += ["--browser", "firefox", "--user-data-dir", os.path.expanduser("~/.job-applier-firefox")]
    else:
        args.append("--extension")
cfg["mcpServers"]["applier"]["env"]["APPLIER_ASSIST"] = "1"
json.dump(cfg, open("data/.mcp-assist.json", "w"), indent=2)
PY

export MCP_TOOL_TIMEOUT=900000
while :; do
prompt="$(cat prompts/apply.md)

## Run context
- Mode: submit, **assist mode**: the candidate is watching the browser window and will do any
  CAPTCHA, security code or account verification by hand when you ask (see the tools).
- Jobs without \`my_browser\` were tried before and stopped at a CAPTCHA or bot check. Each queue
  entry's \`previous_attempt\` has the answers written last time: reuse them rather than rewriting.
- Resume PDF to upload (absolute path): ${PWD}/resume/resume.pdf
- Unofficial transcript PDF (absolute path): ${PWD}/resume/transcript_unofficial.pdf
- Official transcript PDF (absolute path): ${PWD}/resume/transcript_official.pdf
- Writing sample PDF (absolute path): ${PWD}/resume/writing_sample.pdf
$( [ "$MY_BROWSER" = 1 ] && echo "- **my-browser mode**: the browser is the candidate's own, already logged in to LinkedIn,
  Handshake, Indeed and WayUp, and they are at the screen. For these jobs (\`my_browser\`) you MAY
  open those sites, but only to find and apply to the job in the queue: the email link in \`url\` if
  there is one, otherwise a search for the company and title. Use the site's own apply flow (Easy
  Apply, Handshake apply) or follow its link to the employer's page. Work at a human pace, one job
  at a time. Never log in, and never touch a CAPTCHA, Duo prompt or other verification: use
  \`wait_for_candidate\`. Don't open anything else (feeds, messages, profiles, other jobs). If the
  job can't be found or is closed, record \`skipped\` (\"not found\" or \"closed\") as usual." )
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

# One batch unless in my-browser mode; there, keep going until nothing is left (or it's stopped).
[ "$MY_BROWSER" = 1 ] || break
save
list=$(build_queue "$@")
[ "$list" = 0 ] && break
done
