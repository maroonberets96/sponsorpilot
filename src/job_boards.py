"""Job-board API clients (Adzuna, Reed and Jooble).

All offer free API keys:
  Adzuna: https://developer.adzuna.com  (ADZUNA_APP_ID + ADZUNA_APP_KEY)
  Reed:   https://www.reed.co.uk/developers  (REED_API_KEY, UK only)
  Jooble: https://jooble.org/api/about  (JOOBLE_API_KEY)

Each search returns normalized job dicts:
  {source, source_id, title, company, location, url, description,
   salary_min, salary_max, posted_date}
"""
import html
import os
import re
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

import config
from logger import get_logger

logger = get_logger()
load_dotenv()

TIMEOUT = 30


def _clean(text):
    """Strips HTML tags/entities the boards embed in titles and snippets."""
    text = re.sub(r"<[^>]+>", "", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def adzuna_configured():
    return bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY"))


def reed_configured():
    return bool(os.getenv("REED_API_KEY"))


def jooble_configured():
    return bool(os.getenv("JOOBLE_API_KEY"))


def search_adzuna(query, country="uk"):
    """Searches Adzuna for one country. Returns a list of normalized job dicts."""
    cc = config.COUNTRIES[country]
    params = {
        "app_id": os.getenv("ADZUNA_APP_ID"),
        "app_key": os.getenv("ADZUNA_APP_KEY"),
        "what": query,
        "results_per_page": config.RESULTS_PER_QUERY,
        "max_days_old": config.MAX_JOB_AGE_DAYS,
        "sort_by": "date",
    }
    if cc["location"]:  # None = search the whole country (remote included)
        params["where"] = cc["location"]
        params["distance"] = int(cc["distance_miles"] * 1.6)  # Adzuna uses km
    url = f"https://api.adzuna.com/v1/api/jobs/{cc['adzuna_code']}/search/1"
    response = httpx.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    jobs = []
    for r in response.json().get("results", []):
        company = _clean((r.get("company") or {}).get("display_name", ""))
        if not company:
            continue
        jobs.append({
            "source": "adzuna",
            "source_id": r.get("id"),
            "country": country,
            "title": _clean(r.get("title", "")),
            "company": company,
            "location": _clean((r.get("location") or {}).get("display_name", "")),
            "url": r.get("redirect_url"),
            "description": _clean(r.get("description", "")),
            "salary_min": r.get("salary_min"),
            "salary_max": r.get("salary_max"),
            "posted_date": (r.get("created") or "")[:10] or None,
        })
    return jobs


def search_reed(query):
    """Searches Reed UK. Returns a list of normalized job dicts."""
    params = {
        "keywords": query,
        "locationName": config.JOB_LOCATION,
        "distanceFromLocation": config.JOB_DISTANCE_MILES,
        "resultsToTake": config.RESULTS_PER_QUERY,
    }
    response = httpx.get(
        "https://www.reed.co.uk/api/1.0/search",
        params=params,
        auth=(os.getenv("REED_API_KEY"), ""),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    cutoff = datetime.now() - timedelta(days=config.MAX_JOB_AGE_DAYS)
    jobs = []
    for r in response.json().get("results", []):
        company = _clean(r.get("employerName", ""))
        if not company:
            continue
        posted = None
        try:
            posted_dt = datetime.strptime(r.get("date", ""), "%d/%m/%Y")
            if posted_dt < cutoff:
                continue
            posted = posted_dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            pass  # keep jobs with unparseable dates
        jobs.append({
            "source": "reed",
            "source_id": r.get("jobId"),
            "country": "uk",
            "title": _clean(r.get("jobTitle", "")),
            "company": company,
            "location": _clean(r.get("locationName", "")),
            "url": r.get("jobUrl"),
            "description": _clean(r.get("jobDescription", "")),
            "salary_min": r.get("minimumSalary"),
            "salary_max": r.get("maximumSalary"),
            "posted_date": posted,
        })
    return jobs


def search_jooble(query, country="uk"):
    """Searches Jooble (multi-country aggregator). Returns normalized job dicts."""
    cc = config.COUNTRIES[country]
    response = httpx.post(
        f"https://jooble.org/api/{os.getenv('JOOBLE_API_KEY')}",
        json={"keywords": query, "location": cc["jooble_location"], "page": 1},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    cutoff = datetime.now() - timedelta(days=config.MAX_JOB_AGE_DAYS)
    jobs = []
    for r in response.json().get("jobs", []):
        company = _clean(r.get("company", ""))
        if not company:
            continue
        posted = None
        try:
            # e.g. '2026-07-08T08:14:35.0530000' - no max-age API param,
            # so old postings are dropped here
            posted_dt = datetime.fromisoformat((r.get("updated") or "")[:19])
            if posted_dt < cutoff:
                continue
            posted = posted_dt.strftime("%Y-%m-%d")
        except ValueError:
            pass  # keep jobs with unparseable dates
        description = _clean(r.get("snippet", ""))
        salary = _clean(r.get("salary", ""))
        if salary:  # free-text ('$40 - $55 per hour'); surface it to the LLM
            description = f"{description} Salary: {salary}".strip()
        jobs.append({
            "source": "jooble",
            "source_id": r.get("id"),
            "country": country,
            "title": _clean(r.get("title", "")),
            "company": company,
            "location": _clean(r.get("location", "")),
            "url": r.get("link"),
            "description": description,
            "salary_min": None,
            "salary_max": None,
            "posted_date": posted,
        })
    return jobs


def fetch_all_jobs(queries, country="uk"):
    """Runs every query against every board configured for the country.

    Returns (jobs, sources_used). Raises RuntimeError if no board is configured.
    """
    boards = {
        "adzuna": ("Adzuna", adzuna_configured, lambda q: search_adzuna(q, country)),
        "reed": ("Reed", reed_configured, search_reed),
        "jooble": ("Jooble", jooble_configured, lambda q: search_jooble(q, country)),
    }
    sources = [
        (name, search)
        for board in config.COUNTRIES[country]["boards"]
        for name, configured, search in [boards[board]]
        if configured()
    ]
    if not sources:
        raise RuntimeError(
            "No job-board API configured. Get free keys and set them in .env:\n"
            "  Adzuna (ADZUNA_APP_ID, ADZUNA_APP_KEY): https://developer.adzuna.com\n"
            "  Reed (REED_API_KEY): https://www.reed.co.uk/developers\n"
            "  Jooble (JOOBLE_API_KEY): https://jooble.org/api/about"
        )

    all_jobs = []
    for name, search in sources:
        for query in queries:
            try:
                results = search(query)
                logger.info(f"{name}: '{query}' -> {len(results)} jobs")
                all_jobs.extend(results)
            except Exception as e:
                logger.error(f"{name} search failed for '{query}': {e}")
    return all_jobs, [name for name, _ in sources]
