---
name: captcha-assist
description: Start (or restart) the assist session on this Mac: the agent works through the jobs blocked on a CAPTCHA, bot check or emailed code, then the email-alert jobs (LinkedIn, Handshake, Indeed, WayUp) it couldn't find on employer sites, in the candidate's own Chrome while they're at the screen. Use when the user asks to start or restart the assist session, the CAPTCHA jobs, the "captcha queue", the live session, or which jobs are waiting.
---

# Assist session

`scripts/assist.sh --my-browser` works through one queue: first the jobs stopped on a CAPTCHA, bot
check or emailed code (`needs_human` in `private/data/applications.jsonl`), then the email-alert
jobs the scheduled runs couldn't find on an employer's site (`data/LEADS.md`, "Not applied"). It
drives the candidate's own Chrome, already logged in to LinkedIn, Handshake, Indeed and WayUp,
through the Playwright Extension, in batches of 15 until the queue is empty or it's stopped.
When a CAPTCHA, security code, Duo prompt or verification comes up, a macOS dialog beeps and asks
the candidate to do that step in the browser and click **Done** (or **Skip**). Results are
committed and pushed every batch and whenever it stops, so it can be stopped and restarted any time.

**Never solve, click or otherwise touch a CAPTCHA yourself, and don't drive the browser from
this session.** The candidate does that step; the script's own agent does everything else.

## Steps (the user may ask in one short message; don't ask questions first)

1. Run `scripts/assist.sh --my-browser --list` from the repo root and tell the user how many jobs
   are waiting.
2. Start it right away with `run_in_background`: `scripts/assist.sh --my-browser` (add `--max N`
   only if the user gave a number or time limit; use `--firefox` only if they ask). The script
   itself handles running inside Claude Code, keeping the Mac awake and saving results.
3. Tell the user, briefly, what to do on their side: in Chrome, when the Playwright Extension asks
   which tab to allow, choose the job-application tab it opened ("Allow & select"); a dialog
   beeps whenever a CAPTCHA or code needs them; Skip moves on. If the first line of the output is
   an error, or a batch records nothing, say why (e.g. no API credit, Chrome not connected).
4. When it finishes or the user stops it (`TaskStop`), report per job: submitted, still needs a
   human (and why), or not found/closed. The outcomes are the last rows of
   `private/data/applications.jsonl`. Offer to restart.

## If it doesn't start

- `claude` must be logged in, and `npx` and Python's `mcp` package installed (`pip install -r
  requirements.txt`).
- The Playwright Extension must be installed in Chrome, with "Allow access to file URLs" turned
  on (chrome://extensions → Details), or resume uploads fail.
- By default it uses the `claude` login (subscription). `USE_API_KEY=1 scripts/assist.sh ...`
  uses the key in `~/.config/job-applier/env` instead.
- If `git pull` fails in `private/` because of local changes, show the user `git -C private
  status` rather than discarding anything.
