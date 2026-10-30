# job-applier

An internship application agent. Three times a day, GitHub Actions:

1. **Discovers** new postings from company job boards (Greenhouse, Lever, Ashby and Workday
   public APIs), the SimplifyJobs and vanshb03 internship feeds (any term, any company site), a
   curated program list for freshmen and sophomores (zapplyjobs/underclassmen-internships),
   job-alert emails and internship newsletters, and hand-picked links, filtered by title,
   location and age (`scripts/discover.py`, `scripts/inbox.py`). Every email lead is queued
   regardless of title or location filters.
2. **Screens** each posting cheaply: its description is fetched from the job board's API and
   Claude Haiku skips hard mismatches (degree level, graduation window, terms already over,
   country) (`scripts/prescreen.py`).
3. **Applies** by running Claude Code headlessly with a Playwright browser (`scripts/run.sh`,
   `prompts/apply.md`). Claude scores fit against the candidate's resume, fills out the form,
   creates a candidate account on the application site when needed, uploads the resume
   (optionally a copy tailored to the company, see below) and the transcript if asked, writes
   short answers grounded in the resume and the company's stated values, and submits.
4. **Records every outcome** in the private data repo: the application log, `APPLIED.md`,
   `NEEDS_YOU.md` for anything with an unusual requirement (a video, an assessment, an emailed
   application, a question the profile can't answer), and `LEADS.md`, every job from the alert
   emails with what became of it.

Jobs that stop on a CAPTCHA, bot check or emailed code, and email-alert jobs the agent couldn't
find on an employer's site, are finished on the candidate's Mac in **assist mode** (below).

## Two repos: public code, private data

This repo is public and contains **only code**, so Actions minutes are free. Everything personal
lives in a separate **private** repo with this layout:

```text
config/profile.yaml       contact info, work authorization, canned answers
config/search.yaml        target roles, sources, filters, limits
config/writing_style.md   rules for cover letters and free-text answers
config/extra_jobs.txt     hand-picked application links
resume/resume.md, .pdf    resume text (what Claude may say) and the file to upload: the ground truth
resume/resume.yaml        structured copy of resume.md, used only to build tailored PDFs
resume/transcript_*.pdf   unofficial and official transcripts, uploaded only where a form asks
resume/writing_sample.pdf a writing sample, uploaded only where a form asks for one
data/                     application log, NEEDS_YOU.md / APPLIED.md / LEADS.md, leads, synced
                          emails, private run logs, tailored resumes (data/tailored/), per-batch
                          usage (data/usage.jsonl) and a cache of resolved feed links
```

Each run checks the private repo out into `private/`, links `config/`, `resume/` and `data/` into
place, and pushes results back. The public log shows only counts: the agent's own output goes to
`data/logs/` in the private repo, and no screenshots or artifacts are uploaded. Each run also
updates `last-run.txt` here, because GitHub disables scheduled workflows in public repos after 60
days without activity.

## Setup

1. Create the private data repo with the layout above.
2. In this repo's **Settings → Secrets and variables → Actions**, add:
   - `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`. If both are
     set, the API key is used and the subscription token is the fallback.
   - `DATA_REPO_TOKEN`: a fine-grained token with **Contents: Read and write** on the private repo.
   - `WORKDAY_PASSWORD` (optional, despite the name): the password for every candidate account
     the agent creates, on Workday and any other application site. Use one you don't reuse
     elsewhere. 8+ characters with upper, lower, number and symbol; no single quotes. Claude
     never sees it: the browser tool fills it in. Without it, Workday jobs are skipped.
3. Set `DATA_REPO` in `.github/workflows/apply.yml` to the private repo's name.
4. Optional repository **variables**: `CLAUDE_MODEL` (default `sonnet`), `MAX_BATCHES_PER_RUN`
   (default `0` = no cap, see **Usage** below), `USAGE_CAP_5H_USD` / `USAGE_CAP_WEEK_USD`
   (estimated usage caps, default `0` = off), `MAX_BUDGET_USD` (spending cap per batch of
   15 jobs, API key only; none by default). The API key's Anthropic account needs credit
   ("Credit balance is too low" otherwise).
5. Run it once from **Actions → Job applier → Run workflow**.

## Job-alert emails and newsletters

WayUp, Handshake, LinkedIn and Indeed forbid automated access to their sites, so the scheduled
agent never visits them. It reads the emails they send instead, plus internship newsletters
(UCLA mailing lists such as the Undergraduate Internship Program and CS newsletters, Direct
Consideration):

1. `integrations/gmail_to_repo.gs`, a Google Apps Script in each Gmail account that receives
   them, copies new alert emails (marked read) and newsletters (left as they are), with their
   link targets, into the private repo's `data/inbox/` every 2 hours. Newsletters are matched by
   mailing list or sender, so edit `NEWSLETTER_QUERY` to add one.
2. Each run, Claude Haiku extracts each company + title + link into `data/leads.json`.
3. Links are followed through click trackers to the employer's page, stopping before any
   alert-site page (`scripts/links.py`). Otherwise the company's Greenhouse/Lever/Ashby board is
   looked up and the title matched there (`scripts/boards.py`); boards found this way also
   become regular sources. Only leads still without a link fall back to web search. The email's
   own link is kept in the lead for the candidate, never opened by the scheduled agent.
4. Leads with no employer posting end up in `data/LEADS.md` under "Not applied", and are the
   second half of the assist queue (below).

The script also marks "thank you for applying" receipts as read (anything that looks like a real
next step, such as an interview invitation or assessment, stays unread).

Apps Script setup: script.google.com → New project → paste the script → Project Settings →
Script properties → `GITHUB_TOKEN` = a fine-grained token with Contents: Read and write on the
private repo → run `install`. Set `REPO` at the top of the script to the private repo. After
pasting a newer version, run `install` again to refresh the triggers.

## Emailed verification codes and account links

Some sites (often Greenhouse) email a security code before accepting an application. Replying
**"yes"** to that email approves that one submission: the Apps Script relays only that email
into `data/codes/`, the waiting agent enters the code and submits, and the relayed copy is
deleted. Codes without a "yes" reply are never relayed. CAPTCHAs are never attempted.

When the agent creates a candidate account itself, the site may email a "verify your account"
link. Those emails are relayed automatically (no reply needed) into `data/verify/`. The agent
opens a link only if it is on the same site it is applying on (the same Workday tenant, or the
same domain elsewhere), then signs in and continues. Other emails are never opened.

## Assist mode (CAPTCHAs, and the alert sites)

The agent never solves CAPTCHAs, so jobs that hit one are recorded as `needs_human`, and
`NEEDS_YOU.md` leaves them out. To work through them with the candidate at the screen, run on a
Mac (in Claude Code, ask to "restart the live session": the `captcha-assist` skill does it):

```bash
scripts/assist.sh --my-browser          # the whole queue, in the candidate's own Chrome
scripts/assist.sh --my-browser --list   # what's waiting
scripts/assist.sh                       # just the CAPTCHA jobs, in a separate visible browser
```

`--my-browser` works one queue: the CAPTCHA, bot-check and emailed-code jobs first, then the
email-alert jobs the scheduled runs couldn't find on an employer's site. It drives the
candidate's own, already logged-in Chrome through the Playwright Extension (install it in Chrome,
turn on "Allow access to file URLs" for it, and approve the tab when Chrome asks), in batches of
15 until the queue is empty or it's stopped. It may open LinkedIn, Handshake, Indeed and WayUp
only in this mode, only for the job in the queue, and never logs in. Whenever a CAPTCHA, Duo
prompt or other check comes up, a dialog beeps and asks the candidate to do that step and click
Done (or Skip). A security code the candidate already approved with "yes", or an account link,
is picked up from the relay automatically instead of asking. Results are saved and pushed after
every batch and whenever the session stops, and it keeps the Mac awake while a batch runs. It
stops if a batch records nothing (for example, no API credit). `--firefox` uses a separate
Firefox window instead (untested).

