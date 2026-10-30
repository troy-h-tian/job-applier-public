"""Find new postings from the configured sources and write data/queue.json for the agent."""

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import boards
import links

from common import (
    DATA,
    ROOT,
    is_blocked_site,
    normalize,
    QUEUE,
    counted_today,
    effective_mode,
    load_applications,
    load_search_config,
    retired_ids,
    limit,
)

USER_AGENT = "Mozilla/5.0 (job-applier; personal use)"
LEADS = DATA / "leads.json"
SHORT_LINKS = DATA / "short_links.json"  # cache: feed short link -> where it redirects
# Feeds whose listings point at a link shortener instead of the job board.
SHORT_LINK_HOSTS = ("go.vansh.nyc",)
DESCRIPTION_CHARS = 6000


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def html_to_text(raw):
    text = html.unescape(raw or "")
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def iso_from_epoch(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


def job_id_from_url(url):
    """Map an ATS URL to the same id the direct board fetchers produce, for dedupe."""
    parsed = urllib.parse.urlparse(url)
    host, path = parsed.netloc.lower(), parsed.path
    if "greenhouse.io" in host:
        m = re.search(r"/jobs/(\d+)", path) or re.search(r"(?:gh_jid|token)=(\d+)", parsed.query)
        if m:
            return f"gh:{m.group(1)}"
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", path)
    if host == "jobs.lever.co" and m:
        return f"lever:{m.group(1)}"
    if host == "jobs.ashbyhq.com" and m:
        return f"ashby:{m.group(1)}"
    if host.endswith("myworkdayjobs.com"):
        req = re.search(r"_([A-Za-z0-9-]+)$", re.sub(r"/apply.*$", "", path.rstrip("/")))
        if req:
            return f"wd:{host.split('.')[0]}:{req.group(1)}"
    return f"url:{host}{path.rstrip('/')}"


def greenhouse(board):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"gh:{j['id']}",
            "company": j.get("company_name") or board,
            "title": j["title"].strip(),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j["absolute_url"],
            "posted_at": j.get("first_published") or j.get("updated_at"),
            "description": html_to_text(j.get("content")),
        }


def lever(site):
    for j in fetch_json(f"https://api.lever.co/v0/postings/{site}?mode=json"):
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location", "")]
        text = "\n\n".join(
            [j.get("descriptionPlain", "")]
            + [f"{l['text']}\n{html_to_text(l['content'])}" for l in j.get("lists", [])]
            + [j.get("additionalPlain", "")]
        )
        yield {
            "id": f"lever:{j['id']}",
            "company": site,
            "title": j["text"].strip(),
            "location": " / ".join(filter(None, locs + [j.get("workplaceType", "")])),
            "url": j.get("applyUrl") or j["hostedUrl"],
            "posted_at": iso_from_epoch(j["createdAt"] / 1000) if j.get("createdAt") else None,
            "description": text,
        }


def ashby(board):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        yield {
            "id": f"ashby:{j['id']}",
            "company": board,
            "title": j["title"].strip(),
            "location": " / ".join(filter(None, locs)),
            "url": j.get("applyUrl") or j["jobUrl"],
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain", ""),
        }


def workday_posted(text):
    """Turn Workday's "Posted 5 Days Ago" / "Posted 30+ Days Ago" into an ISO date."""
    text = (text or "").lower()
    if "today" in text:
        days = 0
    elif "yesterday" in text:
        days = 1
    else:
        m = re.search(r"(\d+)", text)
        if not m:
            return None
        days = int(m.group(1))
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


