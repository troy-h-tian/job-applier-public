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

# Work through the whole queue in batches, each in a fresh Claude session (one session can't hold
# hundreds of jobs). Stop when nothing is left, or before GitHub's 6-hour job limit.
deadline=$(( $(date +%s) + ${RUN_MINUTES:-330} * 60 ))
batch=0
while [ "$(date +%s)" -lt "$deadline" ]; do
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
  claude -p "$prompt" \
    --model "${CLAUDE_MODEL:-sonnet}" \
    --mcp-config mcp.json --strict-mcp-config \
    --permission-mode dontAsk \
    --allowedTools "mcp__playwright" "mcp__applier" "Read" "WebSearch" \
    ${budget_args[@]+"${budget_args[@]}"} \
    --no-session-persistence >> "$out" 2>&1 || echo "::warning::Batch $batch ended with an error"
  if [ -n "${PUBLIC_LOGS:-}" ]; then
    tail -n 5 "$out" | grep -qiE "usage limit|session limit|rate limit" && echo "::warning::Claude usage limit reached"
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
