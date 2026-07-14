# Job Application Assistant

An automated, LLM-powered job-hunting pipeline. Every run pulls live vacancies
from job-board APIs, scores each one against your CV, and writes a tailored CV
and cover letter (Markdown + PDF) for the best matches — so your daily job is to
review a shortlist and hit apply, not to trawl listings.

It runs entirely on **free** API tiers and keeps all your personal data on your
own machine.

## How it works

```
Job boards (Adzuna / Reed / Jooble)
        │  live vacancies for your target roles
        ▼
[UK only] UK sponsor-licence filter        ← keep employers that can sponsor a visa
        ▼
SQLite state (data/jobs.db)                 ← dedup: nothing is ever processed twice
        ▼
Title pre-filter (free, in code)            ← drop senior / dev roles before spending LLM calls
        ▼
LLM scoring 1–10 against your CV profile    ← generate documents at score ≥ 7
        ▼
Tailored CV + cover letter (PDF)  +  hiring-contact email lookup
        ▼
data/output/<Country>/<date>/   and   data/applications.md
```

State lives in SQLite, so a vacancy seen yesterday is never re-scored or
re-generated. Each job carries a status (`new → shortlisted → generated →
applied`) across runs.

## Features

- **Two countries.** UK and Canada. Pick `uk`, `ca`, or `both` at startup (or
  with `--country`). The UK path filters employers against the official
  sponsor-licence register; the Canada path skips that (for holders of PR /
  work authorization) and searches nationwide including remote roles.
- **Multi-board.** Adzuna (UK + Canada), Reed (UK), and Jooble (Canada), each
  behind a free API key. Configurable per country.
- **LLM waterfall.** Tries NVIDIA NIM → Groq → Ollama Cloud, so a rate limit or
  outage on one provider transparently falls through to the next.
- **Tailored documents.** Each application gets a CV and cover letter rewritten
  for that specific role — emphasising your relevant experience without
  inventing anything or relabelling you as the advertised job title.
- **Hiring-contact lookup.** Best-effort: extracts a genuine contact email
  published in the posting (never guessed or constructed). Shows "not found"
  when the employer publishes none.
- **Rate-limited generation.** Caps documents per run (highest scores first);
  the rest stay shortlisted and roll over to the next run.
- **Desktop notification** when a run finishes (Windows toast).

## Requirements

- **Python 3.11+** (developed on 3.14)
- **API keys** — all have free tiers:
  | Service | Used for | Get a key |
  |---|---|---|
  | [Adzuna](https://developer.adzuna.com) | UK + Canada vacancies | free |
  | [Reed](https://www.reed.co.uk/developers) | UK vacancies | free |
  | [Jooble](https://jooble.org/api/about) | Canada vacancies | free |
  | [NVIDIA NIM](https://build.nvidia.com) | primary LLM | free tier |
  | [Groq](https://console.groq.com) | fallback LLM | free tier |
  | [Ollama Cloud](https://ollama.com) | fallback LLM (`ollama signin`) | free tier |

  At least one job board and one LLM provider are required; the rest are
  optional and improve coverage/resilience.
- **Your CV** as a `.docx` file.
- **(UK only)** The latest *Worker and Temporary Worker* sponsor register
  (`.xlsx`) from
  [gov.uk](https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers).

## Setup

```bash
# 1. Clone and enter
git clone <your-repo-url>
cd job-application-assistant

# 2. Virtual environment
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

# 3. Dependencies
pip install -r requirements.txt
playwright install chromium      # only needed for the legacy --mode scan

# 4. Configuration
copy .env.example .env           # then edit .env with your keys and filenames

# 5. Input files -> data/input/
#    - your CV .docx        (set CV_FILENAME in .env to its filename)
#    - (UK) the sponsor .xlsx (set SPONSOR_XLSX_FILENAME in .env)
```

`data/input/` and everything the tool generates is gitignored — your CV, the
database, generated documents, and your `.env` keys never leave your machine.

## Usage

```bash
python src/main.py
```

You'll be asked which country to target (`uk` / `ca` / `both`). To skip the
prompt (e.g. for a scheduled task):

```bash
python src/main.py --country ca
python src/main.py --country both
```

On Windows you can just double-click **`run_daily.bat`**.

After a run, review the shortlist in **`data/applications.md`**. When you've
applied to one, mark it so it drops off the list:

```bash
python src/main.py --mark-applied <ID>     # ID shown in applications.md
```

### Output layout

```
data/output/
├── UK/
│   └── 2026-07-14/
│       ├── report.md
│       └── <Company>_<JobTitle>/   ← CV.pdf, CV.md, CoverLetter.pdf, CoverLetter.md
└── Canada/
    └── 2026-07-14/
        └── ...
```

### Tuning what it searches for

Edit `src/config.py`:

- `SEARCH_QUERIES` — the queries sent to the job boards (this determines which
  jobs are fetched at all).
- `MANUAL_ROLES` — target roles the LLM scores against, alongside titles it
  infers from your CV.
- `MIN_MATCH_SCORE`, `MAX_DOCS_PER_RUN`, `MAX_JOB_AGE_DAYS`, `JOB_LOCATION`,
  `JOB_DISTANCE_MILES` — thresholds and search scope.

The tool caches an analysis of your CV in `data/cv_profile.json`. **If you
update your CV, delete that file** so it re-analyses on the next run.

### Modes

- **`--mode jobs`** (default) — the API-driven pipeline described above.
- **`--mode scan`** (legacy) — scrapes the careers pages of sponsor-register
  companies directly with a headless browser. Slower; requires
  `playwright install`.

## Notes & caveats

- **It visits third-party pages.** To find contact emails and (in scan mode) job
  listings, the tool fetches arbitrary job-posting and careers URLs returned by
  the boards. That's expected behaviour, but be aware it makes outbound requests
  to sites it doesn't control.
- **Contact emails are extracted, never guessed.** Only addresses literally
  published in a posting are reported. Most postings publish none.
- **LLM output should be reviewed.** Tailored CVs and cover letters are drafts —
  read them before sending. The prompts forbid inventing experience, but always
  check.
- **Not affiliated** with Adzuna, Reed, Jooble, NVIDIA, Groq, Ollama, or any
  government body. Respect each API's terms of use.

## Project layout

```
src/
├── main.py            pipeline orchestration + CLI
├── config.py          all tunables and per-country settings
├── job_boards.py      Adzuna / Reed / Jooble API clients
├── sponsor_register.py UK sponsor-licence name matching
├── db.py              SQLite state + dedup
├── matcher.py         title pre-filter + LLM scoring
├── cv_analyzer.py     extracts a target-role profile from your CV
├── generator.py       tailored CV + cover letter prompts
├── contact_finder.py  best-effort hiring-contact email extraction
├── pdf_generator.py   Markdown -> PDF
├── scraper.py         headless-browser careers scraping (legacy scan mode)
├── llm_client.py      NVIDIA -> Groq -> Ollama Cloud waterfall
└── logger.py          logging setup
```

## License

[MIT](LICENSE).
