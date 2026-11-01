"""Run the application agent on the jobs in data/queue.json, one fresh session per job.

Each job goes to the first available backend in BACKENDS, then to Claude: Gemini (the candidate's
Google plan, via the Antigravity CLI `agy`) then Codex (their ChatGPT plan, via `codex login`) on
the candidate's Mac, Codex alone in GitHub Actions. A backend that isn't installed or signed in, or
is under its usage limit, is skipped; one that reports a limit is paused until it resets
(data/.paused_<name>). USE_CODEX=0 / USE_GEMINI=0 turn one off; with none left, Claude does it.

A session's cost grows with everything already in its context (page snapshots stay there and are
re-read on every turn), so one session per job is much cheaper than one per batch. The sessions
share a prompt prefix (instructions and candidate files, then the job last), which is cached.

  python3 scripts/agent.py --mcp-config mcp.json --tools Read,WebSearch --context-file ctx.md [--batch N]

Each job's text goes to --log (and/or stderr) and its tokens (and Claude's cost) to data/usage.jsonl.
Exit status: 0 the batch is done, 2 Claude reported a usage limit, 3 two sessions in a row
recorded nothing (no credit, no network, browser not connected), 4 the application limit is reached,
5 no backend can take the job (all paused, failed or capped); the job is left for a later run.
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
from usage_guard import capped
from record import record, remaining

PROMPT = ROOT / "prompts" / "apply.md"
USAGE = ROOT / "data" / "usage.jsonl"
CLAUDE_LIMIT = re.compile(r"hit your .*limit|usage limit|rate limit|credit balance is too low", re.I)
LIMIT = re.compile(r"hit your .*limit|usage limit|rate limit|quota|exhausted|credit balance is too low|\b429\b", re.I)
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


def build_prompt(context, jobs):
    """jobs: one job (a dict) or several, for a session that works through all of them."""
    many = isinstance(jobs, list) and len(jobs) > 1
    head = ("## These jobs\n\nWork through them in order, recording each one before starting the next."
            if many else "## This job")
    # Everything before the jobs is identical across the batch's sessions, so it's cached.
    return (f"{PROMPT.read_text()}\n\n## Candidate files\n\n{candidate_files()}\n\n"
            f"## Run context\n{context.strip()}\n\n"
            f"{head}\n\n```json\n{json.dumps(jobs, indent=2)}\n```\n"
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
            "limited": bool(CLAUDE_LIMIT.search(text[-500:]))}


def codex_bin():
    if os.environ.get("USE_CODEX", "1") == "0":
        return None
    return shutil.which("codex") or next(
        (p for p in [os.path.expanduser("~/.npm-global/bin/codex")] if os.access(p, os.X_OK)), None)


def paused(name):
    try:
        return dt.datetime.now(dt.timezone.utc) < dt.datetime.fromisoformat(
            (ROOT / "data" / f".paused_{name}").read_text().strip())
    except (FileNotFoundError, ValueError):
        return False


def pause(name, message):
    """Pause a backend until the reset time its limit message gives ("try again in 2 hours 5
    minutes"), or for an hour."""
    m = re.search(r"in ((?:\d+\s*(?:days?|hours?|hrs?|minutes?|mins?)[\s,and]*)+)", message, re.I)
    wait = dt.timedelta(hours=1)
    if m:
        parts = dict((u[0].lower(), int(n)) for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I))
        wait = dt.timedelta(days=parts.get("d", 0), hours=parts.get("h", 0), minutes=parts.get("m", 0)) or wait
    until = dt.datetime.now(dt.timezone.utc) + wait
    (ROOT / "data" / f".paused_{name}").write_text(until.isoformat(timespec="seconds"))
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
        if "url" in server:   # a server already running (assist.sh's shared browser tool)
            cmd += ["-c", f"{key}.url={toml(server['url'])}", "-c", f"{key}.tool_timeout_sec=900",
                    "-c", f'{key}.default_tools_approval_mode="approve"']
            continue
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