It uses the API key in `~/.config/job-applier/env` (`ANTHROPIC_API_KEY=...`, kept outside the
repo) when that file exists; `USE_API_KEY=0` uses the `claude` login instead.

## Tailored resumes

For each application Claude may build a copy of the resume that leans toward the company: it can
reword or reorder up to 3 bullets, put the most relevant skills first, and put Experience before
Projects. The original `resume.pdf` stays the ground truth and the default upload.
`scripts/tailor.py` builds the copy from `resume.yaml` with the original's LaTeX layout (Jake's
Resume, compiled with [Tectonic](https://tectonic-typesetting.github.io)) and refuses any version
that:

- changes a heading, title, date, contact detail or the number of bullets,
- adds a number the original bullet doesn't have, or a name, tool or term not in the resume,
- uses more than a few words that aren't in the resume, grows an entry by more than 8%, or
  spills past one page,
- adds, drops or renames a skill (reordering only).

Every check also confirms that `resume.yaml` still matches `resume.md` word for word, so **after
editing your resume, update `resume.md`, `resume.pdf` and `resume.yaml` together**
(`python3 scripts/tailor.py --check` shows mismatches). Each tailored PDF and the changes behind it
are saved under `data/tailored/<job id>/`, so you can see exactly what was sent.

## Usage

Runs use `ANTHROPIC_API_KEY` (billed per token) when it's set, otherwise your Claude
subscription (`CLAUDE_CODE_OAUTH_TOKEN`). The subscription allows a certain amount per 5-hour
window plus a weekly cap, shared with your own use of Claude. The schedule is 8:17am, 1:27pm and
6:37pm Pacific (an hour earlier in winter), just over 5 hours apart, so each run starts in a
fresh window, while you're awake to approve emailed codes. By default each run keeps going until
the queue is empty, Claude reports a usage limit, or GitHub's 6-hour job limit nears. To leave
room for your own use, set `MAX_BATCHES_PER_RUN`, or `USAGE_CAP_5H_USD` / `USAGE_CAP_WEEK_USD`
(API-equivalent dollars per trailing 5 hours / 7 days, estimated from `data/usage.jsonl` by
`scripts/usage_guard.py`).

Each batch's cost is appended to `data/usage.jsonl` (`cost_usd` is the API-equivalent price; on a
subscription nothing extra is charged). With an API key nothing caps spending unless you set one
of the variables above.

