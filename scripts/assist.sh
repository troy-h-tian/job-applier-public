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
# Lets the agent read the emails the Gmail script relays into the private repo (codes you approved
# by replying "yes", account-verification links), using the GitHub login on this Mac.
export DATA_REPO_TOKEN="${DATA_REPO_TOKEN:-$(gh auth token 2>/dev/null || true)}"
export DATA_REPO="${DATA_REPO:-$(git -C private remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s/\.git$//')}"

# Runnable from inside a Claude Code session: this script starts its own, separate agent.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
# By default `claude` uses its own login (the subscription). With USE_API_KEY=1, use the
# ANTHROPIC_API_KEY in ~/.config/job-applier/env instead (kept outside the repo, never committed).
if [ "${USE_API_KEY:-0}" = 1 ] && [ -f "$HOME/.config/job-applier/env" ]; then
  set -a; . "$HOME/.config/job-applier/env"; set +a
  echo "Using ANTHROPIC_API_KEY from ~/.config/job-applier/env" >&2
else
  unset ANTHROPIC_API_KEY
  echo "Using the claude login (subscription)" >&2
fi
export PLAYWRIGHT_PORT="${PLAYWRIGHT_PORT:-8931}"
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
from common import DATA, QUEUE, load_applications

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
from common import needs_assist
ids = args or [i for i, r in latest.items() if needs_assist(r)]
lead_ids = set()
if my_browser and not args:
    # Then email-alert leads whose employer posting wasn't found: each is a job on LinkedIn,
    # Handshake, Indeed or WayUp. Newest first; the email's own link is used when it was kept.
    leads = {l["id"]: l for l in json.loads((DATA / "leads.json").read_text())}
    lead_ids = {i for i, r in latest.items()
                if i in leads and r["status"] == "skipped" and "not found" in r["reason"].lower()}
    ids += sorted(lead_ids, key=lambda i: latest[i]["at"], reverse=True)
    for i in lead_ids:
        latest[i] = {**latest[i], "url": leads[i].get("alert_url") or "", "notes": ""}
hs_ids = []
if my_browser and not args:
    # Application links found in the Handshake inbox (scripts/inbox_sweep.py) that haven't been tried.
    try:
        links = json.loads((DATA / "handshake_inbox.json").read_text())["links"]
    except (FileNotFoundError, ValueError, KeyError):
        links = {}
    for i, l in links.items():
        if i not in latest:
            latest[i] = {"at": "9999", "company": l["company"] or "Handshake message", "title": l["title"] or "(from a Handshake message)",
                         "url": l["url"], "reason": "", "notes": ""}
            hs_ids.append(i)
    lead_ids |= set(hs_ids)
    ids = hs_ids + ids
attempted = set(open(os.environ["ATTEMPTED"]).read().split()) if os.path.exists(os.environ["ATTEMPTED"]) else set()
ids = [i for i in ids if i not in attempted]
if list_only:
    for i in ids:
        r = latest[i]
        print(f"{r['at'][:10]}  {r['company']}: {r['title']} ({i})", file=sys.stderr)
    print(f"{len(ids)} job(s) waiting ({len(lead_ids)} of them email-alert or Handshake-message jobs for your browser).", file=sys.stderr)
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

# Skip postings that are plainly closed without opening a browser (employer links only).
case " $* " in *" --list "*) ;; *) python3 scripts/stale_check.py || true ;; esac
list=$(build_queue "$@")
[ "$list" = list ] || [ "$list" = 0 ] && exit 0
trap 'rm -f data/.mcp-assist.json data/.secrets.env data/.run_context.md "$ATTEMPTED"; save' EXIT
trap 'exit 130' INT TERM

mkdir -p data/screenshots data/logs
# The candidate-account password lives in GitHub's secrets for scheduled runs; here it comes from the
# environment or a WORKDAY_PASSWORD=... line in ~/.config/job-applier/env (outside the repo, never
# committed). Without it every site that needs an account is typed the placeholder word and fails.
if [ -z "${WORKDAY_PASSWORD:-}" ] && [ -f "$HOME/.config/job-applier/env" ]; then
  WORKDAY_PASSWORD=$(sed -n 's/^WORKDAY_PASSWORD=//p' "$HOME/.config/job-applier/env" | head -1 | sed -e "s/^['\"]//" -e "s/['\"]$//")
  export WORKDAY_PASSWORD
