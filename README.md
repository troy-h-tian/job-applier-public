# job-applier

An unattended internship application agent. Three times a day, GitHub Actions:

1. **Discovers** new postings from company job boards (Greenhouse, Lever, Ashby and Workday
   public APIs), the SimplifyJobs internship feed, job-alert emails (WayUp, Handshake, LinkedIn),
   and hand-picked links, filtered by title, location and age (`scripts/discover.py`,
   `scripts/inbox.py`).
2. **Screens** each posting cheaply: its description is fetched from the job board's API and
   Claude Haiku skips hard mismatches (degree level, graduation window, term, country)
   (`scripts/prescreen.py`).
3. **Applies** by running Claude Code headlessly with a Playwright browser (`scripts/run.sh`,
   `prompts/apply.md`). Claude scores fit against the candidate's resume, fills out the form,
   uploads the resume (optionally a copy tailored to the company, see below) and the transcript if
   asked, writes short answers grounded in the resume and the company's stated values, and submits.
4. **Records every outcome** in the private data repo: the application log, `APPLIED.md`, and
   `NEEDS_YOU.md` for anything that needs a human (CAPTCHAs, unanswerable questions).

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
data/                     application log, reports, leads, synced emails, private run logs,
                          tailored resumes (data/tailored/) and per-batch usage (data/usage.jsonl)
```

Each run checks the private repo out into `private/`, links `config/`, `resume/` and `data/` into
place, and pushes results back. The public log shows only counts: the agent's own output goes to
`data/logs/` in the private repo, and no screenshots or artifacts are uploaded. Each run also
updates `last-run.txt` here, because GitHub disables scheduled workflows in public repos after 60
days without activity.

## Setup

1. Create the private data repo with the layout above.
2. In this repo's **Settings → Secrets and variables → Actions**, add:
   - `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) or `ANTHROPIC_API_KEY`.
   - `DATA_REPO_TOKEN`: a fine-grained token with **Contents: Read and write** on the private repo.
   - `WORKDAY_PASSWORD` (optional): used for every Workday account the agent creates. 8+
     characters with upper, lower, number and symbol; no single quotes. Claude never sees it:
     the browser tool fills it in.
3. Set `DATA_REPO` in `.github/workflows/apply.yml` to the private repo's name.
4. Optional repository **variables**: `CLAUDE_MODEL` (default `sonnet`), `MAX_BATCHES_PER_RUN`
   (default `6`; `0` = no cap, see **Usage** below), `MAX_BUDGET_USD` (spending cap per batch of
   15 jobs, API key only; none by default).
5. Run it once from **Actions → Job applier → Run workflow**.

## Job-alert emails

WayUp, Handshake and LinkedIn forbid automated access to their sites, so the agent never visits
them. It reads the alert emails they send instead:

1. `integrations/gmail_to_repo.gs`, a Google Apps Script in each Gmail account that receives
   alerts, copies new alert emails (with their link targets) into the private repo's
   `data/inbox/` every 2 hours.
2. Each run, Claude Haiku extracts each company + title + link into `data/leads.json`.
3. Links are followed through click trackers to the employer's page, stopping before any
   WayUp/LinkedIn/Handshake page (`scripts/links.py`). Otherwise the company's
   Greenhouse/Lever/Ashby board is looked up and the title matched there (`scripts/boards.py`);
   boards found this way also become regular sources. Only leads still without a link fall back
   to web search.

Apps Script setup: script.google.com → New project → paste the script → Project Settings →
Script properties → `GITHUB_TOKEN` = a fine-grained token with Contents: Read and write on the
private repo → run `install`. Set `REPO` at the top of the script to the private repo.

## Emailed verification codes

Some sites (often Greenhouse) email a security code before accepting an application. Replying
**"yes"** to that email approves that one submission: the Apps Script relays only that email, the
waiting agent enters the code and submits, and the relayed copy is deleted. Codes without a
"yes" reply are never relayed. CAPTCHAs are never attempted.

Workday's "verify your candidate account" emails are relayed automatically (no reply needed),
because the agent created that account itself: only their links on
`myworkdayjobs.com`/`myworkday.com`. The sender isn't checked, since it varies by company
(`@myworkday.com`, `@otp.workday.com`, the company's own domain). The agent opens the link from the same Workday site
it is applying on, then signs in and continues. After updating the Apps Script, run `install`
again so the new trigger is created.

## Assist mode (CAPTCHAs)

The agent never solves CAPTCHAs, so jobs that hit one are recorded as `needs_human`. To finish
them with the candidate at the screen, run on a Mac:

```bash
scripts/assist.sh --list    # the jobs waiting on a CAPTCHA or bot check
scripts/assist.sh           # retry up to 15 of them (--max N, or pass job ids)
```

A visible browser opens and the agent refills each form, reusing the answers it wrote last time.
When a CAPTCHA, security code or Workday verification comes up, a dialog asks the candidate to do
that step in the browser and click Done; the agent then submits and records the outcome, and the
script pushes the results. In Claude Code, the `captcha-assist` skill does all of this from one
request.

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

Runs use your Claude subscription (`CLAUDE_CODE_OAUTH_TOKEN`), which allows a certain amount per
5-hour window plus a weekly cap, shared with your own use of Claude. The schedule is 8:17am,
1:27pm and 6:37pm Pacific (an hour earlier in winter), just over 5 hours apart, so each run starts
in a fresh window, while you're awake to approve emailed codes. Each run stops after
`MAX_BATCHES_PER_RUN` batches (default 6) or as soon as Claude reports a usage limit, leaving some
of the window for you.

Each batch's cost is appended to `data/usage.jsonl` (`cost_usd` is the API-equivalent price; on a
subscription nothing extra is charged). To calibrate, compare a few days of it with
**claude.ai → Settings → Usage**: if the weekly bar fills before the week ends, lower
`MAX_BATCHES_PER_RUN` or remove a cron line; if it never gets close, raise it.

## Tuning (in the private `config/search.yaml`)

- **Where it looks:** board names under `sources`, plus Workday career-site URLs.
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

- Supported application sites: Greenhouse, Lever, Ashby, Workday. iCIMS, Taleo and custom
  portals are filtered out.
- Workday account-verification emails are relayed automatically; if one doesn't arrive within
  8 minutes, the job lands in `NEEDS_YOU.md`.
- You're responsible for what gets submitted in your name. Keep the resume and profile accurate
  and read `APPLIED.md` now and then.
