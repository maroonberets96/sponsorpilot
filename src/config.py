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
        "split_by_email": True,
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
        # Sort each day's application folders into With_Email / No_Email
        "split_by_email": True,
        # Applying from abroad: write your phone in international format
        # (adds PHONE_COUNTRY_CODE from .env, e.g. 07700... -> +44 7700...)
        "intl_phone": True,
    },
}
# Your phone's country code (e.g. +44), used for countries with intl_phone
PHONE_COUNTRY_CODE = os.getenv("PHONE_COUNTRY_CODE", "")
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
    "Microsoft 365 Administrator",
    "Service Desk Analyst",
    "Help Desk Analyst",
    "Application Support Analyst",
    "Junior Systems Administrator",
    "Business Systems Analyst",
    "Operations Analyst",
    "Process Improvement Analyst",
    "Systems Officer",
    "IT Coordinator",
    "Implementation Specialist",
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

# --- LLM providers (OpenAI-compatible waterfall) ---
# Every provider below speaks the OpenAI chat-completions protocol, so one
# client class (openai.OpenAI) drives all of them - only the base URL, key and
# model name change. llm_client tries them in LLM_ORDER and fails over on rate
# limits, dead keys and errors (free tiers produce these constantly, so one
# provider alone stalls a run). Set as many keys as you have in .env; spare
# keys for the same provider go in NVIDIA_API_KEY_2, GROQ_API_KEY_2, etc. and
# are tried as extra fallbacks.
#
# gpt-oss-120b is the shared default model: it is the strongest model that
# Groq, NVIDIA, Cerebras, Hugging Face and Ollama Cloud all serve, and it
# handles both JSON extraction (matching) and document writing well. Each
# provider spells it slightly differently, hence a model per provider.

# Base URLs (OpenAI-compatible endpoints)
NVIDIA_BASE_URL      = "https://integrate.api.nvidia.com/v1"
GROQ_BASE_URL        = "https://api.groq.com/openai/v1"
CEREBRAS_BASE_URL    = "https://api.cerebras.ai/v1"
GEMINI_BASE_URL      = "https://generativelanguage.googleapis.com/v1beta/openai"
HUGGINGFACE_BASE_URL = "https://router.huggingface.co/v1"
OPENROUTER_BASE_URL  = "https://openrouter.ai/api/v1"
MISTRAL_BASE_URL     = "https://api.mistral.ai/v1"
REQUESTY_BASE_URL    = "https://router.requesty.ai/v1"
# Ollama in either flavour: cloud (ollama.com/v1 + OLLAMA_API_KEY, no daemon)
# when a key is set, otherwise the local daemon after `ollama signin`.
OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "https://ollama.com/v1" if os.getenv("OLLAMA_API_KEY") else "http://localhost:11434/v1",
)

# Model per provider (gpt-oss-120b where served; others run the nearest model
# that provider actually offers on its free tier - verified live 2026-10-04).
GROQ_MODEL        = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
CEREBRAS_MODEL    = os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")
HUGGINGFACE_MODEL = os.getenv("HUGGINGFACE_MODEL", "openai/gpt-oss-120b")
# NVIDIA retired gpt-oss-120b (410 Gone); it still serves the 20b.
NVIDIA_MODEL      = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-20b")
# Gemini cannot serve gpt-oss; it runs its own model. "-latest" on purpose:
# Google silently zeroes the free quota on pinned older models. flash-latest
# (not flash-LITE) is a clear quality step up with still-generous free limits;
# set GEMINI_MODEL=gemini-pro-latest for max quality (tighter free quota).
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
# OpenRouter rotates which models are free; gpt-oss-20b:free is gone (402).
OPENROUTER_MODEL  = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.8-27b:free")
# Mistral: best writer available on the free key (Large is not on the free tier;
# mistral-medium-latest is the flagship that is). Needs the free "Experiment"
# tier activated in the Mistral console or every call 429s.
MISTRAL_MODEL     = os.getenv("MISTRAL_MODEL", "mistral-medium-latest")
# Requesty router: default to the fast-ish Nemotron for general fallback; the
# 550B flagship is wired into WRITE_PREFERENCES below as a second route.
REQUESTY_MODEL    = os.getenv("REQUESTY_MODEL", "nvidia/nemotron-3-super-120b-a12b")
# Ollama Cloud model. Via the local daemon it must end with "-cloud"; via the
# hosted endpoint (OLLAMA_API_KEY set) the plain "gpt-oss:120b" is used.
OLLAMA_CLOUD_MODEL = os.getenv(
    "OLLAMA_CLOUD_MODEL",
    "gpt-oss:120b" if os.getenv("OLLAMA_API_KEY") else "gpt-oss:120b-cloud",
)

