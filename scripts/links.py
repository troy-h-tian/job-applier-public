"""Resolve links from job-alert emails to the employer's page, without visiting alert sites.

Email links usually pass through click trackers (bbclick.wayup.com, email.g.joinhandshake.com,
...). We follow those redirects one hop at a time, which is what clicking the link does, but stop
before requesting any page on the alert sites themselves (www.wayup.com, linkedin.com,
app.joinhandshake.com): those links are dropped, since their terms forbid automated access.
"""

import urllib.error
import urllib.parse
import urllib.request

from common import BLOCKED_SITES

USER_AGENT = "Mozilla/5.0 (job-applier; personal use)"
MAX_HOPS = 8
# Subdomains of blocked sites that serve their actual pages (as opposed to click trackers).
MAIN_PREFIXES = ("", "www.", "app.", "m.", "jobs.", "careers.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _is_alert_site_page(host):
    return any(host == p + d for d in BLOCKED_SITES for p in MAIN_PREFIXES)


def _is_alert_domain(host):
    return any(host == d or host.endswith("." + d) for d in BLOCKED_SITES)


def resolve(url):
    """Return the employer URL a tracked link leads to, or "" if it leads to an alert site."""
    for _ in range(MAX_HOPS):
        host = urllib.parse.urlparse(url).netloc.lower()
        if not host or _is_alert_site_page(host):
            return ""
        if not _is_alert_domain(host) and not _looks_like_tracker(host):
            return url  # an employer or ATS page: done, without fetching it
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with _opener.open(req, timeout=20) as resp:
                return "" if _is_alert_domain(host) else url  # no redirect: not a tracker after all
        except urllib.error.HTTPError as e:
            location = e.headers.get("Location") if 300 <= e.code < 400 else None
            if not location:
                return ""
            url = urllib.parse.urljoin(url, location)
        except (urllib.error.URLError, TimeoutError, ValueError):
            return ""
    return ""


def _looks_like_tracker(host):
    """Generic email click-tracking hosts (SendGrid, Mailchimp, HubSpot, Braze, ...)."""
    first = host.split(".")[0]
    return (
        first in ("click", "clicks", "ct", "track", "trk", "links", "link", "email", "url", "e", "r", "go", "l")
        or any(t in host for t in ("sendgrid.net", "list-manage.com", "hubspotlinks", "mailchi.mp", "braze", "mandrillapp"))
    )
