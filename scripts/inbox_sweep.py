"""Handshake inbox sweep, run by assist.sh --my-browser: one agent session (the usual backend order)
reads every new message in the candidate's Handshake inbox and records the links that open a job
application (prompts/handshake_inbox.md; data/handshake_inbox.json). assist.sh then queues them.

  python3 scripts/inbox_sweep.py --mcp-config data/.mcp-assist.json [--log FILE]
"""

import argparse
import datetime as dt
import sys
import types

import agent
from common import ROOT


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mcp-config", required=True)
    p.add_argument("--log")
    a = p.parse_args()
    args = types.SimpleNamespace(mcp_config=a.mcp_config, tools="Read", caffeinate=True)
    prompt = ((ROOT / "prompts" / "handshake_inbox.md").read_text()
              + f"\n\n## Run context\n- Current UTC time: {dt.datetime.now(dt.timezone.utc):%Y-%m-%dT%H:%MZ}\n")
    results = agent.run_chain(prompt, args, set())
    for r in results:
        row = agent.log_usage(r, 0, "handshake_inbox")
        print(f"  inbox sweep ({r['backend']}): {row['minutes']} min, {row['turns']} turns", file=sys.stderr)
        if a.log:
            with open(a.log, "a") as f:
                f.write(f"#### handshake inbox sweep ({r['backend']})\n{r['text']}\n")
    print(results[-1]["text"] if results else "No backend available for the inbox sweep.", file=sys.stderr)


if __name__ == "__main__":
    main()