# Matching (job scoring, JSON extraction) is fast-first: the default waterfall
# is fine, so the match model is just the NVIDIA default.
MATCH_MODEL = os.getenv("NVIDIA_MATCH_MODEL", NVIDIA_MODEL)
# Shared NVIDIA fallback if the primary NVIDIA model errors or is rate-limited.
NVIDIA_FALLBACK_MODEL = os.getenv("NVIDIA_FALLBACK_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")

# --- Writing (CV / cover letters): quality-first, best model preferred -------
# These are long-form documents, so unlike matching the writing path leads with
# the STRONGEST model available and only falls back for resilience. Each entry
# is (provider, model); tried in order, then the general waterfall as a last
# resort. Benchmarked live 2026-10-04 on a real cover-letter prompt: the 550B
# flagship produced the most polished, placeholder-free output. NVIDIA Nemotron
# models run with chain-of-thought disabled (handled in llm_client) so the full
# token budget goes to the document, not to hidden reasoning.
WRITE_PREFERENCES = [
    ("nvidia",   "nvidia/nemotron-3-ultra-550b-a55b"),   # 550B flagship - best prose
    ("requesty", "nvidia/nemotron-3-ultra-550b-a55b"),   # 2nd free route to the SAME 550B
    ("nvidia",   "nvidia/nemotron-3-super-120b-a12b"),   # 120B, ~2x faster, still strong
    ("groq",     "openai/gpt-oss-120b"),                 # fast, strong, very reliable
]
# Force a single write model with NVIDIA_WRITE_MODEL (kept for back-compat); it
# becomes the top preference when set.
WRITE_MODEL = os.getenv("NVIDIA_WRITE_MODEL", WRITE_PREFERENCES[0][1])
if os.getenv("NVIDIA_WRITE_MODEL"):
    WRITE_PREFERENCES = [("nvidia", WRITE_MODEL)] + WRITE_PREFERENCES

# --- Scoring (job matching): ONE model, several hosts ------------------------
# Scores feed a hard cutoff (MIN_MATCH_SCORE) and a ranking, so every job in a
# run must be judged by the SAME model or a "7" from one model competes with a
# "6" from another. So scoring pins a single model - gpt-oss-120b - and only
# varies the HOST serving it (Groq -> Hugging Face -> Ollama Cloud), giving one
# consistent judge plus three independent quotas for resilience. The general
# waterfall remains as a last resort if all three hosts are down (logged, rare).
# All three names below are the identical gpt-oss-120b, just as each host spells
# it. (NVIDIA/Cerebras are intentionally absent: NVIDIA retired the 120b and
# Cerebras needs billing - both verified live 2026-10-04.)
SCORE_PREFERENCES = [
    ("groq", "openai/gpt-oss-120b"),
    ("huggingface", "openai/gpt-oss-120b"),
    ("ollama", OLLAMA_CLOUD_MODEL),  # "gpt-oss:120b"
]

# Order the providers are tried in, fastest + most generous free tier first
# (verified live 2026-10-04). Cerebras is omitted from the default because its
# key currently returns 402 (needs billing); add it back once funded, e.g.
# LLM_ORDER=groq,cerebras,gemini,huggingface,nvidia,openrouter,ollama
LLM_ORDER = [
    p.strip().lower()
    for p in os.getenv("LLM_ORDER", "").split(",") if p.strip()
] or ["groq", "gemini", "huggingface", "nvidia", "requesty", "openrouter", "ollama", "mistral"]

# --- Work authorization (woven into application emails, per country) ---------
# A short, honest eligibility statement per country, inserted into the
# application email so each market gets accurate messaging. Personal, so it
# lives in .env (WORK_ELIGIBILITY_UK / WORK_ELIGIBILITY_CA), not in code.
# Blank = do not mention work authorization for that country.
WORK_ELIGIBILITY = {
    "uk": os.getenv("WORK_ELIGIBILITY_UK", ""),
    "ca": os.getenv("WORK_ELIGIBILITY_CA", ""),
}

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
    "Microsoft 365 Administrator",
    "Service Desk / Help Desk Analyst",
    "Application Support Analyst",
    "Junior Systems Administrator",
    "Business Systems Analyst",
    "Operations Analyst",
    "Process Improvement Analyst",
    "Systems Officer",
    "IT Coordinator",
    "Implementation Specialist",
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
