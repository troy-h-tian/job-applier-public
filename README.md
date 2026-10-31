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
     set, the subscription token is used and the API key is the fallback.
   - `DATA_REPO_TOKEN`: a fine-grained token with **Contents: Read and write** on the private repo.
   - `WORKDAY_PASSWORD` (optional, despite the name): the password for every candidate account
     the agent creates, on Workday and any other application site. Use one you don't reuse
     elsewhere. 8+ characters with upper, lower, number and symbol; no single quotes. Claude
     never sees it: the browser tool fills it in. Without it, Workday jobs are skipped.
3. Set `DATA_REPO` in `.github/workflows/apply.yml` to the private repo's name.
4. Optional repository **variables**: `CLAUDE_MODEL` (default `sonnet`), `MAX_BATCHES_PER_RUN`
   (default `0` = no cap, see **Usage** below), `USAGE_CAP_5H_USD` / `USAGE_CAP_WEEK_USD`
   (estimated usage caps, defaults 10 and 120; `0` = off), `MAX_BUDGET_USD` (spending cap per job,
   API key only; none by default). The API key's Anthropic account needs credit
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
stops if two jobs in a row record nothing (for example, no API credit). `--firefox` uses a separate
Firefox window instead (untested).

It uses the `claude` login (subscription). `USE_API_KEY=1` uses the API key in
`~/.config/job-applier/env` (`ANTHROPIC_API_KEY=...`, kept outside the repo) instead.

`scripts/update_profiles.sh` uses the same setup to bring the candidate's Handshake, WayUp and
Indeed profiles in line with `resume.md` (`prompts/update_profiles.md`): it adds what's missing and
corrects what contradicts the resume, keeps true extra details, uploads the resume PDF, and never
changes account, privacy or EEO settings. Don't run it while `assist.sh` is running.

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

Runs use your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`) when it's set, otherwise
`ANTHROPIC_API_KEY` (billed per token). The subscription allows a certain amount per 5-hour window
plus a weekly cap, shared with your own use of Claude. The schedule is 8:17am, 1:27pm and 6:37pm
Pacific (an hour earlier in winter), just over 5 hours apart, so each run starts in a fresh window,
while you're awake to approve emailed codes. Each run keeps going until the queue is empty, no backend can take more jobs, or
`MAX_BATCHES_PER_RUN` batches are done (default: no limit). To leave you room, Claude is only used
while its estimated usage is under `USAGE_CAP_5H_USD` (default 10) and `USAGE_CAP_WEEK_USD` (default
120), checked before every Claude session (`scripts/usage_guard.py`); Codex and Gemini are not
counted. Once Claude is capped or reports a usage limit, jobs only run on the other backends.

Each job's cost and token counts are appended to `data/usage.jsonl` (`cost_usd` is the
API-equivalent price; on a subscription nothing extra is charged). With an API key nothing caps
spending unless you set one of the variables above.

What keeps it cheap (`scripts/agent.py`, `mcp.json`, `prompts/apply.md`): every turn re-reads the
whole conversation, so the cost is mostly context size times turns.

- Each job gets its own fresh Claude session, so one job's pages never ride along with the next.
  The instructions and candidate files come first in every prompt and the job last, so that
  shared part is cached.
- The agent has only the tools it needs (`Read`, `WebSearch`, the browser and its own tools), not
  Claude Code's full set, which would add ~17k tokens to every turn.
- Browser actions return just the URL and title (`--snapshot-mode none`); the agent looks things
  up with `browser_find` and small, targeted snapshots instead of whole-page dumps.
- Screenshots are saved for you but never sent to the agent (`--image-responses omit`); when it
  needs to see pixels, `describe_screenshot` asks Haiku.
- `KEEP_SESSIONS=1` keeps the session transcripts (in `~/.claude/projects/`) for looking into
  where a costly job's tokens went.

### Gemini and Codex first, then Claude

Each job runs on the first backend in the list that is installed, signed in and under its usage
limit, with Claude as the last resort. All get the same prompt and browser tools.

- **On the candidate's Mac** (assist, local runs, the profile update): Gemini, then Codex, then Claude.
- **In GitHub Actions:** Codex, then Claude (no Gemini login there). `BACKENDS` overrides the list.

1. **Gemini** (the Google AI plan): `brew install --cask antigravity-cli`, then run `agy` once to
   sign in. `agy` can't switch off its own shell and file tools, so each session runs with a
   workspace `.agents/hooks.json` hook (`scripts/agent.py`, `GATE`) that denies every tool except
   calls to the browser and applier servers, plus permission rules approving only those two. It
   never uses `--dangerously-skip-permissions`. The `.agents/` folder exists only while a session
   runs. `GEMINI_MODEL` picks the model (default `gemini-3.8-flash-high`).
2. **Codex** (the ChatGPT plan): `npm i -g --prefix ~/.npm-global @openai/codex`, then
   `codex login` and "Sign in with ChatGPT". Its shell and built-in browser are switched off.
   *GitHub Actions* uses a second, dedicated login (`CODEX_HOME=~/.codex-github codex login`, then
   copy its `auth.json` to `data/codex_auth.json` in the private repo). The workflow restores it
   before each run and saves the refreshed copy afterwards, so it never shares a refresh chain with
   the Mac's login. Without that file the workflow uses Claude only.
3. **Claude**: the subscription, or the API key.

Gemini and Codex have no usage caps here. When one reports a usage limit it's paused until the
reset time it gives (or an hour; `data/.paused_<name>`) and the job goes to the next one. A backend
that fails for another reason is skipped for the rest of the batch. `USE_CODEX=0` / `USE_GEMINI=0`
turn one off. Only Claude's cost is counted by the usage caps below.

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
