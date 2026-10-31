"""Run the application agent on the jobs in data/queue.json, one fresh session per job.

Each job goes to Codex first (the candidate's ChatGPT plan, via `codex login`) when it's installed
and signed in, and to Claude when Codex isn't available or reports a usage limit; Codex is then
paused until its limit resets (data/.codex_paused_until). USE_CODEX=0 uses Claude only.

A session's cost grows with everything already in its context (page snapshots stay there and are
re-read on every turn), so one session per job is much cheaper than one per batch. The sessions
share a prompt prefix (instructions and candidate files, then the job last), which is cached.

  python3 scripts/agent.py --mcp-config mcp.json --tools Read,WebSearch --context-file ctx.md [--batch N]

Each job's text goes to --log (and/or stderr) and its tokens (and Claude's cost) to data/usage.jsonl.
Exit status: 0 the batch is done, 2 Claude reported a usage limit, 3 two sessions in a row
recorded nothing (no credit, no network, browser not connected), 4 the application limit is reached.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from common import QUEUE, ROOT, load_applications
from record import record, remaining

PROMPT = ROOT / "prompts" / "apply.md"
USAGE = ROOT / "data" / "usage.jsonl"
CODEX_PAUSE = ROOT / "data" / ".codex_paused_until"
LIMIT = re.compile(r"hit your .*limit|usage limit|rate limit|quota|credit balance is too low", re.I)
# The parts of config/search.yaml the agent uses; the rest (sources, title filters) is for discovery.
SEARCH_KEYS = ("mode", "limits", "min_fit_score", "preferences")
# Screenshots are saved for the candidate but never shown to the agent (see mcp.json); it reads
# pages through browser_find and small snapshots, and describe_screenshot asks a cheap model when
# pixels matter. Images and saved page dumps are the expensive things to Read, so it can't.
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", "60"))
DENY = ["Read(./data/.secrets.env)", "Read(./data/screenshots/**)", "Read(**/*.png)", "Read(**/*.jpg)", "Read(**/*.jpeg)", "Read(**/*.pdf)"]


def search_config_excerpt():
    """config/search.yaml without the top-level sections the agent doesn't use."""
    keep, out = False, []
    for line in (ROOT / "config" / "search.yaml").read_text().splitlines():
        m = re.match(r"([a-z_]+):", line)
        if m:
            keep = m.group(1) in SEARCH_KEYS
        if keep:
            out.append(line)
    return "\n".join(out)


def candidate_files():
    files = [
        ("config/profile.yaml", (ROOT / "config" / "profile.yaml").read_text()),
        ("resume/resume.md", (ROOT / "resume" / "resume.md").read_text()),
        ("config/search.yaml (the parts that apply here)", search_config_excerpt()),
        ("config/writing_style.md", (ROOT / "config" / "writing_style.md").read_text()),
    ]
    return "\n\n".join(f"### `{name}`\n\n````\n{text.strip()}\n````" for name, text in files)


def build_prompt(context, job):
    # Everything before "## This job" is identical across the batch's sessions, so it's cached.
    return (f"{PROMPT.read_text()}\n\n## Candidate files\n\n{candidate_files()}\n\n"
            f"## Run context\n{context.strip()}\n\n"
            f"## This job\n\n```json\n{json.dumps(job, indent=2)}\n```\n"
            f"- Current UTC time: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}\n")