def workday(site_url, search_text, max_results):
    parsed = urllib.parse.urlparse(site_url)
    host, site = parsed.netloc, parsed.path.strip("/").split("/")[-1]
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    for offset in range(0, max_results, 20):
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search_text})
        req = urllib.request.Request(
            api, data=body.encode(), headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            postings = json.load(resp).get("jobPostings", [])
        for j in postings:
            path = j["externalPath"]
            loc = j.get("locationsText", "")
            url = f"https://{host}/{site}{path}"
            yield {
                "id": job_id_from_url(url),
                "company": tenant,
                "title": j["title"].strip(),
                # "3 Locations" says nothing useful; leave it unknown so the agent checks the page.
                "location": "" if re.fullmatch(r"\d+ Locations", loc) else loc,
                "url": url,
                "posted_at": workday_posted(j.get("postedOn")),
                "description": "",
            }
        if len(postings) < 20:
            break


def simplify(feed_url, apply_hosts, categories):
    """SimplifyJobs-format listings.json (also used by vanshb03/Summer2027-Internships)."""
    for j in fetch_json(feed_url):
        if not j.get("active") or not j.get("is_visible", True):
            continue
        # Some feeds have no categories; vanshb03's mixes in off-season listings instead.
        if categories and j.get("category") and j["category"] not in categories:
            continue
        if j.get("season") and "Summer" not in j["season"]:
            continue
        url = j["url"]
        host = urllib.parse.urlparse(url).netloc.lower()
        # Short links are resolved, and checked against apply_hosts, after the title filters.
        if host not in SHORT_LINK_HOSTS and not any(h in host for h in apply_hosts):
            continue
        yield {
            "id": job_id_from_url(url),
            "company": j.get("company_name", ""),
            "title": j.get("title", "").strip(),
            "location": " / ".join(j.get("locations") or []),
            "url": url,
            "posted_at": iso_from_epoch(j["date_posted"]) if j.get("date_posted") else None,
            "description": "",  # the agent reads it from the posting page
            "sponsorship": j.get("sponsorship"),
        }


def resolve_short_links(jobs, apply_hosts):
    """Swap feed short links for the job board URL they redirect to; drop non-ATS targets."""
    def is_short(url):
        return urllib.parse.urlparse(url).netloc.lower() in SHORT_LINK_HOSTS

    if not any(is_short(j["url"]) for j in jobs):
        return jobs
    cache = json.loads(SHORT_LINKS.read_text()) if SHORT_LINKS.exists() else {}
    todo = list({j["url"] for j in jobs if is_short(j["url"])} - cache.keys())
    with ThreadPoolExecutor(max_workers=8) as pool:
        for short, target in zip(todo, pool.map(links.resolve, todo)):
            if target:  # "" is a failed request: try again next run
                cache[short] = target
    if todo:
        DATA.mkdir(exist_ok=True)
        SHORT_LINKS.write_text(json.dumps(cache, indent=1, sort_keys=True))
    out = []
    for j in jobs:
        if not is_short(j["url"]):
            out.append(j)
            continue
        url = cache.get(j["url"], j["url"])
        host = urllib.parse.urlparse(url).netloc.lower()
        if is_short(url) or not any(h in host for h in apply_hosts):
            continue
        out.append({**j, "url": url, "id": job_id_from_url(url)})
    return out


def program_list(readme_url, sections):
    """Programs from a curated README table (zapplyjobs/underclassmen-internships).

    Rows look like `| [Name](url) | Status/Open Date | Year | Note |`. Only rows marked open are
    kept: the agent can't apply to a program that hasn't opened, and a row is picked up as soon
    as the list marks it open. The url is often a program landing page, not a form.
    """
    req = urllib.request.Request(readme_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode()
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.fullmatch(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", cells[0]) if line.startswith("|") else None
        if section not in sections or not m or len(cells) < 4:
            continue
        name, url = m.groups()
        status, year, note = cells[1], cells[2], " ".join(cells[3:])
        if "open" not in status.lower() and "rolling" not in status.lower() or is_blocked_site(url):
            continue
        job_id = job_id_from_url(url)
        query = urllib.parse.urlparse(url).query
        if job_id.startswith("url:") and query:  # e.g. one gem.com/form per company
            job_id += "?" + query
        yield {
            "id": job_id,
            "company": name,
            "title": name,
            "location": "",
            "url": url,
            "posted_at": None,
            "description": f"{section} (from a curated list for freshmen and sophomores). "
                           f"Status: {status}. Class years: {year}. {note}".strip(),
            "program": True,
        }


def extra_jobs(path):
    """Jobs the candidate pasted into config/extra_jobs.txt."""
    for line in path.read_text().splitlines() if path.exists() else []:
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        if is_blocked_site(url):
            host = urllib.parse.urlparse(url).netloc
            print(f"  ! skipping a {host} link in extra_jobs.txt: paste the employer's link instead", file=sys.stderr)
            continue
        yield {
            "id": job_id_from_url(url),
            "company": "",
            "title": "",
            "location": "",
            "url": url,
            "posted_at": None,
            "description": "",
            "manual": True,
        }


def matches_any(text, needles):
    """Case-insensitive whole-word match, so "intern" doesn't match "Internal Tools"."""
    return any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text, re.I) for n in needles)


def location_ok(part, cfg):
    include, exclude = cfg.get("locations_include") or [], cfg.get("locations_exclude") or []
    return (not include or matches_any(part, include)) and not matches_any(part, exclude)


def days_ago(days):
    """ISO timestamp `days` ago, or "" (matches nothing older) when there's no age limit."""
    if days is None:
        return ""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()


def passes_filters(job, cfg, cutoff):
    title, loc = job["title"], job["location"]
    if job.get("program"):  # names like "NASA" or "MLH Fellowship": the agent judges fit
        return True
    if cfg.get("title_require") and not matches_any(title, cfg["title_require"]):
        return False
    if cfg.get("title_include") and not matches_any(title, cfg["title_include"]):
        return False
    if matches_any(title, cfg.get("title_exclude") or []):
        return False
    if loc and not any(location_ok(part, cfg) for part in re.split(r"\s*(?:/|•|;|\|)\s*", loc)):
        return False
    if job.get("posted_at") and job["posted_at"] < cutoff:
        return False
    return True


def main():
    cfg = load_search_config()
    mode = effective_mode(cfg)
    rows = load_applications()
    retired = retired_ids(rows, mode)
    per_day = limit(cfg, "max_applications_per_day")
    remaining_today = None if per_day is None else max(0, per_day - counted_today(rows, mode))

    sources = cfg.get("sources", {})
    # Boards found for companies in job-alert emails (scripts/boards.py) become sources too.
    found = boards.discovered_boards()
    board_lists = {ats: list(dict.fromkeys((sources.get(ats) or []) + found[ats])) for ats in found}
    tasks = [(f"greenhouse/{b}", greenhouse, (b,)) for b in board_lists["greenhouse"]]
    tasks += [(f"lever/{b}", lever, (b,)) for b in board_lists["lever"]]
    tasks += [(f"ashby/{b}", ashby, (b,)) for b in board_lists["ashby"]]
    workday_enabled = bool(os.environ.get("WORKDAY_PASSWORD"))
    if not workday_enabled:
        print("  (WORKDAY_PASSWORD not set: skipping Workday jobs)")
    tasks += [
        (f"workday/{u.split('//')[1].split('.')[0]}", workday,
         (u, sources.get("workday_search", ""), sources.get("workday_max_results", 100)))
        for u in (sources.get("workday") or []) if workday_enabled
    ]
    apply_hosts = sources.get("apply_hosts") or ["greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "myworkdayjobs.com"]
    if not workday_enabled:
        apply_hosts = [h for h in apply_hosts if "myworkdayjobs.com" not in h]
    tasks += [(f"simplify/{u.split('/')[3]}", simplify, (u, apply_hosts, sources.get("simplify_categories"))) for u in sources.get("simplify_feeds") or []]
    tasks += [(f"list/{u.split('/')[3]}", program_list, (u, sources.get("program_list_sections") or []))
              for u in sources.get("program_lists") or []]

    def run(task):
        name, fn, args = task
        try:
            return name, list(fn(*args)), None
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
            return name, [], e

    cutoff = days_ago(limit(cfg, "max_posting_age_days"))
    candidates, seen = [], set()
    # A job found through an email lead is recorded under the lead id; also retire the real
    # posting's id (from the URL the agent applied on) so it isn't applied to twice.
    retired |= {job_id_from_url(r["url"]) for r in rows if r["id"] in retired and r.get("url")}
    seen_roles = {(normalize(r["company"]), normalize(r["title"])) for r in rows if r["id"] in retired}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, jobs, err in pool.map(run, tasks):
            if err:
                print(f"  ! {name}: {err}", file=sys.stderr)
                continue
            kept = resolve_short_links([j for j in jobs if passes_filters(j, cfg, cutoff)], apply_hosts)
            print(f"  {name}: {len(jobs)} postings, {len(kept)} match filters")
            for j in kept:
                # Direct board results come first in `tasks`, so they win dedupe (they carry descriptions).
                # The same role is often posted once per location; treat those as one job.
                role = (normalize(j["company"]), normalize(j["title"]))
                if j["id"] in seen or j["id"] in retired or role in seen_roles:
                    continue
                seen.add(j["id"])
                seen_roles.add(role)
                j["source"] = name
                j["description"] = j["description"][:DESCRIPTION_CHARS]
                candidates.append(j)

    # Leads from job-alert emails (scripts/inbox.py). Ones matching a posting found above are
    # already covered. The rest carry a direct employer link if the email had one; otherwise the
    # agent searches for the employer's posting.
    lead_cutoff = days_ago(limit(cfg, "max_lead_age_days"))
    lead_count = 0
    for lead in json.loads(LEADS.read_text()) if LEADS.exists() else []:
        role = (normalize(lead["company"]), normalize(lead["title"]))
        url_id = job_id_from_url(lead["url"]) if lead.get("url") else None
        if (lead["id"] in retired or role in seen_roles or lead["found_at"] < lead_cutoff
                or url_id in seen or url_id in retired):
            continue
        job = {**lead, "url": lead.get("url", ""), "posted_at": lead["found_at"], "description": "",
               "lead": True, "source": "email"}
        if passes_filters(job, cfg, cutoff):
            seen_roles.add(role)
            candidates.append(job)
            lead_count += 1
    if lead_count:
        print(f"  email leads: {lead_count} without a matching posting yet")

    # Newest first, then (stable sort) priority roles (ML/software engineering, `title_priority`)
    # ahead of everything else, and within each group email-alert leads ahead of board postings.
    # Other fields only reach the agent once the priority roles are used up.
    priority = cfg.get("title_priority") or []
    candidates.sort(key=lambda j: j.get("posted_at") or "", reverse=True)
    candidates.sort(key=lambda j: (not matches_any(j["title"], priority), not j.get("lead")))
    manual = [j for j in extra_jobs(ROOT / "config" / "extra_jobs.txt") if j["id"] not in retired]
    # Hand-picked jobs go first. If discovery found the same job, keep its richer details.
    found = {c["id"]: c for c in candidates}
    manual_ids = {j["id"] for j in manual}
    front = [{**found[j["id"]], "manual": True} if j["id"] in found else j for j in manual]
    candidates = front + [c for c in candidates if c["id"] not in manual_ids]
    if manual:
        print(f"  config/extra_jobs.txt: {len(manual)} not yet handled")
    # Spread each run across companies; some cap applications per candidate (e.g. 5 per 180 days).
    # Leads without a link cost several web searches each and often lead nowhere, so cap them per
    # run. Leads whose email included a direct employer link are as cheap as any other job.
    per_company, queue, searches = {}, [], 0
    max_searches = limit(cfg, "max_lead_searches_per_run")
    max_per_company = limit(cfg, "max_per_company_per_run")
    for j in candidates if remaining_today != 0 else []:
        key = normalize(j["company"])
        needs_search = j.get("lead") and not j.get("url")
        if needs_search and max_searches is not None and searches >= max_searches:
            continue
        if j.get("manual"):
            queue.append(j)
        elif max_per_company is None or per_company.get(key, 0) < max_per_company:
            searches += bool(needs_search)
            per_company[key] = per_company.get(key, 0) + 1
            queue.append(j)
    # Hand the agent one batch at a time (scripts/run.sh loops until the queue is empty), so a
    # long run doesn't overflow a single Claude session's context.
    queue = queue[: int(os.environ.get("QUEUE_BATCH", "15"))]

    DATA.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps(queue, indent=2))
    print(
        f"mode={mode} new_matching={len(candidates)} queued={len(queue)} "
        f"remaining_today={'unlimited' if remaining_today is None else remaining_today} "
        f"already_handled={len(retired)}"
    )


if __name__ == "__main__":
    main()