GATE = '''#!/usr/bin/env python3
"""PreToolUse gate for unattended job-applier sessions: allow only the browser and applier MCP
servers (and reading their tool schemas); deny every other tool, including shell and file access."""
import json, os, sys

call = json.load(sys.stdin).get("toolCall") or {}
name, args = call.get("name"), call.get("args") or {}
schemas = os.path.realpath(os.path.expanduser("~/.gemini/antigravity-cli/mcp")) + os.sep
ok = False
if name == "call_mcp_tool":
    ok = args.get("ServerName") in ("playwright", "applier") and args.get("ToolName") != "browser_run_code_unsafe"
elif name in ("read_file", "list_dir", "view_file"):
    path = os.path.realpath(os.path.expanduser(str(args.get("Path") or args.get("DirectoryPath") or "")))
    ok = path.startswith(schemas)
print(json.dumps({"decision": "allow"} if ok else
                 {"decision": "deny", "reason": "only the browser tools are available here"}))
'''


def gemini_bin():
    if os.environ.get("USE_GEMINI", "1") == "0":
        return None
    return shutil.which("agy")


def agy_signed_in():
    out = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=60)
    return out.returncode == 0 and "sign in" not in (out.stdout + out.stderr).lower()


def run_gemini(binary, prompt, args):
    """One session on Gemini through the Antigravity CLI, which has no way to switch off its shell
    and file tools. So the session runs with a PreToolUse hook (.agents/hooks.json) that denies
    every tool except calls to the playwright and applier MCP servers, and permission rules that
    pre-approve only those two. Never --dangerously-skip-permissions."""
    cfg = json.load(open(ROOT / args.mcp_config))
    servers = {}
    for name, server in cfg["mcpServers"].items():
        if "url" in server:
            servers[name] = {"url": server["url"]}
            continue
        servers[name] = {
            "command": server["command"],
            "args": [str(ROOT / a) if re.match(r"(data|scripts)/", a) else expand(a) for a in server.get("args", [])],
            "env": {k: expand(v) for k, v in server.get("env", {}).items()}}
    agents = ROOT / ".agents"
    agents.mkdir(exist_ok=True)
    (agents / "gate.py").write_text(GATE)
    (agents / "hooks.json").write_text(json.dumps({"job-applier-gate": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"command": f"python3 {agents / 'gate.py'}", "timeout": 10}]}]}}))
    (agents / "mcp_config.json").write_text(json.dumps({"mcpServers": servers}, indent=2))
    cmd = [binary, "-p", prompt, "--model", os.environ.get("GEMINI_MODEL", "gemini-3.8-flash-high"),
           "--output-format", "json",
           "--settings", json.dumps({"permissions": {"allow": ["mcp(playwright/*)", "mcp(applier/*)"]}})]
    try:
        out, err, seconds = run(cmd, args)
    finally:   # so a person's own agy sessions in this folder aren't restricted
        shutil.rmtree(agents, ignore_errors=True)
    if out is None:
        return {"backend": "gemini", "text": f"Session timed out after {JOB_TIMEOUT_MINUTES} minutes.",
                "error": True, "seconds": seconds}
    try:
        r = json.loads(out[out.index("{"):])
    except ValueError:
        problem = (out + err).strip()[-600:]
        return {"backend": "gemini", "text": problem, "error": True, "seconds": seconds,
                "limited": bool(LIMIT.search(problem)), "problem": problem}
    u = r.get("usage") or {}
    ok = r.get("status") == "SUCCESS"
    text = (r.get("response") or "").rstrip()
    return {"backend": "gemini", "text": text, "error": not ok, "seconds": seconds, "turns": r.get("num_turns"),
            "input_tokens": u.get("input_tokens"), "cache_read_tokens": u.get("cache_read_tokens"),
            "output_tokens": u.get("output_tokens"), "limited": (not ok) and bool(LIMIT.search(text)),
            "problem": "" if ok else text}


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


def backend_order():
    """BACKENDS (comma-separated) before Claude; default gemini,codex, or just codex in GitHub Actions
    (which has no Gemini login)."""
    default = "codex" if os.environ.get("GITHUB_ACTIONS") else "gemini,codex"
    return [n for n in os.environ.get("BACKENDS", default).replace(" ", "").split(",") if n in ("codex", "gemini")]