def run(cmd, args):
    """Run one agent session; returns (stdout, stderr, seconds), or stdout None if it timed out."""
    if args.caffeinate:
        cmd = ["caffeinate", "-is", *cmd]   # keep the Mac awake while the candidate assists
    started = time.time()
    # Its own process group, so a timeout can stop the whole session (with its browser and tool
    # servers, which can hang forever if they die); Ctrl-C and stops are passed on to it.
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True, cwd=ROOT)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: (os.killpg(p.pid, signal.SIGTERM), sys.exit(130)))
    try:
        # A job never legitimately takes this long, even with the candidate solving CAPTCHAs.
        out, err = p.communicate(timeout=JOB_TIMEOUT_MINUTES * 60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        p.communicate()
        out, err = None, ""
    return out, err, time.time() - started


def run_claude(prompt, args):
    cmd = ["claude", "-p", prompt, "--model", os.environ.get("CLAUDE_MODEL", "sonnet"),
           "--mcp-config", args.mcp_config, "--strict-mcp-config",
           "--permission-mode", "dontAsk",
           "--tools", args.tools,
           "--allowedTools", "mcp__playwright", "mcp__applier", *args.tools.split(","),
           "--disallowedTools", *DENY,
           "--output-format", "json"]
    if not os.environ.get("KEEP_SESSIONS"):   # set it to keep transcripts for looking into costs
        cmd.append("--no-session-persistence")
    if os.environ.get("MAX_BUDGET_USD"):
        cmd += ["--max-budget-usd", os.environ["MAX_BUDGET_USD"]]
    out, err, seconds = run(cmd, args)
    if out is None:
        return {"backend": "claude", "text": f"Session timed out after {JOB_TIMEOUT_MINUTES} minutes.",
                "error": True, "seconds": seconds}
    try:
        r = json.loads(out)
    except ValueError:
        return {"backend": "claude", "text": (out + err).strip(), "error": True, "seconds": seconds}
    u = r.get("usage") or {}
    text = (r.get("result") or "").rstrip()
    return {"backend": "claude", "text": text, "error": r.get("is_error"), "seconds": seconds,
            "cost_usd": r.get("total_cost_usd"), "turns": r.get("num_turns"),
            "input_tokens": u.get("input_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
            "cache_write_tokens": u.get("cache_creation_input_tokens"), "output_tokens": u.get("output_tokens"),
            "limited": bool(LIMIT.search(text[-500:]))}


def codex_bin():
    if os.environ.get("USE_CODEX", "1") == "0":
        return None
    return shutil.which("codex") or next(
        (p for p in [os.path.expanduser("~/.npm-global/bin/codex")] if os.access(p, os.X_OK)), None)


def codex_paused():
    try:
        return dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(CODEX_PAUSE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False


def pause_codex(message):
    """Pause Codex until the reset time its limit message gives ("try again in 2 hours 5 minutes"),
    or for an hour."""
    m = re.search(r"in ((?:\d+\s*(?:days?|hours?|hrs?|minutes?|mins?)[\s,and]*)+)", message, re.I)
    wait = dt.timedelta(hours=1)
    if m:
        parts = dict((u[0].lower(), int(n)) for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I))
        wait = dt.timedelta(days=parts.get("d", 0), hours=parts.get("h", 0), minutes=parts.get("m", 0)) or wait
    until = dt.datetime.now(dt.timezone.utc) + wait
    CODEX_PAUSE.write_text(until.isoformat(timespec="seconds"))
    return until


def toml(value):
    """A TOML value for `codex -c`: JSON strings and arrays of strings are valid TOML."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def expand(value):
    """${VAR:-default} as in mcp.json (Claude Code expands these itself; Codex doesn't)."""
    return re.sub(r"\$\{(\w+)(?::-([^}]*))?\}", lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def run_codex(binary, prompt, args):
    cmd = [binary, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-C", str(ROOT),
           "-s", "read-only", "-c", "approval_policy=never",
           "-c", f"web_search={toml('live' if 'WebSearch' in args.tools else 'disabled')}"]
    # Only the tools the job needs: the browser and applier MCP servers (as in --mcp-config), not
    # Codex's own shell, browser or apps.
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "browser_use_external",
                    "computer_use", "in_app_browser"):
        cmd += ["--disable", feature]
    if os.environ.get("CODEX_MODEL"):
        cmd += ["-m", os.environ["CODEX_MODEL"]]
    for name, server in json.load(open(ROOT / args.mcp_config))["mcpServers"].items():
        key = f"mcp_servers.{name}"
        cmd += ["-c", f"{key}.command={toml(server['command'])}",
                "-c", f"{key}.args={toml([expand(a) for a in server.get('args', [])])}",
                "-c", f"{key}.env={toml({k: expand(v) for k, v in server.get('env', {}).items()})}",
                "-c", f'{key}.env_vars={toml(["PATH", "HOME", "USER", "TMPDIR", "LANG"])}',
                "-c", f"{key}.startup_timeout_sec=120",
                "-c", f"{key}.tool_timeout_sec=900",   # wait_for_* can block up to 12 minutes
                "-c", f'{key}.default_tools_approval_mode="approve"']
    out, err, seconds = run(cmd + [prompt], args)
    if out is None:
        return {"backend": "codex", "text": f"Session timed out after {JOB_TIMEOUT_MINUTES} minutes.",
                "error": True, "seconds": seconds}
    text, errors, turns, usage = "", [], 0, {}
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or text
        elif ev.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            turns += 1
        elif ev.get("type") == "turn.completed":
            for k, v in (ev.get("usage") or {}).items():
                usage[k] = usage.get(k, 0) + (v if isinstance(v, int) else 0)
        elif ev.get("type") in ("error", "turn.failed"):
            errors.append(ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev))
    problem = "\n".join(errors) or ("" if out.strip() else err.strip())
    return {"backend": "codex", "text": "\n".join(x for x in (text.rstrip(), problem) if x),
            "error": bool(problem), "seconds": seconds, "turns": turns,
            "input_tokens": usage.get("input_tokens"), "cache_read_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"), "limited": bool(LIMIT.search(problem)), "problem": problem}


def log_usage(r, batch, job_id):
    row = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "batch": batch,
           "job": job_id, "backend": r["backend"],
           # Claude's API-equivalent cost (what usage_guard.py caps); Codex counts against the ChatGPT plan.
           "cost_usd": r.get("cost_usd") if r["backend"] == "claude" else None,
           "minutes": round(r["seconds"] / 60, 1), "turns": r.get("turns"), "error": r.get("error"),
           **{k: r.get(k) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}}
    with USAGE.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def recorded(job, before):
    return any(a["id"] == job["id"] for a in load_applications()[before:])


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mcp-config", required=True)
    p.add_argument("--tools", required=True, help="built-in tools, comma-separated")
    p.add_argument("--context-file", required=True, help="the run context lines for the prompt")
    p.add_argument("--batch", type=int, default=0)
    p.add_argument("--log", help="append the agent's text here instead of stderr")
    p.add_argument("--echo", action="store_true", help="with --log, also print the text to stderr")
    p.add_argument("--caffeinate", action="store_true")
    args = p.parse_args()

    context = open(args.context_file).read()
    jobs = json.loads(QUEUE.read_text())
    codex = codex_bin()
    misses = 0
    for n, job in enumerate(jobs, 1):
        before = len(load_applications())
        QUEUE.write_text(json.dumps([job], indent=2))   # what data/queue.json means in the prompt
        prompt = build_prompt(context, job)
        results = []
        if codex and not codex_paused():
            r = run_codex(codex, prompt, args)
            results.append(r)
            if r["limited"]:
                print(f"Codex reached its usage limit; using Claude until {pause_codex(r['problem']):%H:%MZ}.",
                      file=sys.stderr)
            elif r["error"] and not recorded(job, before):
                print(f"Codex failed ({r['problem'][:200]}); using Claude for the rest of this batch.",
                      file=sys.stderr)
                codex = None
        if not recorded(job, before):
            results.append(run_claude(prompt, args))
        for r in results:
            row = log_usage(r, args.batch, job["id"])
            cost = f"${row['cost_usd'] or 0:.2f}, " if r["backend"] == "claude" else ""
            print(f"  job {n}/{len(jobs)} ({r['backend']}): {cost}{row['minutes']} min, {row['turns']} turns",
                  file=sys.stderr)
            if args.log:
                with open(args.log, "a") as f:
                    f.write(f"#### {job['id']} ({r['backend']})\n{r['text']}\n")
            if args.echo or not args.log:
                print(r["text"], file=sys.stderr)
        r = results[-1]

        if r["backend"] == "claude" and r.get("limited"):
            print("Claude reported a usage limit.", file=sys.stderr)
            return 2
        if not recorded(job, before):
            # Retried at most once more (an id is retired after two errors).
            record(job["id"], "error", job.get("company", ""), job.get("title", ""), job.get("url", ""),
                   "The agent's session ended without recording an outcome.", notes=r["text"][-300:])
            misses += 1
            if misses >= 2:
                print("Two sessions in a row recorded nothing; stopping.", file=sys.stderr)
                return 3
        else:
            misses = 0
        if remaining() == 0:
            print("Application limit reached.", file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
