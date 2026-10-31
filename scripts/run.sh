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
# With both set, use the subscription token (the API key is the fallback).
[ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && unset ANTHROPIC_API_KEY

mkdir -p data/screenshots

# The browser tool types this password wherever Claude types the placeholder WORKDAY_PASSWORD,
# and redacts it from everything Claude sees. It is used for every site's candidate account, not
# just Workday. Without it, Workday jobs are skipped.
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

# Work through the queue in batches of up to 15 jobs (scripts/agent.py gives each job its own
# Claude session). Stop when nothing is left, after MAX_BATCHES_PER_RUN batches (leaves part of
# the subscription's usage for the candidate), when no backend can take more jobs, or before
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

  # Everything here is the same for every job in the batch; agent.py adds the job itself.
  cat > data/.run_context.md <<EOF
- Mode: ${mode}
- Resume PDF to upload (absolute path): ${PWD}/resume/resume.pdf
- Unofficial transcript PDF (absolute path): ${PWD}/resume/transcript_unofficial.pdf
- Official transcript PDF (absolute path): ${PWD}/resume/transcript_official.pdf
- Writing sample PDF (absolute path): ${PWD}/resume/writing_sample.pdf
EOF

  # One fresh Claude session per job. With PUBLIC_LOGS set (the public GitHub workflow), the
  # agent's output, which names companies and quotes cover letters, goes to a log in the private
  # data repo instead of this public log. Costs and tokens go to data/usage.jsonl.
  log_args=()
  if [ -n "${PUBLIC_LOGS:-}" ]; then
    mkdir -p data/logs
    log_args=(--log "data/logs/$(date -u +%Y-%m-%d).log")
    echo "### $(date -u +%H:%MZ) batch $batch" >> "${log_args[1]}"
  fi
  status=0
  python3 scripts/agent.py --mcp-config mcp.json --tools Read,WebSearch \
    --context-file data/.run_context.md --batch "$batch" ${log_args[@]+"${log_args[@]}"} || status=$?
  if [ -n "${PUBLIC_LOGS:-}" ]; then
    python3 - "$before" <<'PY'
import collections, json, sys
rows = open("data/applications.jsonl").read().splitlines()[int(sys.argv[1]):]
counts = collections.Counter(json.loads(r)["status"] for r in rows)
print("  batch outcomes:", ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none")
PY
  fi
  case "$status" in
    0) ;;
    2) echo "::warning::Claude usage limit reached; stopping this run."; break ;;
    3) echo "::warning::Agent sessions are recording nothing; stopping."; break ;;
    4) echo "Application limit reached; stopping."; break ;;
    5) echo "::warning::No backend (Codex, Gemini, Claude) can take more jobs right now; stopping this run."; break ;;
    *) echo "::warning::Batch $batch ended with an error"; break ;;
  esac
done
