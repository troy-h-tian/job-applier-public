"""MCP server giving the agent its own tools (recording outcomes, receiving codes the candidate
approved) so it never needs a shell."""

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import codes  # noqa: E402
from record import record  # noqa: E402

server = MCPServer("applier")


@server.tool()
def record_outcome(
    id: str,
    status: Literal["submitted", "dry_run", "skipped", "needs_human", "error"],
    company: str,
    title: str,
    url: str,
    reason: str,
    fit: int | None = None,
    notes: str = "",
) -> str:
    """Record what happened with one job from data/queue.json. Call exactly once per job.

    reason: one sentence explaining the outcome.
    fit: your 0-100 fit score.
    notes: free-text answers you wrote, the exact blocking question, anything the candidate should know.
    Returns how many applications remain this run.
    """
    return record(id, status, company, title, url, reason, fit, notes)


@server.tool()
def wait_for_approved_code(company: str, max_wait_minutes: int = 10) -> str:
    """Call this when an application site says it emailed a security/verification code.

    The candidate approves each submission by replying "yes" to that email; the approved email
    is then relayed here. Waits up to max_wait_minutes (max 12) and returns the approved code
    email(s), or a message saying none arrived, in which case record needs_human.
    company: the employer, so the candidate's approval can be matched to this application.
    """
    minutes = max(1, min(max_wait_minutes, 12))
    return codes.describe(codes.wait_for_codes(minutes), minutes)


if __name__ == "__main__":
    server.run()
