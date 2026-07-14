"""Central configuration for the Job Application Assistant pipeline."""
import os

from dotenv import load_dotenv

# Load .env before any os.getenv below runs. config is imported first by every
# module, so loading here makes .env overrides available process-wide.
load_dotenv()

# --- Paths ---
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_DIR = os.path.join(BASE_DIR, "data")
INPUT_DIR = os.path.join(DATA_DIR, "input")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
LOG_DIR = os.path.join(DATA_DIR, "logs")

# Your base CV as a .docx in data/input/. Set CV_FILENAME in .env to your file.
CV_DOCX_PATH = os.path.join(INPUT_DIR, os.getenv("CV_FILENAME", "cv.docx"))
# UK sponsor-licence register (jobs-first UK mode). Download the latest
# "Worker and Temporary Worker" list from gov.uk, drop it in data/input/, and
# point SPONSOR_XLSX_FILENAME in .env at it.
COMPANIES_XLSX_PATH = os.path.join(
    INPUT_DIR,
    os.getenv("SPONSOR_XLSX_FILENAME", "Worker_and_Temporary_Worker.xlsx"),
)
COMPANIES_XLSX_COLUMN = "Organisation Name"

PROFILE_CACHE_PATH = os.path.join(DATA_DIR, "cv_profile.json")
CAREER_URL_CACHE_PATH = os.path.join(DATA_DIR, "career_urls_cache.json")
PROCESSED_TRACKER_PATH = os.path.join(DATA_DIR, "processed_companies.txt")

# --- Jobs-first pipeline ---
DB_PATH = os.path.join(DATA_DIR, "jobs.db")
APPLICATIONS_MD_PATH = os.path.join(DATA_DIR, "applications.md")

JOB_LOCATION = os.getenv("JOB_LOCATION", "London")
JOB_DISTANCE_MILES = int(os.getenv("JOB_DISTANCE_MILES", "20"))

# Per-country search settings. The UK needs a sponsor-licence check; Canada
# does not (candidate will hold PR). location=None searches the whole country
# (used for Canada to include remote roles nationwide).
COUNTRIES = {
    "uk": {
        "label": "UK",
        "boards": ["adzuna", "reed"],
        "adzuna_code": "gb",
        "jooble_location": JOB_LOCATION,
        "currency": "£",
        "location": JOB_LOCATION,
        "distance_miles": JOB_DISTANCE_MILES,
        "sponsor_filter": True,
    },
    "ca": {
        "label": "Canada",
        "boards": ["adzuna", "jooble"],
        "adzuna_code": "ca",
        "jooble_location": "Canada",
        "currency": "C$",
        "location": os.getenv("JOB_LOCATION_CA") or None,
        "distance_miles": JOB_DISTANCE_MILES,
        "sponsor_filter": False,
    },
}
MAX_JOB_AGE_DAYS = 14          # ignore postings older than this
RESULTS_PER_QUERY = 50         # per source, per query
MIN_MATCH_SCORE = 7            # LLM score (1-10) required to generate documents
MAX_DOCS_PER_RUN = 10          # highest-scored first; the rest stay shortlisted for next run
SCORE_BATCH_SIZE = 20          # jobs scored per LLM call

# Queries sent to the job boards (drawn from the target roles)
SEARCH_QUERIES = [
    "IT Support Analyst",
    "IT Support Officer",
    "IT Technician",
    "Desktop Support",
    "IT Operations",
    "IT Project Coordinator",
    "Data Analyst",
    "Business Analyst",
    "Business Process Analyst",
    "Digital Transformation",
    "Power Platform",
    "Facilities Officer",
]

# Title pre-filter (mirrors the LLM matching rules, applied in code first)
SENIOR_TITLE_KEYWORDS = [
    "senior", "lead ", " lead", "principal", "head of", "director",
    "vp ", "vice president", "chief", "staff engineer",
]
DEV_TITLE_KEYWORDS = [
    "software engineer", "software developer", "backend", "back end",
    "frontend", "front end", "full stack", "fullstack", "android", "ios",
    "machine learning", "devops",
]
DEV_TITLE_ALLOW = ["data", "analyst", "power platform", "automation"]

