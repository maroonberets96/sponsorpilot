"""Best-effort discovery of a hiring-contact email for shortlisted jobs.

Scans the job description first, then the live posting page, then the
employer's own website (careers / contact pages). Most board postings publish
no contact at all, so callers must treat None as normal.
"""
import re
import urllib.parse

import httpx

from logger import get_logger
from scraper import _find_career_page_yahoo, _is_aggregator

logger = get_logger()

TIMEOUT = 20
# TLD must be alphabetic: also filters JS package strings like react@18.3.1
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}")

# Local parts that are never an application contact
GENERIC_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "privacy", "unsubscribe", "webmaster", "postmaster", "abuse",
    "security", "dpo", "gdpr", "complaints",
    # Corporate inboxes on company websites that never handle hiring
    "companysecretary", "company.secretary", "cosec", "legal", "investor",
    "press", "media", "sales", "billing", "accounts", "invoice", "marketing",
    "procurement", "suppliers", "donat", "fundrais",
)
# Short local parts matched exactly (as prefixes they would hit real names)
GENERIC_LOCALS = ("ir", "pr", "ap", "ar")
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
    if local.startswith(GENERIC_PREFIXES) or local in GENERIC_LOCALS:
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


HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Hosted applicant-tracking systems: a posting that redirects here tells us
# nothing about the employer's own domain
ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "workday", "myworkdayjobs", "workable.com",
    "smartrecruiters", "bamboohr", "icims", "taleo", "jobvite", "ashbyhq",
    "successfactors", "recruitee", "teamtailor", "breezy.hr", "jazzhr",
    "applytojob", "dayforcehcm", "ultipro", "ukg", "paylocity", "adp.com",
    "oraclecloud", "hirebridge", "njoyn", "applicantpro", "pinpointhq",
)
# Two-label public suffixes, so "acme.co.uk" is the base, not "co.uk"
TWO_LABEL_SUFFIXES = ("co.uk", "org.uk", "ac.uk", "gov.uk", "nhs.uk", "ltd.uk", "plc.uk",
                      "gc.ca", "on.ca", "qc.ca", "bc.ca", "ab.ca", "com.au")
# On a company website, role inboxes are preferred over named people
SITE_INBOX_HINTS = PREFERRED_HINTS + ("info", "hello", "contact", "enquir", "inquir",
                                      "office", "admin", "general", "reception")
# A named person's address counts as a hiring contact when these words appear
# within CONTEXT_CHARS of it on the page (e.g. "Talent Acquisition: jo@...")
HIRING_CONTEXT_RE = re.compile(
    r"recruit|talent|hiring|human resources|hr|careers?|vacanc|apply|"
    r"applications?|people (?:team|partner)|resourcing", re.I)
CONTEXT_CHARS = 200
# Pages on the employer's site most likely to list a recruiting inbox
SITE_PATHS = ("", "/careers", "/jobs", "/contact", "/contact-us", "/about/contact")


def _base_domain(netloc):
    labels = netloc.lower().split(":")[0].removeprefix("www.").split(".")
    keep = 3 if ".".join(labels[-2:]) in TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def _employer_domain(url):
    """Base domain of `url` if it looks like the employer's own site, else None."""
    if not url or _is_aggregator(url):
        return None
    netloc = urllib.parse.urlparse(url).netloc.lower()
    if not netloc or any(d in netloc for d in ATS_DOMAINS + EXCLUDED_DOMAINS):
        return None
    return _base_domain(netloc)


def _get(url):
    return httpx.get(url, timeout=TIMEOUT, follow_redirects=True, headers=HEADERS)


def _search_careers_page(company):
    """First employer-site result for '<company> careers': DuckDuckGo's plain
    HTML endpoint (no browser needed, thread-safe), Yahoo as a fallback."""
    try:
        response = _get("https://html.duckduckgo.com/html/?q="
                        + urllib.parse.quote_plus(f"{company} careers"))
        for target in re.findall(r'uddg=([^&"]+)', response.text):
            result = urllib.parse.unquote(target)
            if "duckduckgo.com" not in result and _employer_domain(result):
                return result
    except Exception as e:
        logger.info(f"   DuckDuckGo search failed for {company}: {e}")
    return _find_career_page_yahoo(company)


def _from_company_site(domain, careers_url=None):
    """Scan the employer's careers/contact pages for an email on their own
    domain (off-domain addresses on a company site are usually unrelated).

    Returns (email, kind) or (None, None). kind, best first:
      "inbox"            - a role inbox (careers@, hr@, info@ ...)
      "hiring person"    - a named person mentioned near hiring words
      "person"           - any other named person at the company
    """
    urls = [careers_url] if careers_url else []
    urls += [f"https://{domain}{path}" for path in SITE_PATHS]
    inboxes, hiring_people, people = [], [], []
    for page in dict.fromkeys(urls):  # de-dupe, keep order
        try:
            response = _get(page)
        except Exception:
            continue
        if response.status_code >= 400:
            continue
        text = response.text
        for match in EMAIL_RE.finditer(text):
            email = _plausible(match.group())
            if not email or _base_domain(email.partition("@")[2]) != domain:
                continue
            if email.partition("@")[0].startswith(SITE_INBOX_HINTS):
                bucket = inboxes
            else:
                nearby = text[max(0, match.start() - CONTEXT_CHARS):match.end() + CONTEXT_CHARS]
                bucket = hiring_people if HIRING_CONTEXT_RE.search(nearby) else people
            if email not in bucket:
                bucket.append(email)
        found = _pick([" ".join(inboxes)])
        if found and any(hint in found.partition("@")[0] for hint in PREFERRED_HINTS):
            return found, "inbox"  # recruiting inbox: no need to fetch more pages
    for bucket, kind in ((inboxes, "inbox"), (hiring_people, "hiring person"), (people, "person")):
        if bucket:
            return _pick([" ".join(bucket)]), kind
    return None, None


def find_contact(url, description, company=None):
    """Returns (email, source) for the posting, or (None, None).

    source is "posting" (description or posting page), or for the employer's
    site "company website" (role inbox), "company website - hiring person" or
    "company website - person".
    """
    email = _pick([description])
    if email:
        return email, "posting"

    final_url = None
    if url:
        try:
            response = _get(url)
            final_url = str(response.url)
            email = _pick([response.text])
            if email:
                return email, "posting"
        except Exception as e:
            logger.info(f"   Contact-email lookup failed for {url}: {e}")

    # Fall back to the employer's own site: the posting often redirects there,
    # otherwise search for their careers page.
    domain = _employer_domain(final_url)
    careers_url = None
    if not domain and company:
        careers_url = _search_careers_page(company)
        domain = _employer_domain(careers_url)
    if not domain:
        return None, None
    email, kind = _from_company_site(domain, careers_url)
    if email:
        logger.info(f"   Found {email} ({kind}) on {company or domain}'s website.")
        return email, "company website" if kind == "inbox" else f"company website - {kind}"
    return None, None


def find_contact_email(url, description, company=None):
    """Returns a likely hiring-contact email for the posting, or None."""
    return find_contact(url, description, company)[0]
