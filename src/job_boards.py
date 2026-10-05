"""Job-board API clients.

All are free:
  Adzuna:    https://developer.adzuna.com  (ADZUNA_APP_ID + ADZUNA_APP_KEY)
  Reed:      https://www.reed.co.uk/developers  (REED_API_KEY, UK only)
  Jooble:    https://jooble.org/api/about  (JOOBLE_API_KEY)
  Careerjet: https://www.careerjet.com/partners/api  (CAREERJET_API_KEY)
  JSearch:   https://www.openwebninja.com/api/jsearch  (JSEARCH_API_KEY) -
             Google for Jobs (LinkedIn, Indeed, Glassdoor...). The free tier is
             200 requests/month, so it runs a few rotating queries per run
             within a monthly budget tracked in data/api_usage.json.
  Arbeitnow: https://www.arbeitnow.co.uk/api/job-board-api  (no key, UK only)
             - a feed of the latest postings, filtered locally by title.

Each search returns normalized job dicts:
  {source, source_id, title, company, location, url, description,
   salary_min, salary_max, posted_date}
"""
import html
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from dotenv import load_dotenv

import config
from logger import get_logger

logger = get_logger()
load_dotenv()

TIMEOUT = 30
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
# Board queries are independent network calls, so they run concurrently. The
# boards are public APIs with generous limits; a dozen in flight is well within
# them and turns a ~minute of serial waiting into a few seconds.
FETCH_WORKERS = 8


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


def careerjet_configured():
    return bool(os.getenv("CAREERJET_API_KEY"))


def jsearch_configured():
    return bool(os.getenv("JSEARCH_API_KEY"))


def arbeitnow_configured():
    return True  # keyless public feed


def _too_old(posted_dt):
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.MAX_JOB_AGE_DAYS)
    return posted_dt is not None and posted_dt < cutoff


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


# --- Careerjet ---------------------------------------------------------------

CAREERJET_LOCALES = {"uk": "en_GB", "ca": "en_CA"}
_public_ip = None
_public_ip_lock = threading.Lock()


def _caller_ip():
    """Careerjet requires the searching user's IP. For a tool running on your
    own machine that is your public IP (override with CAREERJET_USER_IP)."""
    global _public_ip
    with _public_ip_lock:
        if _public_ip is None:
            _public_ip = os.getenv("CAREERJET_USER_IP", "")
            if not _public_ip:
                try:
                    _public_ip = httpx.get("https://api.ipify.org", timeout=10).text.strip()
                except Exception:
                    _public_ip = "127.0.0.1"
        return _public_ip