# --- Pipeline behaviour ---
BATCH_SIZE = 50                 # companies checked per run (override with --batch-size)
CACHE_RETRY_DAYS = 30           # re-check "not found" career pages after this many days
MAX_DEEP_LINKS = 2              # promising sub-links to follow from a careers landing page
PAGE_TEXT_LIMIT = 15000         # chars of page text sent to the LLM
PAGE_LINKS_LIMIT = 150          # links sent to the LLM

# --- LLM models (NVIDIA NIM API, OpenAI-compatible) ---
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Fast + accurate JSON extraction for job matching (sparse MoE, ~10B active params)
MATCH_MODEL = os.getenv("NVIDIA_MATCH_MODEL", "qwen/qwen3.5-122b-a10b")
# Qwen flagship for CV / cover letter writing (kimi-k2.6 is listed in the
# catalog but returns 404 on invocation for standard accounts)
WRITE_MODEL = os.getenv("NVIDIA_WRITE_MODEL", "qwen/qwen3.5-397b-a17b")
# Shared NVIDIA fallback if the primary model errors or is rate-limited
NVIDIA_FALLBACK_MODEL = os.getenv("NVIDIA_FALLBACK_MODEL", "deepseek-ai/deepseek-v4-flash")
# Fallback provider 2
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Fallback provider 3: Ollama Cloud (via local daemon after `ollama signin`).
# Model MUST end with "-cloud" - local models are never used.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
OLLAMA_CLOUD_MODEL = os.getenv("OLLAMA_CLOUD_MODEL", "gpt-oss:120b-cloud")

MATCH_TEMPERATURE = 0.2         # deterministic extraction
WRITE_TEMPERATURE = 0.5         # controlled creativity for documents
MAX_OUTPUT_TOKENS = 4096

# --- Job targeting ---
MANUAL_ROLES = [
    "IT & Facilities Officer",
    "IT Support Officer / IT Support Analyst",
    "Digital Transformation Officer / Coordinator",
    "Business Process Analyst",
    "Data & Systems Analyst",
    "IT Operations Officer",
    "Power Platform Developer / Automation Analyst",
    "IT Project Coordinator",
    "Junior Data Analyst",
]

# Inferred titles containing these words are dropped unless they also contain "Data"
EXCLUDED_TITLE_KEYWORDS = ["Developer", "Software", "Engineer"]

# Job titles that exactly equal one of these (case-insensitive) are treated as
# hallucinated placeholders, not real vacancies
INVALID_TITLE_EXACT = {"any", "none", "unknown", "n/a"}
# Job titles containing one of these phrases are treated as invalid
INVALID_TITLE_PHRASES = [
    "no specific", "not found", "not specified",
    "general application", "open application", "no job",
]

# Postings where the "employer" is actually a job board reposting an
# anonymous company's ad (normalized names; see sponsor_register.normalize)
EXCLUDED_EMPLOYER_NAMES = {
    "efinancialcareers", "e financialcareers", "cv library", "totaljobs",
    "jobsite", "jobserve", "adzuna", "reed", "indeed", "linkedin",
    "jobleads", "jobg8", "appcast",
}

# Search results on these domains are never a company's own careers page
AGGREGATOR_DOMAINS = [
    "indeed.", "linkedin.", "glassdoor.", "reed.co", "totaljobs.",
    "cv-library.", "monster.", "adzuna.", "jobsite.", "ziprecruiter.",
    "simplyhired.", "wikipedia.", "companieshouse.", "find-and-update.company-information",
    "facebook.", "instagram.", "yell.com", "trustpilot.",
    "visajob.", "visapath.", "ukhired.", "jobsora.", "uktiersponsors.", "workpermit.",
]

# Link text hinting that a sub-page holds the actual job listings
JOB_LINK_HINTS = [
    "vacanc", "job", "career", "position", "opening", "opportunit",
    "join us", "join our", "work with us", "work for us", "we're hiring", "apply",
]
