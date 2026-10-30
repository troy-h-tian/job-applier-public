"""Shared paths and helpers for the job applier scripts."""

import datetime as dt
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
APPLICATIONS = DATA / "applications.jsonl"
QUEUE = DATA / "queue.json"
SEARCH_CONFIG = ROOT / "config" / "search.yaml"

# Outcomes that permanently retire a job id. dry_run only counts while in dry_run mode,
# so switching to submit revisits jobs that were only rehearsed.
FINAL_STATUSES = {"submitted", "skipped", "needs_human"}
MAX_ERROR_ATTEMPTS = 2


def load_search_config():
    return yaml.safe_load(SEARCH_CONFIG.read_text())


def effective_mode(cfg):
    import os

    override = os.environ.get("APPLIER_MODE", "").strip()
    mode = override if override in ("dry_run", "submit") else cfg.get("mode", "dry_run")
    if mode not in ("dry_run", "submit"):
        raise SystemExit(f"config/search.yaml: mode must be dry_run or submit, got {mode!r}")
    return mode


def load_applications():
    if not APPLICATIONS.exists():
        return []
    rows = []
    for line in APPLICATIONS.read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def retired_ids(rows, mode):
    final = FINAL_STATUSES | ({"dry_run"} if mode == "dry_run" else set())
    errors = {}
    retired = set()
    for r in rows:
        if r["status"] in final:
            retired.add(r["id"])
        elif r["status"] == "error":
            errors[r["id"]] = errors.get(r["id"], 0) + 1
        elif r["status"] == "requeued":  # e.g. a needs_human job after its blocker was fixed
            retired.discard(r["id"])
            errors.pop(r["id"], None)
    retired.update(i for i, n in errors.items() if n >= MAX_ERROR_ATTEMPTS)
    return retired


def utc_today():
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def normalize(text):
    """Loose key for matching a company/title across sources ("Nissan USA, Inc." ~ "nissan")."""
    import re

    text = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|usa|us|company|the)\b", " ", text.lower())
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


# needs_human reasons the assist session (scripts/assist.sh) handles with the candidate at the
# screen: CAPTCHAs, bot checks, and emailed codes or account-verification links.
ASSIST_PATTERN = re.compile(
    r"captcha|bot check|cloudflare|(verification|validation|security|one-time) code|verif(y|ication) (email|link)|"
    r"email verification|account (needs )?verif", re.I)


def needs_assist(row):
    return row["status"] == "needs_human" and bool(ASSIST_PATTERN.search(row["reason"] + " " + row.get("notes", "")))


# Job boards whose terms forbid automated access. The agent never opens these.
BLOCKED_SITES = ("wayup.com", "linkedin.com", "lnkd.in", "joinhandshake.com", "handshake.com", "indeed.com")


def is_blocked_site(url):
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in BLOCKED_SITES)


def limit(cfg, key):
    """A positive int from config/search.yaml `limits`, or None meaning no limit."""
    value = (cfg.get("limits") or {}).get(key)
    return value if isinstance(value, int) and value > 0 else None


def counted_today(rows, mode):
    counted = {"submitted"} | ({"dry_run"} if mode == "dry_run" else set())
    today = utc_today()
    return sum(1 for r in rows if r["status"] in counted and r["at"].startswith(today))