def search_careerjet(query, country="uk"):
    """Searches Careerjet (90-country aggregator). Returns normalized job dicts."""
    cc = config.COUNTRIES[country]
    params = {
        "locale_code": CAREERJET_LOCALES.get(country, "en_GB"),
        "keywords": query,
        "sort": "relevance",  # date-sorting pulls in loosely related roles
        "page_size": min(config.RESULTS_PER_QUERY, 100),
        "fragment_size": 500,
        "user_ip": _caller_ip(),
        "user_agent": USER_AGENT,
    }
    if cc["location"]:
        params["location"] = cc["location"]
        params["radius"] = cc["distance_miles"]
    response = httpx.get(
        "https://search.api.careerjet.net/v4/query",
        params=params, auth=(os.getenv("CAREERJET_API_KEY"), ""), timeout=TIMEOUT,
        # Careerjet rejects requests without a Referer naming the publisher site
        headers={"Referer": os.getenv(
            "CAREERJET_REFERER", "https://github.com/maroonberets96/sponsorpilot")},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("type") != "JOBS":  # e.g. an ambiguous-location answer
        return []
    jobs = []
    for r in data.get("jobs", []):
        company = _clean(r.get("company", ""))
        if not company:
            continue
        try:
            posted_dt = parsedate_to_datetime(r.get("date", ""))
        except (TypeError, ValueError):
            posted_dt = None
        if _too_old(posted_dt):
            continue
        yearly = (r.get("salary_type") or "Y") == "Y"
        jobs.append({
            "source": "careerjet",
            "source_id": r.get("url"),  # no id field; the tracking URL is unique
            "country": country,
            "title": _clean(r.get("title", "")),
            "company": company,
            "location": _clean(r.get("locations", "")),
            "url": r.get("url"),
            "description": _clean(r.get("description", "")),
            "salary_min": r.get("salary_min") if yearly else None,
            "salary_max": r.get("salary_max") if yearly else None,
            "posted_date": posted_dt.strftime("%Y-%m-%d") if posted_dt else None,
        })
    return jobs


# --- JSearch (Google for Jobs) -----------------------------------------------

JSEARCH_COUNTRIES = {"uk": "gb", "ca": "ca"}
_usage_lock = threading.Lock()


def _load_usage():
    try:
        with open(config.API_USAGE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_usage(usage):
    os.makedirs(os.path.dirname(config.API_USAGE_PATH) or ".", exist_ok=True)
    with open(config.API_USAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(usage, f, indent=2)


def jsearch_queries(queries, country):
    """Pick this run's JSearch queries: a rotating slice of the search list,
    so every query gets its turn across runs, capped by the monthly budget.
    The requests are counted (reserved) here, before they are made."""
    with _usage_lock:
        usage = _load_usage()
        month = datetime.now().strftime("%Y-%m")
        state = usage.get("jsearch", {})
        if state.get("month") != month:
            state = {"month": month, "used": 0, "offset": state.get("offset", {})}
        left = config.JSEARCH_MONTHLY_BUDGET - state["used"]
        take = max(0, min(config.JSEARCH_QUERIES_PER_RUN, left, len(queries)))
        offset = state["offset"].get(country, 0) % max(1, len(queries))
        picked = [queries[(offset + i) % len(queries)] for i in range(take)]
        state["used"] += take
        state["offset"][country] = offset + take
        usage["jsearch"] = state
        _save_usage(usage)
    if not picked:
        logger.info(f"JSearch: monthly budget of {config.JSEARCH_MONTHLY_BUDGET} "
                    f"requests used up; skipping until next month.")
    return picked


def search_jsearch(query, country="uk"):
    """Searches Google for Jobs via JSearch. Returns normalized job dicts."""
    cc = config.COUNTRIES[country]
    where = cc["location"] or cc["label"]
    response = httpx.get(
        "https://api.openwebninja.com/jsearch/search-v2",
        params={
            "query": f"{query} in {where}",
            "country": JSEARCH_COUNTRIES.get(country, "gb"),
            "date_posted": "week" if config.MAX_JOB_AGE_DAYS <= 7 else "month",
        },
        headers={"x-api-key": os.getenv("JSEARCH_API_KEY")},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json().get("data", [])
    if isinstance(data, dict):  # some versions nest the list
        data = data.get("jobs", [])
    jobs = []
    for r in data:
        company = _clean(r.get("employer_name", ""))
        if not company:
            continue
        try:
            posted_dt = datetime.fromisoformat(
                (r.get("job_posted_at_datetime_utc") or "").replace("Z", "+00:00"))
        except ValueError:
            posted_dt = None
        if _too_old(posted_dt):
            continue
        yearly = (r.get("job_salary_period") or "YEAR") == "YEAR"
        location = r.get("job_location") or ", ".join(
            x for x in (r.get("job_city"), r.get("job_state")) if x)
        jobs.append({
            "source": "jsearch",
            "source_id": r.get("job_id"),
            "country": country,
            "title": _clean(r.get("job_title", "")),
            "company": company,
            "location": _clean(location or ""),
            "url": r.get("job_apply_link"),
            "description": _clean(r.get("job_description", ""))[:6000],
            "salary_min": r.get("job_min_salary") if yearly else None,
            "salary_max": r.get("job_max_salary") if yearly else None,
            "posted_date": posted_dt.strftime("%Y-%m-%d") if posted_dt else None,
        })
    return jobs


# --- Arbeitnow (UK feed) -----------------------------------------------------

def _title_matches(title, queries):
    """True if every word of one search query appears in the title."""
    words = set(re.findall(r"[a-z0-9]+", title.lower()))
    return any(set(re.findall(r"[a-z0-9]+", q.lower())) <= words for q in queries)


def search_arbeitnow(queries):
    """Reads the latest Arbeitnow UK postings (no search parameter exists) and
    keeps those whose title matches one of the search queries."""
    jobs = []
    for page in range(1, config.ARBEITNOW_PAGES + 1):
        response = httpx.get(
            "https://www.arbeitnow.co.uk/api/job-board-api",
            params={"page": page}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        for r in payload.get("data", []):
            title = _clean(r.get("title", ""))
            company = _clean(r.get("company_name", ""))
            if not company or not _title_matches(title, queries):
                continue
            try:
                posted_dt = datetime.fromtimestamp(int(r.get("created_at")), timezone.utc)
            except (TypeError, ValueError):
                posted_dt = None
            if _too_old(posted_dt):
                continue
            jobs.append({
                "source": "arbeitnow",
                "source_id": r.get("slug"),
                "country": "uk",
                "title": title,
                "company": company,
                "location": _clean(r.get("location", "")),
                "url": r.get("url"),
                "description": _clean(r.get("description", ""))[:6000],
                "salary_min": None,
                "salary_max": None,
                "posted_date": posted_dt.strftime("%Y-%m-%d") if posted_dt else None,
            })
        if not (payload.get("links") or {}).get("next"):
            break
    return jobs


def fetch_all_jobs(queries, country="uk"):
    """Runs every query against every board configured for the country.

    Returns (jobs, sources_used). Raises RuntimeError if no board is configured.
    """
    # name, configured?, search function, query selection: None = every
    # query; a function = a subset (JSearch's budget); "feed" = a board with
    # no search parameter, called once with all queries to filter locally.
    boards = {
        "adzuna": ("Adzuna", adzuna_configured, lambda q: search_adzuna(q, country), None),
        "reed": ("Reed", reed_configured, search_reed, None),
        "jooble": ("Jooble", jooble_configured, lambda q: search_jooble(q, country), None),
        "careerjet": ("Careerjet", careerjet_configured,
                      lambda q: search_careerjet(q, country), None),
        "jsearch": ("JSearch", jsearch_configured, lambda q: search_jsearch(q, country),
                    lambda qs: jsearch_queries(qs, country)),
        "arbeitnow": ("Arbeitnow", arbeitnow_configured, search_arbeitnow, "feed"),
    }
    sources = [
        (name, search, pick)
        for board in config.COUNTRIES[country]["boards"]
        for name, configured, search, pick in [boards[board]]
        if configured()
    ]
    if not sources:
        raise RuntimeError(
            "No job-board API configured. Get free keys and set them in .env:\n"
            "  Adzuna (ADZUNA_APP_ID, ADZUNA_APP_KEY): https://developer.adzuna.com\n"
            "  Reed (REED_API_KEY): https://www.reed.co.uk/developers\n"
            "  Jooble (JOOBLE_API_KEY): https://jooble.org/api/about\n"
            "  Careerjet (CAREERJET_API_KEY): https://www.careerjet.com/partners/api\n"
            "  JSearch (JSEARCH_API_KEY): https://www.openwebninja.com/api/jsearch"
        )

    # Every (board, query) pair is an independent call, so fan them all out at
    # once and collect as they land. Order does not matter - the DB dedups.
    tasks = []
    for name, search, pick in sources:
        if pick == "feed":
            tasks.append((name, search, list(queries), "latest postings"))
        else:
            for query in (pick(queries) if pick else queries):
                tasks.append((name, search, query, query))
    all_jobs = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(search, arg): (name, label)
            for name, search, arg, label in tasks
        }
        for future in as_completed(futures):
            name, query = futures[future]
            try:
                results = future.result()
                logger.info(f"{name}: '{query}' -> {len(results)} jobs")
                all_jobs.extend(results)
            except Exception as e:
                logger.error(f"{name} search failed for '{query}': {e}")
    return all_jobs, [name for name, _, _ in sources]