fi
if [ -z "${WORKDAY_PASSWORD:-}" ]; then
  echo "WARNING: no WORKDAY_PASSWORD (add WORKDAY_PASSWORD='...' to ~/.config/job-applier/env): jobs that need an account will fail." >&2
elif [[ "$WORKDAY_PASSWORD" == *"'"* ]]; then
  echo "WORKDAY_PASSWORD must not contain a single quote (')." >&2; exit 1
fi
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
        # Name Chrome's executable. Without it the tool reads Chrome's profile folder to check that the
        # extension is installed, which newer macOS blocks for this process, and it then wrongly shows
        # the "install the extension" page. Given the path, it skips that check.
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(chrome):
            args.extend(["--executable-path", chrome])
        # One shared connection to Chrome for the whole session (scripts/browser_proxy.py, started below):
        # every agent session goes through it, so Chrome asks the candidate to "Allow and select" once.
        port = os.environ["PLAYWRIGHT_PORT"]
        import shlex
        open("data/.playwright-serve.sh", "w").write("exec " + shlex.join(
            ["python3", "scripts/browser_proxy.py", "--port", port, "--", cfg["mcpServers"]["playwright"]["command"], *args]) + "\n")
        cfg["mcpServers"]["playwright"] = {"type": "http", "url": f"http://localhost:{port}/mcp"}
cfg["mcpServers"]["applier"]["env"]["APPLIER_ASSIST"] = "1"
json.dump(cfg, open("data/.mcp-assist.json", "w"), indent=2)
PY

export MCP_TOOL_TIMEOUT=900000
if [ -f data/.playwright-serve.sh ]; then
  # The shared browser-tool server: it attaches to the candidate's Chrome once (one "Allow and select"),
  # and every agent session after that connects to it instead of asking again.
  bash data/.playwright-serve.sh > "data/logs/playwright-server.log" 2>&1 &
  PW_PID=$!
  trap 'kill $PW_PID 2>/dev/null; rm -f data/.mcp-assist.json data/.secrets.env data/.run_context.md data/.playwright-serve.sh "$ATTEMPTED"; save' EXIT
  for _ in $(seq 60); do curl -s -o /dev/null "http://localhost:$PLAYWRIGHT_PORT/mcp" && break; sleep 1; done
fi
sweeps=0
while :; do
# Read the Handshake inbox for application links: at the start, then every 4th batch.
if [ "$MY_BROWSER" = 1 ] && [ "${NO_INBOX:-0}" != 1 ] && [ $((sweeps % 4)) = 0 ]; then
  python3 scripts/inbox_sweep.py --mcp-config data/.mcp-assist.json --log "data/logs/$(date -u +%Y-%m-%d).log" || true
  list=$(build_queue "$@")
  [ "$list" = 0 ] && break
fi
sweeps=$((sweeps + 1))
cat > data/.run_context.md <<EOF
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
  \`wait_for_candidate\`. Don't open anything else (feeds, messages, profiles, other jobs). Jobs whose
  id starts with \`hs:\` came from a link in the candidate's Handshake messages: \`url\` is that
  link, so go straight to it. If the
  job can't be found or is closed, record \`skipped\` (\"not found\" or \"closed\") as usual." )
EOF

log="data/logs/$(date -u +%Y-%m-%d).log"
echo "### $(date -u +%H:%MZ) assist" >> "$log"
# One fresh Claude session per job (scripts/agent.py); caffeinate keeps the Mac from idle-sleeping
# meanwhile (closing the lid still sleeps it).
status=0
# 5 jobs per session: each new session makes Chrome ask the candidate to "Allow and select" the tab again.
JOBS_PER_SESSION="${JOBS_PER_SESSION:-5}" python3 scripts/agent.py --mcp-config data/.mcp-assist.json --tools Read --context-file data/.run_context.md \
  --log "$log" --echo --caffeinate || status=$?

# One batch unless in my-browser mode; there, keep going until nothing is left (or it's stopped).
[ "$MY_BROWSER" = 1 ] || break
# Sessions that record nothing failed (no credit, no network, browser not connected), and a usage
# limit won't lift soon: stop rather than burn through the list.
if [ "$status" != 0 ]; then
  echo "Stopping (see above). Fix the problem and run it again." >&2
  break
fi
save
list=$(build_queue "$@")
[ "$list" = 0 ] && break
done