## Tuning (in the private `config/search.yaml`)

- **Where it looks:** board names under `sources`, Workday career-site URLs, the aggregate
  `simplify_feeds` (`apply_hosts: []` allows any application site; list hosts to restrict it) and
  `program_lists`.
- **How picky:** `min_fit_score`, the free-text `preferences`, and the `title_*` / `locations_*`
  pre-filters.
- **How much:** each run works through the queue in batches of 15 until it's empty,
  `MAX_BATCHES_PER_RUN` is reached, Claude's usage limit is hit, or GitHub's 6-hour job limit
  nears. Set any value under `limits` to cap applications too.

## Run locally

Clone the private repo into `private/` and link it into place, then run:

```bash
ln -s private/config config && ln -s private/resume resume && ln -s private/data data
pip install -r requirements.txt
brew install tectonic                 # optional: tailored resumes (otherwise the original is used)
APPLIER_MODE=dry_run scripts/run.sh   # uses your logged-in Claude Code and local Chrome
```

## Limits and caveats

- **Application sites:** any site is allowed (`apply_hosts: []`). Greenhouse, Lever, Ashby and
  Workday are the most reliable and the only ones whose descriptions are read for screening;
  custom portals take longer and fail more often.
- **Accounts:** the agent may create a candidate account on the application site, using the
  profile email and `WORKDAY_PASSWORD`. Sites that email a link are handled through the relay;
  sites offering only "Sign in with Google/LinkedIn", or rejecting the password's format, land in
  `NEEDS_YOU.md`. If a verification email doesn't arrive within 8 minutes, so does the job.
- **Any term:** summer, fall, winter, spring, co-op and part-time internships are all in scope,
  as long as the term hasn't ended.
- **Alert sites:** the scheduled agent never opens LinkedIn, Handshake, Indeed or WayUp. Only
  assist mode does, in the candidate's own browser while they're present.
- You're responsible for what gets submitted in your name. Keep the resume and profile accurate
  and read `APPLIED.md` now and then.
