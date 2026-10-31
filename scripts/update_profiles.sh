#!/usr/bin/env bash
# Bring the candidate's Handshake, WayUp and Indeed profiles in line with resume/resume.md, in
# their own logged-in Chrome while they're at the screen (prompts/update_profiles.md). Like
# `assist.sh --my-browser`: needs the Playwright Extension in Chrome, and a dialog asks the
# candidate to do any login, CAPTCHA or verification step. Don't run it while assist.sh is running:
# they would share the browser.
#
#   scripts/update_profiles.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Runnable from inside a Claude Code session: this script starts its own, separate agent.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT
if [ "${USE_API_KEY:-0}" = 1 ] && [ -f "$HOME/.config/job-applier/env" ]; then
  set -a; . "$HOME/.config/job-applier/env"; set +a
else
  unset ANTHROPIC_API_KEY
fi

mkdir -p data/screenshots data/logs
cfg=data/.mcp-profiles.json
secrets_made=0
[ -f data/.secrets.env ] || { : > data/.secrets.env; secrets_made=1; }   # mcp.json expects it
trap 'rm -f "$cfg"; [ "$secrets_made" = 1 ] && rm -f data/.secrets.env' EXIT
# The normal tools, attached to the candidate's Chrome, with the assist dialogs turned on.
python3 - "$cfg" <<'PY'
import json, sys
cfg = json.load(open("mcp.json"))
args = cfg["mcpServers"]["playwright"]["args"]
args.remove("--headless")
for flag in ("--isolated", "--viewport-size"):
    i = args.index(flag)
    del args[i:i + (2 if flag == "--viewport-size" else 1)]
args.append("--extension")
cfg["mcpServers"]["applier"]["env"]["APPLIER_ASSIST"] = "1"
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY

prompt="$(cat prompts/update_profiles.md)

## Run context
- Resume PDF to upload (absolute path): ${PWD}/resume/resume.pdf
- Current UTC time: $(date -u +%Y-%m-%dT%H:%MZ)"

log="data/logs/$(date -u +%Y-%m-%d).log"
echo "### $(date -u +%H:%MZ) profile update" >> "$log"
caffeinate -is claude -p "$prompt" \
  --model "${CLAUDE_MODEL:-sonnet}" \
  --mcp-config "$cfg" --strict-mcp-config \
  --permission-mode dontAsk \
  --tools Read \
  --allowedTools "mcp__playwright" "mcp__applier__wait_for_candidate" "Read" \
  --no-session-persistence | tee -a "$log"
