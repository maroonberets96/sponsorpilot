"""Career-page discovery and scraping.

A single Playwright browser instance is shared across the whole run via the
Scraper context manager (instead of launching Chromium per page).
"""
import os
import json
import re
import urllib.request
import urllib.parse
from datetime import datetime
from playwright.sync_api import sync_playwright

import config
from llm_client import generate_content, LLMError
from logger import get_logger

logger = get_logger()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _is_aggregator(url):
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return any(domain in netloc for domain in config.AGGREGATOR_DOMAINS)


# --- Career URL cache ---

def load_career_url_cache(cache_path=config.CAREER_URL_CACHE_PATH):
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading career URLs cache: {e}")
    return {}


def save_career_url_cache(cache, cache_path=config.CAREER_URL_CACHE_PATH):
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving career URLs cache: {e}")


class Scraper:
    """Owns one browser for the whole pipeline run."""

    def __init__(self):
        self._playwright = None
        self._browser = None

    def __enter__(self):
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()
        return False

    def _new_page(self):
        return self._browser.new_page(user_agent=USER_AGENT)

    # --- Career page discovery ---

    def get_career_page_with_cache(self, company_name, cache_path=config.CAREER_URL_CACHE_PATH):
        """Retrieves career page URL using cache first, falling back to web search."""
        cache = load_career_url_cache(cache_path)
        today = datetime.now().strftime("%Y-%m-%d")

        if company_name in cache:
            entry = cache[company_name]
            status = entry.get("status")
            url = entry.get("url")
            last_checked_str = entry.get("last_checked")

            is_valid = True
            if last_checked_str:
                try:
                    last_checked = datetime.strptime(last_checked_str, "%Y-%m-%d")
                    days_elapsed = (datetime.now() - last_checked).days
                    if status in ["not_found", "failed"] and days_elapsed > config.CACHE_RETRY_DAYS:
                        is_valid = False
                except ValueError:
                    is_valid = False
            else:
                is_valid = False

            if is_valid:
                if status == "found" and url:
                    logger.info(f"Using cached career page for {company_name}: {url}")
                    return url
                if status == "not_found":
                    logger.info(f"Using cached 'not_found' for {company_name} (checked {last_checked_str}).")
                    return None

        url = self.find_career_page(company_name)

        cache[company_name] = {
            "url": url,
            "status": "found" if url else "not_found",
            "last_checked": today,
        }
        save_career_url_cache(cache, cache_path)
        return url

    def find_career_page(self, company_name):
        """DuckDuckGo first, Yahoo as fallback. Skips job boards / aggregators."""
        url = self._find_career_page_ddg(company_name)
        if not url:
            url = _find_career_page_yahoo(company_name)
        return url

    def _find_career_page_ddg(self, company_name):
        """Queries DuckDuckGo HTML search; returns the first non-aggregator result."""
        query = f"{company_name} careers"
        logger.info(f"Searching DDG for: {query}")
        page = None
        try:
            page = self._new_page()
            url_encoded_query = urllib.parse.quote_plus(query)
            page.goto(f"https://html.duckduckgo.com/html/?q={url_encoded_query}", timeout=30000)

            url_texts = page.evaluate('''() => {
                return Array.from(document.querySelectorAll('.result__url'))
                    .slice(0, 5).map(el => el.innerText.trim());
            }''')

            for url_text in url_texts or []:
                if not url_text:
                    continue
                if not url_text.startswith("http"):
                    url_text = "https://" + url_text
                if _is_aggregator(url_text):
                    logger.info(f"Skipping aggregator result: {url_text}")
                    continue
                return url_text
        except Exception as e:
            logger.error(f"DDG search failed for {company_name}: {e}")
        finally:
            if page:
                page.close()
        return None

    # --- Page scraping ---

    def scrape_page_text_and_links(self, url):
        """Renders the page and extracts visible text and links."""
        logger.info(f"Scraping URL: {url}")
        page = None
        try:
            page = self._new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            text = page.evaluate("document.body.innerText")
            links = page.evaluate('''() => {
                return Array.from(document.querySelectorAll('a')).map(a => {
                    return {text: a.innerText.trim(), href: a.href}
                }).filter(l => l.href && l.text);
            }''')
            return text, links
        except Exception as e:
            logger.error(f"Failed to scrape {url}: {e}")
            return "", []
        finally:
            if page:
                page.close()

    def scrape_career_site(self, url):
        """Scrapes the landing page, then follows up to MAX_DEEP_LINKS promising
        job-listing sub-links (careers pages often keep vacancies one level deeper)."""
        text, links = self.scrape_page_text_and_links(url)
        if not text:
            return "", []

        base_netloc = urllib.parse.urlparse(url).netloc
        seen = {url.rstrip("/")}
        candidates = []
        for link in links:
            href = link.get("href", "")
            if href.rstrip("/") in seen or not href.startswith("http"):
                continue
            # Match hints against link text + URL path only (not the domain,
            # which would false-match e.g. any link on a *job*board.com site)
            parsed = urllib.parse.urlparse(href)
            label = (link.get("text", "") + " " + parsed.path).lower()
            if _is_aggregator(href):
                continue
            # Same-site links and external ATS hosts (greenhouse, workable, ...)
            # are both fine; aggregators were already filtered out above.
            if any(hint in label for hint in config.JOB_LINK_HINTS):
                # Prefer same-site links by putting them first
                if urllib.parse.urlparse(href).netloc == base_netloc:
                    candidates.insert(0, href)
                else:
                    candidates.append(href)
                seen.add(href.rstrip("/"))

        combined_text = text
        combined_links = list(links)
        for href in candidates[:config.MAX_DEEP_LINKS]:
            logger.info(f"Following job-listing link: {href}")
            sub_text, sub_links = self.scrape_page_text_and_links(href)
            if sub_text:
                combined_text += f"\n\n--- SUB-PAGE ({href}) ---\n{sub_text}"
                combined_links.extend(sub_links)
        return combined_text, combined_links


