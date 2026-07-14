"""Best-effort discovery of a hiring-contact email for shortlisted jobs.

Scans the job description first, then the live posting page. Most board
postings publish no contact at all, so callers must treat None as normal.
"""
import re

import httpx

from logger import get_logger

logger = get_logger()

TIMEOUT = 20
# TLD must be alphabetic: also filters JS package strings like react@18.3.1
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")

# Local parts that are never an application contact
GENERIC_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "privacy", "unsubscribe", "webmaster", "postmaster", "abuse",
    "security", "dpo", "gdpr", "complaints",
)
# Domains belonging to boards/infrastructure, not the employer
EXCLUDED_DOMAINS = (
    "adzuna", "reed.co.uk", "jooble", "indeed", "linkedin", "totaljobs",
    "cv-library", "cvlibrary", "ziprecruiter", "glassdoor", "monster",
    "sentry.io", "wixpress", "example.", "sentry-next", "cloudfront",
    "domain.com", "email.com", "test.com", "yourcompany", "mysite",
)
# Template placeholders found on posting pages ('your.email@domain.com')
PLACEHOLDER_LOCALS = (
    "your", "name", "firstname", "lastname", "john.doe", "jane.doe",
    "user", "someone", "sample", "email", "me", "test",
)
# Local parts that suggest a recruiting inbox - picked first when present
PREFERRED_HINTS = ("recruit", "career", "job", "talent", "hr", "hiring", "people", "apply")


def _plausible(email):
    """Returns a cleaned lowercase email, or None if it is junk."""
    email = email.lower().strip().rstrip(".")
    local, _, domain = email.partition("@")
    if local.startswith(GENERIC_PREFIXES):
        return None
    if local in PLACEHOLDER_LOCALS or local.startswith(("your.", "your_", "firstname.")):
        return None
    if any(excluded in domain for excluded in EXCLUDED_DOMAINS):
        return None
    # the regex also matches asset filenames like logo@2x.png
    if domain.rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "webp", "svg", "js", "css"):
        return None
    return email


def _pick(texts):
    """Collects plausible emails from the texts, preferring recruiting inboxes."""
    candidates = []
    for text in texts:
        for match in EMAIL_RE.findall(text or ""):
            email = _plausible(match)
            if email and email not in candidates:
                candidates.append(email)
    for email in candidates:
        if any(hint in email.partition("@")[0] for hint in PREFERRED_HINTS):
            return email
    return candidates[0] if candidates else None


def find_contact_email(url, description):
    """Returns a likely hiring-contact email for the posting, or None."""
    email = _pick([description])
    if email:
        return email
    if not url:
        return None
    try:
        response = httpx.get(
            url, timeout=TIMEOUT, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        return _pick([response.text])
    except Exception as e:
        logger.info(f"   Contact-email lookup failed for {url}: {e}")
        return None
