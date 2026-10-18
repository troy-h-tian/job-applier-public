# job-applier

An unattended internship application agent. Twice a day, GitHub Actions:

1. **Discovers** new postings from company job boards (Greenhouse, Lever, Ashby and Workday
   public APIs), the SimplifyJobs internship feed, job-alert emails (WayUp, Handshake, LinkedIn),
   and hand-picked links, filtered by title, location and age (`scripts/discover.py`,
   `scripts/inbox.py`).
2. **Screens** each posting cheaply: its description is fetched from the job board's API and
   Claude Haiku skips hard mismatches (degree level, graduation window, term, country)
   (`scripts/prescreen.py`).
3. **Applies** by running Claude Code headlessly with a Playwright browser (`scripts/run.sh`,
   `prompts/apply.md`). Claude scores fit against the candidate's resume, fills out the form,
   uploads the resume, writes short grounded answers, and submits.
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
resume/resume.md, .pdf    resume text (what Claude may say) and the file to upload
data/                     application log, reports, leads, synced emails, private run logs
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
4. Optional repository **variables**: `CLAUDE_MODEL` (default `sonnet`), `MAX_BUDGET_USD`
   (spending cap per batch of 15 jobs; none by default).
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

## Tuning (in the private `config/search.yaml`)

- **Where it looks:** board names under `sources`, plus Workday career-site URLs.
- **How picky:** `min_fit_score`, the free-text `preferences`, and the `title_*` / `locations_*`
  pre-filters.
- **How much:** no self-imposed limits by default; each run works through the queue in batches of
  15 until it's empty, Claude's usage limit is hit, or GitHub's 6-hour job limit nears. Set any
  value under `limits` to cap it.

## Run locally

Clone the private repo into `private/` and link it into place, then run:

```bash
ln -s private/config config && ln -s private/resume resume && ln -s private/data data
pip install -r requirements.txt
APPLIER_MODE=dry_run scripts/run.sh   # uses your logged-in Claude Code and local Chrome
```

## Limits and caveats

- Supported application sites: Greenhouse, Lever, Ashby, Workday. iCIMS, Taleo and custom
  portals are filtered out.
- Some Workday sites require email verification before you can apply; those land in
  `NEEDS_YOU.md`.
- You're responsible for what gets submitted in your name. Keep the resume and profile accurate
  and read `APPLIED.md` now and then.
