"""Run the application agent on the jobs in data/queue.json, one fresh Claude session per job.

A session's cost grows with everything already in its context (page snapshots stay there and are
re-read on every turn), so one session per job is much cheaper than one per batch. The sessions
share a prompt prefix (instructions and candidate files, then the job last), which Claude caches.

  python3 scripts/agent.py --mcp-config mcp.json --tools Read,WebSearch --context-file ctx.md [--batch N]

Each job's text goes to --log (and/or stderr) and its cost and tokens to data/usage.jsonl.
Exit status: 0 the batch is done, 2 Claude reported a usage limit, 3 two sessions in a row
recorded nothing (no credit, no network, browser not connected), 4 the application limit is reached.
"""

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import time

from common import QUEUE, ROOT, load_applications
from record import record, remaining

PROMPT = ROOT / "prompts" / "apply.md"
USAGE = ROOT / "data" / "usage.jsonl"
# The parts of config/search.yaml the agent uses; the rest (sources, title filters) is for discovery.
SEARCH_KEYS = ("mode", "limits", "min_fit_score", "preferences")
# Screenshots are saved for the candidate but never shown to the agent (see mcp.json); it reads
# pages through browser_find and small snapshots, and describe_screenshot asks a cheap model when
# pixels matter. Images and saved page dumps are the expensive things to Read, so it can't.
JOB_TIMEOUT_MINUTES = int(os.environ.get("JOB_TIMEOUT_MINUTES", "60"))
DENY = ["Read(./data/screenshots/**)", "Read(**/*.png)", "Read(**/*.jpg)", "Read(**/*.jpeg)", "Read(**/*.pdf)"]


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


def run_session(prompt, args):
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
    if args.caffeinate:
        cmd = ["caffeinate", "-is", *cmd]   # keep the Mac awake while the candidate assists
    started = time.time()
    try:
        # A session whose tool servers died can hang forever; a job never legitimately takes this
        # long, even with the candidate solving several CAPTCHAs.
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        # Its own process group, so a timeout can stop the whole session; pass on Ctrl-C and stops too.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (os.killpg(p.pid, signal.SIGTERM), sys.exit(130)))
        out, err = p.communicate(timeout=JOB_TIMEOUT_MINUTES * 60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        p.communicate()
        return {"result": f"Session timed out after {JOB_TIMEOUT_MINUTES} minutes.", "is_error": True}, time.time() - started
    try:
        return json.loads(out), time.time() - started
    except ValueError:
        return {"result": (out + err).strip(), "is_error": True}, time.time() - started


def log_usage(r, seconds, batch, job_id):
    u = r.get("usage") or {}
    row = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "batch": batch,
           "job": job_id, "cost_usd": r.get("total_cost_usd"), "minutes": round(seconds / 60, 1),
           "turns": r.get("num_turns"), "error": r.get("is_error"),
           "input_tokens": u.get("input_tokens"), "cache_read_tokens": u.get("cache_read_input_tokens"),
           "cache_write_tokens": u.get("cache_creation_input_tokens"), "output_tokens": u.get("output_tokens")}
    with USAGE.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


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
    misses = 0
    for n, job in enumerate(jobs, 1):
        before = len(load_applications())
        QUEUE.write_text(json.dumps([job], indent=2))   # what data/queue.json means in the prompt
        r, seconds = run_session(build_prompt(context, job), args)
        row = log_usage(r, seconds, args.batch, job["id"])
        print(f"  job {n}/{len(jobs)}: ${row['cost_usd'] or 0:.2f}, {row['minutes']} min, {row['turns']} turns",
              file=sys.stderr)
        text = (r.get("result") or "").rstrip()
        if args.log:
            with open(args.log, "a") as f:
                f.write(f"#### {job['id']}\n{text}\n")
        if args.echo or not args.log:
            print(text, file=sys.stderr)

        if re.search(r"hit your .*limit|usage limit|rate limit|credit balance is too low", text[-500:], re.I):
            print("Claude reported a usage limit.", file=sys.stderr)
            return 2
        if not any(a["id"] == job["id"] for a in load_applications()[before:]):
            # Retried at most once more (an id is retired after two errors).
            record(job["id"], "error", job.get("company", ""), job.get("title", ""), job.get("url", ""),
                   "The agent's session ended without recording an outcome.", notes=text[-300:])
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