def run_chain(prompt, args, dead, done=lambda: False):
    """Run one session on each backend in turn until the work is done: those in backend_order(),
    then Claude. A backend is skipped if it isn't installed or signed in, is paused after reporting a usage
    limit, or failed earlier in this batch (`dead`). done() says whether the job has its outcome
    (a session can fail after finishing). Returns the sessions run, the last one being the result."""
    results = []
    available = {"codex": (codex_bin(), run_codex), "gemini": (gemini_bin(), run_gemini)}
    for name in backend_order():
        binary, runner = available[name]
        if not binary or name in dead or paused(name):
            continue
        if name == "gemini" and not agy_signed_in():
            print("Gemini isn't signed in (run `agy`); skipping it.", file=sys.stderr)
            dead.add(name)
            continue
        r = runner(binary, prompt, args)
        results.append(r)
        if r.get("limited"):
            print(f"{name.title()} reached its usage limit; paused until {pause(name, r['problem']):%H:%MZ}.",
                  file=sys.stderr)
        elif r["error"] and not done():
            print(f"{name.title()} failed ({(r.get('problem') or r['text'])[:200]}); skipping it for this batch.",
                  file=sys.stderr)
            dead.add(name)
        if done() or not (r.get("limited") or r["error"]):
            return results
    if capped():
        print("Claude is at its estimated usage cap (USAGE_CAP_5H_USD / USAGE_CAP_WEEK_USD); not using it.",
              file=sys.stderr)
        return results
    results.append(run_claude(prompt, args))
    return results


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
    for job in jobs:
        # Alert-email links to Handshake jobs are the logged-out "public" form, which says "You don't
        # have permission to view this job" for jobs only shown to signed-in students, even in the
        # candidate's own logged-in Chrome. The signed-in form of the same job is /jobs/<id>.
        job["url"] = re.sub(r"^(https://app\.joinhandshake\.com)/public/jobs/", r"\1/jobs/", job.get("url") or "")
    dead = set()   # backends that failed in this batch
    misses = 0
    # JOBS_PER_SESSION jobs share one session. 1 (the default) keeps each job's pages out of the next
    # one's context; assist uses more, since every new session reconnects to the candidate's Chrome and
    # makes them click "Allow and select" again.
    per = max(1, int(os.environ.get("JOBS_PER_SESSION", "1")))
    done_jobs = 0
    for start in range(0, len(jobs), per):
        chunk = jobs[start:start + per]
        before = len(load_applications())
        QUEUE.write_text(json.dumps(chunk, indent=2))   # what data/queue.json means in the prompt
        ids = [j["id"] for j in chunk]

        def done():
            seen = {a["id"] for a in load_applications()[before:]}
            return all(i in seen for i in ids)

        results = run_chain(build_prompt(context, chunk if per > 1 else chunk[0]), args, dead, done=done)
        if not results:
            print("No backend can take the next job; stopping.", file=sys.stderr)
            return 5
        label = ",".join(ids)
        for r in results:
            row = log_usage(r, args.batch, label)
            cost = f"${row['cost_usd'] or 0:.2f}, " if r["backend"] == "claude" else ""
            print(f"  jobs {done_jobs + 1}-{done_jobs + len(chunk)}/{len(jobs)} ({r['backend']}): {cost}{row['minutes']} min, {row['turns']} turns",
                  file=sys.stderr)
            if args.log:
                with open(args.log, "a") as f:
                    f.write(f"#### {label} ({r['backend']})\n{r['text']}\n")
            if args.echo or not args.log:
                print(r["text"], file=sys.stderr)
        done_jobs += len(chunk)
        r = results[-1]

        if r["backend"] == "claude" and r.get("limited"):
            print("Claude reported a usage limit.", file=sys.stderr)
            return 2
        seen = {a["id"] for a in load_applications()[before:]}
        for job in chunk:
            if job["id"] in seen:
                misses = 0
                continue
            # Retried at most once more (an id is retired after two errors).
            record(job["id"], "error", job.get("company", ""), job.get("title", ""), job.get("url", ""),
                   "The agent's session ended without recording an outcome.", notes=r["text"][-300:])
            misses += 1
            if misses >= 2:
                print("Two jobs in a row recorded nothing; stopping.", file=sys.stderr)
                return 3
        if remaining() == 0:
            print("Application limit reached.", file=sys.stderr)
            return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
