"""Find an email lead's posting on the company's own job board, without web search.

For each company name, try likely board names on Greenhouse, Lever and Ashby's public APIs
("Together AI" -> togetherai, together-ai, together), then match the lead's title against that
board's postings. Results, including misses, are cached in data/company_boards.json, and boards
found this way also become regular discovery sources (see discover.py).
"""

import difflib
import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import DATA, normalize

CACHE = DATA / "company_boards.json"
USER_AGENT = "Mozilla/5.0 (job-applier; personal use)"
SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the",
            "technologies", "technology", "labs", "lab", "ai", "usa", "us"}
API = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{}/jobs",
    "lever": "https://api.lever.co/v0/postings/{}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{}",
}


def slug_candidates(company):
    words = normalize(company).split()
    core = [w for w in words if w not in SUFFIXES] or words
    # Whole-name guesses only: a single word of a longer name ("general" for General Motors)
    # usually belongs to some other company.
    cands = ["".join(words), "-".join(words), "".join(core), "-".join(core)]
    out = []
    for c in cands:
        if c and len(c) > 2 and c not in out:
            out.append(c)
    return out[:5]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def _postings(ats, slug):
    """[(title, url)] for a board, or None if it doesn't exist."""
    data = _get(API[ats].format(slug))
    if ats == "greenhouse" and isinstance(data, dict) and "jobs" in data:
        return [(j["title"], j["absolute_url"]) for j in data["jobs"]]
    if ats == "lever" and isinstance(data, list):
        return [(j["text"], j.get("applyUrl") or j["hostedUrl"]) for j in data]
    if ats == "ashby" and isinstance(data, dict) and "jobs" in data:
        return [(j["title"], j.get("applyUrl") or j["jobUrl"]) for j in data["jobs"] if j.get("isListed", True)]
    return None


def _greenhouse_company_matches(slug, company):
    """Greenhouse lists each job's company name; reject boards that belong to someone else."""
    data = _get(API["greenhouse"].format(slug)) or {}
    names = {normalize(j.get("company_name") or "") for j in data.get("jobs", [])[:20]} - {""}
    want = set(normalize(company).split()) - SUFFIXES
    return not names or any(want <= set(n.split()) for n in names)


def find_board(company):
    """{"ats":..., "slug":...} for the first board found, or {} if none."""
    for slug in slug_candidates(company):
        for ats in API:
            jobs = _postings(ats, slug)
            if not jobs:  # missing, or empty and so unconfirmable
                continue
            if ats == "greenhouse" and not _greenhouse_company_matches(slug, company):
                continue
            return {"ats": ats, "slug": slug}
    return {}


def best_match(title, postings):
    """URL of the posting whose title clearly matches, else ""."""
    want = normalize(title)
    scored = [(difflib.SequenceMatcher(None, want, normalize(t)).ratio(), u) for t, u in postings]
    scored.sort(reverse=True)
    return scored[0][1] if scored and scored[0][0] >= 0.82 else ""


def load_cache():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def resolve_leads(leads):
    """Fill in `url` for leads whose company has a findable board. Returns how many were resolved."""
    cache = load_cache()
    todo = sorted({l["company"] for l in leads if not l.get("url") and normalize(l["company"]) not in cache})
    with ThreadPoolExecutor(max_workers=8) as pool:
        for company, board in zip(todo, pool.map(find_board, todo)):
            cache[normalize(company)] = board
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True) + "\n")

    postings_by_board = {}
    resolved = 0
    for lead in leads:
        if lead.get("url") or lead.get("board_checked"):
            continue
        lead["board_checked"] = True
        board = cache.get(normalize(lead["company"])) or {}
        if not board:
            continue
        key = (board["ats"], board["slug"])
        if key not in postings_by_board:
            postings_by_board[key] = _postings(*key) or []
        url = best_match(lead["title"], postings_by_board[key])
        if url:
            lead["url"] = url
            resolved += 1
    return resolved


def discovered_boards():
    """{"greenhouse": [...], "lever": [...], "ashby": [...]} of boards found for email-lead companies."""
    out = {ats: [] for ats in API}
    for board in load_cache().values():
        if board and board["slug"] not in out[board["ats"]]:
            out[board["ats"]].append(board["slug"])
    return out