def _find_career_page_yahoo(company_name):
    """Queries Yahoo Search via urllib; returns the first non-aggregator result."""
    query = f"{company_name} careers"
    logger.info(f"Searching Yahoo for: {query}")
    try:
        url_encoded_query = urllib.parse.quote_plus(query)
        url = f"https://search.yahoo.com/search?p={url_encoded_query}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8")

        matches = re.findall(r'href=\x22https://r\.search\.yahoo\.com/[^\x22]*RU=([^\x22\/]+)', html)
        for m in matches:
            decoded_url = urllib.parse.unquote(m)
            parsed_url = urllib.parse.urlparse(decoded_url)
            if any(d in parsed_url.netloc for d in ["yahoo.com", "yimg.com", "bing.com"]):
                continue
            if decoded_url.startswith("http") and not _is_aggregator(decoded_url):
                return decoded_url
    except Exception as e:
        logger.error(f"Yahoo search failed for {company_name}: {e}")
    return None


# --- Job matching ---

def find_matching_jobs(company_name, page_text, page_links, target_titles):
    """Asks the LLM to find matching vacancies in the scraped content.

    Returns a list of job dicts (possibly empty), or None if the LLM never
    answered - callers must NOT treat None as "no jobs found".
    """
    truncated_text = page_text[:config.PAGE_TEXT_LIMIT] if page_text else ""
    links_str = json.dumps(page_links[:config.PAGE_LINKS_LIMIT])

    prompt = f"""
    You are an AI assistant helping a candidate find jobs.
    The candidate is looking for the following roles:
    {json.dumps(target_titles)}

    We have scraped what we believe is the careers page of a company named '{company_name}'.

    PAGE TEXT:
    {truncated_text}

    PAGE LINKS:
    {links_str}

    Task: Find any job postings on this page that are a reasonable match for the candidate's target roles.

    CRITICAL FILTERING RULES:
    1. VERIFY THE PAGE: First check the page actually belongs to (or hosts jobs for) '{company_name}'. If it is clearly a different company, a news article, a directory listing, or a job board unrelated to this company, return an empty array [].
    2. EXCLUDE SENIOR ROLES: The candidate is looking for entry-level to mid-level positions. You MUST strictly ignore and exclude any jobs with "Senior", "Lead", "Director", "Head", "Principal", "VP", "Staff", or "Manager" in the title (unless it's a junior manager role).
    3. EXCLUDE DEVELOPMENT ROLES: The candidate does NOT want Software Engineering or Development roles. You MUST strictly exclude any jobs with "Software Engineer", "Backend", "Frontend", "Android", "iOS", "Machine Learning", or "Developer" in the title. The ONLY exception is Data Analytics roles.
    4. STRICT ANTI-HALLUCINATION: Do NOT invent, guess, or hallucinate jobs. The job MUST be explicitly written in the PAGE TEXT as an open vacancy. If the page is just a generic 'About Us' or 'Contact' page and contains no specific job listings, you MUST return an empty array [].
    5. Only return jobs that match the candidate's target titles AND are appropriate for an early-to-mid career professional.
    6. NO GENERIC TITLES: Never return generic titles like "No specific job title found", "General Application", or "Unknown". If there are no explicit, concrete job postings that match, you MUST return an empty array [].

    Return a JSON object containing a "jobs" array. Each job object should have:
    - "job_title": The exact title found
    - "link": The most likely URL to apply or view details (use the PAGE LINKS provided)
    - "match_reason": Why it matches the candidate's target roles.

    If no matching jobs are found, return {{"jobs": []}}.
    Output ONLY valid JSON.
    """

    try:
        result_str = generate_content(
            prompt, is_json=True,
            temperature=config.MATCH_TEMPERATURE,
            model=config.MATCH_MODEL,
        )
    except LLMError as e:
        logger.error(f"LLM unavailable while matching jobs for {company_name}: {e}")
        return None

    try:
        parsed = json.loads(result_str)
        return parsed.get("jobs", [])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.error(f"Could not parse LLM job-match response for {company_name}: {e}. Raw output: {result_str[:500]}")
        return None
