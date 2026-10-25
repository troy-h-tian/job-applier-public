#!/usr/bin/env bash
# One full run: discover new jobs, then let Claude work through the queue.
# Used by the GitHub Actions workflow; also runnable locally.
set -euo pipefail
cd "$(dirname "$0")/.."

if grep -q "REPLACE THIS FILE" resume/resume.md || [ ! -f resume/resume.pdf ]; then
  echo "::error::Add your resume first: resume/resume.md (text) and resume/resume.pdf (upload copy)."
  exit 1
fi

# Empty secrets arrive as empty strings; unset them so Claude Code picks whichever is real.
[ -z "${ANTHROPIC_API_KEY:-}" ] && unset ANTHROPIC_API_KEY
[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && unset CLAUDE_CODE_OAUTH_TOKEN

mkdir -p data/screenshots

# The browser tool types this password wherever Claude types the placeholder WORKDAY_PASSWORD,
# and redacts it from everything Claude sees. Without it, Workday jobs are skipped.
if [[ "${WORKDAY_PASSWORD:-}" == *"'"* ]]; then
  echo "::error::WORKDAY_PASSWORD must not contain a single quote (')."
  exit 1
fi
( umask 077; printf "WORKDAY_PASSWORD='%s'\n" "${WORKDAY_PASSWORD:-}" > data/.secrets.env )
date -u +%Y-%m-%dT%H:%M:%S+00:00 > data/.run_started_at

python3 scripts/inbox.py     # job-alert emails synced from Gmail -> data/leads.json
mode=$(cd scripts && python3 -c 'from common import *; print(effective_mode(load_search_config()))')

# wait_for_approved_code can block for up to 12 minutes while you approve a code.
export MCP_TOOL_TIMEOUT=900000
budget_args=()
[ -n "${MAX_BUDGET_USD:-}" ] && budget_args=(--max-budget-usd "$MAX_BUDGET_USD")   # optional

# Work through the queue in batches, each in a fresh Claude session (one session can't hold
# hundreds of jobs). Stop when nothing is left, after MAX_BATCHES_PER_RUN batches (leaves part of
# the subscription's usage for the candidate), when Claude reports a usage limit, or before
# GitHub's 6-hour job limit.
deadline=$(( $(date +%s) + ${RUN_MINUTES:-330} * 60 ))
max_batches=${MAX_BATCHES_PER_RUN:-0}   # 0 = no cap
batch=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if [ "$max_batches" -gt 0 ] && [ "$batch" -ge "$max_batches" ]; then
    echo "Stopping after $batch batch(es) (MAX_BATCHES_PER_RUN)."
    break
  fi
  python3 scripts/discover.py
  queued=$(python3 -c 'import json; print(len(json.load(open("data/queue.json"))))')
  if [ "$queued" = 0 ]; then
    echo "Queue empty. Done after $batch batch(es)."
    break
  fi
  batch=$((batch + 1))
  echo "=== Batch $batch: $queued job(s)"
  before=$(wc -l < data/applications.jsonl)

  # Cheap Haiku screen first: hard mismatches are recorded as skipped and removed from the batch.
  python3 scripts/prescreen.py
  queued=$(python3 -c 'import json; print(len(json.load(open("data/queue.json"))))')
  if [ "$queued" = 0 ]; then
    continue  # the whole batch was screened out; those skips count as progress
  fi

  prompt="$(cat prompts/apply.md)

## Run context
- Mode: ${mode}
- Resume PDF to upload (absolute path): ${PWD}/resume/resume.pdf
- Unofficial transcript PDF (absolute path): ${PWD}/resume/transcript_unofficial.pdf
- Official transcript PDF (absolute path): ${PWD}/resume/transcript_official.pdf
- Writing sample PDF (absolute path): ${PWD}/resume/writing_sample.pdf
- Jobs in queue: ${queued}
- Current UTC time: $(date -u +%Y-%m-%dT%H:%MZ)"

  # With PUBLIC_LOGS set (the public GitHub workflow), the agent's output, which names companies
  # and quotes cover letters, goes to a log in the private data repo instead of this public log.
  out=/dev/stdout
  if [ -n "${PUBLIC_LOGS:-}" ]; then
    mkdir -p data/logs
    out="data/logs/$(date -u +%Y-%m-%d).log"
    echo "### $(date -u +%H:%MZ) batch $batch" >> "$out"
  fi
  # JSON output carries the session's cost: what it would cost on the API (on a subscription it's
  # an estimate of usage, not a charge). The agent's text goes to $out; cost to data/usage.jsonl.
  result=$(mktemp)
  claude -p "$prompt" \
    --model "${CLAUDE_MODEL:-sonnet}" \
    --mcp-config mcp.json --strict-mcp-config \
    --permission-mode dontAsk \
    --allowedTools "mcp__playwright" "mcp__applier" "Read" "WebSearch" \
    --output-format json \
    ${budget_args[@]+"${budget_args[@]}"} \
    --no-session-persistence > "$result" 2>&1 || echo "::warning::Batch $batch ended with an error"
  limited=$(python3 - "$result" "$out" "$batch" <<'PY'
import datetime as dt, json, re, sys
raw = open(sys.argv[1]).read()
try:
    r = json.loads(raw)
    text = r.get("result") or ""
    row = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "batch": int(sys.argv[3]),
           "cost_usd": r.get("total_cost_usd"), "minutes": round((r.get("duration_ms") or 0) / 60000, 1),
           "turns": r.get("num_turns"), "error": r.get("is_error")}
    with open("data/usage.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    print(f"  batch cost (API-equivalent): ${row['cost_usd'] or 0:.2f}, {row['minutes']} min", file=sys.stderr)
except ValueError:
    text = raw
if sys.argv[2] == "/dev/stdout":   # local run: stdout is captured here, so show it on stderr
    sys.stderr.write(text.rstrip() + "\n")
else:
    with open(sys.argv[2], "a") as f:
        f.write(text.rstrip() + "\n")
print("1" if re.search(r"hit your .*limit|usage limit|rate limit", text[-500:], re.I) else "0")
PY
)
  rm -f "$result"
  if [ "$limited" = 1 ]; then
    echo "::warning::Claude usage limit reached; stopping this run."
    break
  fi
  if [ -n "${PUBLIC_LOGS:-}" ]; then
    python3 - "$before" <<'PY'
import collections, json, sys
rows = open("data/applications.jsonl").read().splitlines()[int(sys.argv[1]):]
counts = collections.Counter(json.loads(r)["status"] for r in rows)
print("  batch outcomes:", ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none")
PY
  fi

  # If a batch recorded nothing, the same jobs would come back forever; stop instead.
  if [ "$(wc -l < data/applications.jsonl)" = "$before" ]; then
    echo "::warning::Batch $batch recorded no outcomes; stopping."
    break
  fi
done
