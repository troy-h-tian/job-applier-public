---
name: captcha-assist
description: Start an assist-mode session that retries the job applications blocked on a CAPTCHA or bot check, in a visible browser on this Mac, while the candidate (with their accommodation helper) solves each CAPTCHA by hand. Use when the user asks to start the CAPTCHA jobs, the assist session, or the "captcha queue", or asks which jobs are waiting on a CAPTCHA.
---

# CAPTCHA assist session

Jobs that stopped on a CAPTCHA or bot check stay recorded as `needs_human` in
`private/data/applications.jsonl`; `scripts/assist.sh` picks them up from there, so new ones from
later scheduled runs are included automatically. The agent refills each form in a visible
browser. When a CAPTCHA, emailed security code or Workday verification comes up, a macOS dialog
beeps and asks the candidate to do that step in the browser window and click **Done** (or
**Skip**). The agent then submits, confirms, and records the outcome.

**Never solve, click or otherwise touch a CAPTCHA yourself, and don't drive the browser from
this session.** The candidate does that step; the script's own agent does everything else.

## Steps

1. Show what's waiting: run `scripts/assist.sh --list` from the repo root and tell the user how
   many jobs there are (the companies, briefly).
2. Start it right away unless the user limited it. Run it with `run_in_background` (it takes a
   few minutes per job and waits up to 12 minutes on each dialog):
   - `scripts/assist.sh`: the first 15 jobs on the list.
   - `scripts/assist.sh --max N`: the first N, if the user gave a number or a time limit (allow
     roughly 5 minutes per job).
   - `scripts/assist.sh <job id> ...`: specific jobs, if the user named companies (take the ids
     from the `--list` output).
3. Tell the user it has started: a Chrome window opens, and a dialog beeps whenever a CAPTCHA
   (or a security code or Workday verification) needs them; Skip moves to the next job.
4. When the script finishes, report per job: submitted, still needs a human (and why), or
   closed. The outcomes are the last rows of `private/data/applications.jsonl` and the end of
   the script's output; the script already commits and pushes them to the private data repo.
   If jobs remain on the list, offer to run the next batch.

## If it doesn't start

- `claude` must be logged in, and `npx` and Python's `mcp` package installed (`pip install -r
  requirements.txt`).
- Workday jobs need `WORKDAY_PASSWORD` in the environment; CAPTCHA jobs are usually Ashby,
  Lever or Greenhouse and don't.
- If `git pull` fails in `private/` because of local changes, show the user `git -C private
  status` rather than discarding anything.
