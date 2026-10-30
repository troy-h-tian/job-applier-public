"""MCP server giving the agent its own tools (recording outcomes, receiving codes the candidate
approved and account-verification links) so it never needs a shell."""

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import assist  # noqa: E402
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
    if assist.enabled():
        return assist.ask_code(company, minutes)
    return codes.describe(codes.wait_for_codes(minutes), minutes)


@server.tool()
def wait_for_account_verification(application_url: str, max_wait_minutes: int = 8) -> str:
    """Call this when an application site (Workday or any other) says it sent an email with a
    link to verify the new candidate account.

    Verification emails are relayed here automatically. Waits up to max_wait_minutes (max 12) and
    returns the verification link(s) on the same site, or a message saying none arrived, in which
    case record needs_human.
    application_url: the current page URL, so the email can be matched to this site.
    """
    minutes = max(1, min(max_wait_minutes, 12))
    if assist.enabled():
        return assist.ask_verification(application_url, minutes)
    return codes.describe_verification(codes.wait_for_verification(application_url, minutes), minutes)


@server.tool()
def wait_for_candidate(task: str, max_wait_minutes: int = 10) -> str:
    """Assist mode only (the run context says so): ask the candidate, who is watching the browser,
    to do one step by hand, such as solving a CAPTCHA or bot check. Shows them a dialog and waits
    up to max_wait_minutes (max 12) for them to click Done or Skip.

    task: one sentence saying what to do, e.g. "Solve the CAPTCHA on the Base Power application".
    """
    minutes = max(1, min(max_wait_minutes, 12))
    if not assist.enabled():
        return "Not in assist mode: nobody is watching the browser. Record needs_human."
    return assist.ask(task, minutes)


@server.tool()
def make_tailored_resume(
    job_id: str,
    bullets: dict[str, list[str]] | None = None,
    skills_order: dict[str, list[str]] | None = None,
    section_order: list[str] | None = None,
    reason: str = "",
) -> str:
    """Build a copy of the resume PDF lightly tailored to one job. Optional: the original resume
    PDF is always fine to upload.

    bullets: entry id -> replacement bullets for that entry, the same number as the original. Entry
      ids: confidentiality, civ6 (projects); nanobase, cls_lab, spar, drasnin, anderson (experience).
      Reword or reorder only: same facts and numbers, no new tools, names or claims, and no longer
      than the original. Leave out entries you don't change.
    skills_order: skills label ("Programming Languages", "Technologies and Frameworks") -> the same
      items reordered, most relevant first.
    section_order: ["experience", "projects"] to put Experience first; omit to keep the original.
    reason: one sentence on what you emphasized for this company.
    Returns the PDF path to upload, or the problems found (fix them and retry once, or upload the
    original resume PDF).
    """
    import tailor

    try:
        path = tailor.tailor(job_id, bullets, skills_order, section_order, reason)
    except (ValueError, RuntimeError) as e:
        return f"Not built:\n{e}\nFix these and call again once, or upload the original resume PDF."
    return f"Tailored resume built. Upload this file: {path}"


if __name__ == "__main__":
    server.run()
