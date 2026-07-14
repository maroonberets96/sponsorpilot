"""Daily job application pipeline.

Default mode (jobs-first): query job-board APIs for live vacancies matching
the target roles, keep employers on the UK sponsor-licence register, score
each new job with the LLM, and generate tailored CV + cover letter PDFs for
the shortlist. State lives in SQLite so nothing is processed twice.

Legacy mode (--mode scan): scrape sponsor-register companies' careers pages
directly, one batch per run.
"""
import os
import re
import json
import argparse
import subprocess
import urllib.request
from datetime import datetime

import pandas as pd

import config
import db
from scraper import Scraper, find_matching_jobs
from cv_analyzer import extract_text_from_docx, infer_job_titles_and_skills
from generator import generate_tailored_cv, generate_cover_letter
from pdf_generator import convert_markdown_to_pdf
from job_boards import fetch_all_jobs
from contact_finder import find_contact_email
from sponsor_register import SponsorRegister, normalize
from matcher import title_prefilter, score_jobs
from llm_client import LLMError
from logger import get_logger

logger = get_logger()


# --- Shared helpers ---

def load_profile(cv_text):
    """Loads the cached CV profile, or infers and caches one."""
    if os.path.exists(config.PROFILE_CACHE_PATH):
        logger.info("Loading cached CV analysis profile...")
        with open(config.PROFILE_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    logger.info("Analyzing CV to get target roles...")
    profile_json = infer_job_titles_and_skills(cv_text)
    profile = json.loads(profile_json)
    with open(config.PROFILE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2, ensure_ascii=False)
    return profile


def build_target_titles(profile):
    """Merges inferred titles (minus excluded categories) with the manual role list."""
    target_titles = [
        t for t in profile.get("inferred_titles", [])
        if ("Data" in t) or not any(kw in t for kw in config.EXCLUDED_TITLE_KEYWORDS)
    ]
    target_titles.extend(config.MANUAL_ROLES)
    return sorted(set(target_titles))


def safe_dir_name(company, job_title):
    safe_company = re.sub(r"[^a-zA-Z0-9]", "_", company)[:50]
    safe_title = re.sub(r"[^a-zA-Z0-9]", "_", job_title)[:50]
    return f"{safe_company}_{safe_title}"


def append_report(report_path, text):
    with open(report_path, "a", encoding="utf-8") as f:
        f.write(text)


def make_output_dir(country=None):
    """Creates data/output/<Country>/<date>/ (or the flat <date>/ layout for
    the legacy scan mode) and seeds its report.md."""
    today = datetime.now().strftime("%Y-%m-%d")
    title = f"Daily Job Report - {today}"
    parts = [config.OUTPUT_DIR]
    if country:
        parts.append(config.COUNTRIES[country]["label"])
        title += f" ({config.COUNTRIES[country]['label']})"
    parts.append(today)
    output_dir = os.path.join(*parts)
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "report.md")
    if not os.path.exists(report_path):
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n")
    return output_dir, report_path


def generate_documents(cv_text, company, job_title, job_link, output_dir, job_description=None):
    """Generates CV + cover letter (Markdown and PDF) for one job.
    Returns the job directory name, or None on failure."""
    try:
        logger.info(f"   Generating tailored CV for {job_title}...")
        tailored_cv = generate_tailored_cv(cv_text, job_title, job_link, job_description)

        job_dir_name = safe_dir_name(company, job_title)
        job_dir_path = os.path.join(output_dir, job_dir_name)
        os.makedirs(job_dir_path, exist_ok=True)

        with open(os.path.join(job_dir_path, "CV.md"), "w", encoding="utf-8") as f:
            f.write(tailored_cv)
        convert_markdown_to_pdf(tailored_cv, os.path.join(job_dir_path, "CV.pdf"))

        logger.info(f"   Generating Cover Letter for {job_title}...")
        cover_letter = generate_cover_letter(cv_text, job_title, job_link, job_description)

        with open(os.path.join(job_dir_path, "CoverLetter.md"), "w", encoding="utf-8") as f:
            f.write(cover_letter)
        convert_markdown_to_pdf(cover_letter, os.path.join(job_dir_path, "CoverLetter.pdf"))
        return job_dir_name
    except Exception as ex:
        logger.error(f"   Error generating materials for {job_title}: {ex}")
        return None


def send_toast(message, output_dir):
    """Fires a native Windows toast notification (best-effort).

    The message and folder URI are passed via environment variables and read
    with $env: inside PowerShell, so dynamic text is never parsed as code
    (no command injection even if a scraped job title reaches this).
    """
    try:
        output_uri = "file:///" + urllib.request.pathname2url(os.path.abspath(output_dir))
        ps_script = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$xml.DocumentElement.SetAttribute("activationType", "protocol")
$xml.DocumentElement.SetAttribute("launch", $env:JAA_TOAST_URI)
$nodes = @($xml.GetElementsByTagName("text"))
$nodes[0].AppendChild($xml.CreateTextNode("Job Application Assistant")) | Out-Null
$nodes[1].AppendChild($xml.CreateTextNode($env:JAA_TOAST_MSG)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Job Application Assistant").Show($toast)
"""
        toast_env = {**os.environ, "JAA_TOAST_MSG": message, "JAA_TOAST_URI": output_uri}
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=toast_env,
        )
    except Exception as e:
        logger.error(f"Notification failed: {e}")


def write_applications_md(conn):
    """Regenerates the rolling pending-applications list from the DB."""
    rows = db.pending_applications(conn)
    lines = [
        "# Pending Applications\n",
        f"_Regenerated {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
        f"Mark one as applied with:_ `python src\\main.py --mark-applied <ID>`\n",
    ]
    if not rows:
        lines.append("\nNothing pending.\n")
    for r in rows:
        cc = config.COUNTRIES.get(r["country"], config.COUNTRIES["uk"])
        salary = ""
        if r["salary_min"] or r["salary_max"]:
            salary = (f" | {cc['currency']}{int(r['salary_min'] or 0):,}-"
                      f"{cc['currency']}{int(r['salary_max'] or 0):,}")
        if cc["sponsor_filter"]:
            sponsor = f"**Sponsor:** {r['sponsor_match']} ({r['sponsor_name']})"
        else:
            sponsor = "**Sponsor:** not needed (PR)"
        lines.append(
            f"\n## [{r['id']}] {r['title']} - {r['company']} ({cc['label']}, score {r['match_score']}/10)\n"
            f"- **Posted:** {r['posted_date'] or '?'} | **Found:** {r['found_date']} | "
            f"{sponsor}{salary}\n"
            f"- **Link:** {r['url']}\n"
            f"- **Contact:** {r['contact_email'] or 'not found'}\n"
            f"- **Why:** {r['match_reason']}\n"
            f"- **Documents:** `{r['docs_dir']}`\n"
        )
    with open(config.APPLICATIONS_MD_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)
    logger.info(f"Pending applications list updated: {config.APPLICATIONS_MD_PATH} ({len(rows)} pending)")


# --- Jobs-first mode ---

def run_jobs_mode(cv_text, profile, target_titles, country="uk"):
    cc = config.COUNTRIES[country]
    conn = db.get_conn()
    run_id = db.start_run(conn, f"jobs-{country}")
    output_dir, report_path = make_output_dir(country)
    logger.info(f"\n=== {cc['label']} job search ===")

    # 1. Fetch live vacancies from the boards
    try:
        fetched, sources = fetch_all_jobs(config.SEARCH_QUERIES, country)
    except RuntimeError as e:
        logger.error(str(e))
        db.finish_run(conn, run_id, 0, 0, 0)
        return
    logger.info(f"Fetched {len(fetched)} postings from {', '.join(sources)}.")

    # 2. Sponsor-register filter (UK only) + store new jobs
    register = SponsorRegister() if cc["sponsor_filter"] else None
    new_ids = []
    for job in fetched:
        if normalize(job["company"]) in config.EXCLUDED_EMPLOYER_NAMES:
            # A job board reposting an anonymous employer's ad - its own
            # sponsor licence says nothing about the real employer
            job["sponsor_match"] = None
            job["sponsor_name"] = None
            job["status"] = "board_posting"
            db.insert_job(conn, job)
            continue
        if register:
            tier, register_name = register.check(job["company"])
            job["sponsor_match"] = tier
            job["sponsor_name"] = register_name
            job["status"] = "new" if tier else "no_sponsor"
        else:
            # No visa needed in this country - every employer qualifies
            job["sponsor_match"] = None
            job["sponsor_name"] = None
            job["status"] = "new"
        row_id = db.insert_job(conn, job)
        if row_id and job["status"] == "new":
            new_ids.append(row_id)
    if register:
        logger.info(f"{len(new_ids)} new sponsor-licensed vacancies (rest: duplicates or non-sponsors).")
    else:
        logger.info(f"{len(new_ids)} new vacancies (rest: duplicates).")

    # 3. Title pre-filter (code, no LLM cost)
    to_score = []
    for row in db.jobs_with_status(conn, "new", country):
        reason = title_prefilter(row["title"])
        if reason:
            db.set_status(conn, row["id"], "excluded_title", match_reason=f"Excluded: {reason}")
        else:
            to_score.append(row)

    # 4. LLM scoring
    docs_generated = 0
    if to_score:
        logger.info(f"Scoring {len(to_score)} jobs with the LLM...")
        scores = score_jobs(to_score, profile, target_titles)
        if scores is None:
            logger.warning("LLM unavailable; jobs stay 'new' and will be scored next run.")
        else:
            for row in to_score:
                score, reason = scores.get(row["id"], (None, None))
                if score is None:
                    continue  # missing from response; stays 'new' for next run
                if score >= config.MIN_MATCH_SCORE:
                    db.set_status(conn, row["id"], "shortlisted",
                                  match_score=score, match_reason=reason)
                else:
                    db.set_status(conn, row["id"], "low_score",
                                  match_score=score, match_reason=reason)

    # 5. Generate documents for the shortlist (highest scores first, capped per run)
    shortlisted = sorted(
        db.jobs_with_status(conn, "shortlisted", country),
        key=lambda r: r["match_score"] or 0, reverse=True,
    )
    if len(shortlisted) > config.MAX_DOCS_PER_RUN:
        logger.info(
            f"{len(shortlisted)} shortlisted; generating top {config.MAX_DOCS_PER_RUN} this run, "
            f"the rest stay shortlisted for the next run."
        )
        shortlisted = shortlisted[:config.MAX_DOCS_PER_RUN]
    if shortlisted:
        append_report(
            report_path,
            f"## Shortlisted vacancies - {cc['label']} ({datetime.now().strftime('%H:%M')})\n\n",
        )
    for row in shortlisted:
        logger.info(f" - {row['title']} at {row['company']} (score {row['match_score']}/10)")
        job_dir = generate_documents(
            cv_text, row["company"], row["title"], row["url"],
            output_dir, job_description=row["description"],
        )
        if job_dir:
            contact = find_contact_email(row["url"], row["description"])
            db.set_status(conn, row["id"], "generated",
                          docs_dir=os.path.join(output_dir, job_dir),
                          contact_email=contact)
            docs_generated += 1
            sponsor_line = (
                f"- **Sponsor:** {row['sponsor_match']} ({row['sponsor_name']})\n"
                if cc["sponsor_filter"] else ""
            )
            append_report(
                report_path,
                f"### {row['title']} - {row['company']} (score {row['match_score']}/10)\n"
                f"- **Link:** {row['url']}\n"
                f"{sponsor_line}"
                f"- **Contact:** {contact or 'not found'}\n"
                f"- **Why:** {row['match_reason']}\n"
                f"- **CV:** [CV.pdf](./{job_dir}/CV.pdf) | **Cover letter:** [CoverLetter.pdf](./{job_dir}/CoverLetter.pdf)\n\n",
            )
        # on failure the job stays 'shortlisted' and is retried next run

    write_applications_md(conn)
    db.finish_run(conn, run_id, len(fetched), len(new_ids), docs_generated)
    logger.info(f"\n{cc['label']} pipeline complete! {docs_generated} application(s) generated. Report: {report_path}")
    send_toast(
        f"{cc['label']}: {len(new_ids)} new jobs, {docs_generated} applications generated.",
        output_dir,
    )


# --- Legacy scan mode ---

def get_companies_batch(batch_size):
    df = pd.read_excel(config.COMPANIES_XLSX_PATH)
    all_companies = df[config.COMPANIES_XLSX_COLUMN].dropna().unique().tolist()

    processed = set()
    if os.path.exists(config.PROCESSED_TRACKER_PATH):
        with open(config.PROCESSED_TRACKER_PATH, "r", encoding="utf-8") as f:
            processed = set(f.read().splitlines())

    remaining = [c for c in all_companies if c not in processed]
    return remaining[:batch_size], len(all_companies), len(remaining)


def mark_processed(company):
    with open(config.PROCESSED_TRACKER_PATH, "a", encoding="utf-8") as f:
        f.write(f"{company}\n")


def is_invalid_job_title(job_title):
    title = job_title.lower().strip()
    if not title or title in config.INVALID_TITLE_EXACT:
        return True
    return any(phrase in title for phrase in config.INVALID_TITLE_PHRASES)


def process_company(scraper, company, cv_text, target_titles, output_dir, report_path):
    """Scrapes one company end to end. Returns the number of jobs generated."""
    logger.info(f"\nScanning {company}...")

    url = scraper.get_career_page_with_cache(company)
    if not url:
        logger.info(f"Could not find career page for {company}.")
        append_report(report_path, f"## {company}\n- Could not find career page.\n\n")
        mark_processed(company)
        return 0

    text, links = scraper.scrape_career_site(url)
    if not text:
        append_report(report_path, f"## {company}\n- Could not scrape career page.\n\n")
        mark_processed(company)
        return 0

    matches = find_matching_jobs(company, text, links, target_titles)
    if matches is None:
        logger.warning(f"LLM unavailable for {company}; will retry on the next run.")
        append_report(report_path, f"## {company}\n- Skipped (LLM unavailable), will retry.\n\n")
        return 0

    unique_matches, seen = [], set()
    for match in matches:
        key = match.get("job_title", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_matches.append(match)

    jobs_generated = 0
    if unique_matches:
        logger.info(f"Found {len(unique_matches)} suitable jobs at {company}!")
        append_report(report_path, f"## {company}\n")
        for match in unique_matches:
            job_title = match.get("job_title", "").strip()
            if is_invalid_job_title(job_title):
                logger.warning(f" - Skipped invalid title: '{job_title}'")
                continue
            logger.info(f" - {job_title}")
            job_link = match.get("link") or url
            job_dir = generate_documents(cv_text, company, job_title, job_link, output_dir)
            if job_dir:
                jobs_generated += 1
                append_report(
                    report_path,
                    f"### {job_title}\n"
                    f"- **Link:** {job_link}\n"
                    f"- **Why it matches:** {match.get('match_reason')}\n"
                    f"- **CV:** [CV.pdf](./{job_dir}/CV.pdf) | **Cover letter:** [CoverLetter.pdf](./{job_dir}/CoverLetter.pdf)\n\n",
                )
    else:
        logger.info(f"No suitable jobs found at {company}.")
        append_report(report_path, f"## {company}\n- No suitable jobs found.\n\n")

    mark_processed(company)
    return jobs_generated


def run_scan_mode(cv_text, target_titles, batch_size):
    companies_to_check, total, remaining = get_companies_batch(batch_size)
    logger.info(f"Total companies in Excel: {total}")
    logger.info(f"Remaining unchecked: {remaining}")
    logger.info(f"Checking the next {len(companies_to_check)} companies today.")

    output_dir, report_path = make_output_dir()
    total_jobs_found = 0
    with Scraper() as scraper:
        for company in companies_to_check:
            try:
                total_jobs_found += process_company(
                    scraper, company, cv_text, target_titles, output_dir, report_path
                )
            except Exception as e:
                logger.error(f"Unexpected error processing {company}: {e}")

    logger.info(f"\nPipeline complete! Report saved to: {report_path}")
    send_toast(
        f"Scanned {len(companies_to_check)} companies. Found {total_jobs_found} jobs.",
        output_dir,
    )


# --- Entry point ---

def choose_countries(arg):
    """Resolves the target countries from --country, or asks interactively."""
    if not arg:
        try:
            arg = input("Which country are you targeting today? [uk/ca/both]: ").strip().lower()
        except EOFError:
            arg = "both"  # non-interactive run (e.g. scheduled task)
            logger.info("No console input available; searching both countries.")
        while arg not in ("uk", "ca", "both"):
            arg = input("Please enter uk, ca or both: ").strip().lower()
    return ["uk", "ca"] if arg == "both" else [arg]


def main():
    parser = argparse.ArgumentParser(description="Daily job application pipeline")
    parser.add_argument("--mode", choices=["jobs", "scan"], default="jobs",
                        help="jobs: query job boards, filter by sponsor register (default). "
                             "scan: scrape sponsor companies' careers pages (legacy)")
    parser.add_argument("--country", choices=["uk", "ca", "both"],
                        help="Country to search in jobs mode. Omit to be asked at startup.")
    parser.add_argument("--batch-size", type=int, default=config.BATCH_SIZE,
                        help=f"Companies per run in scan mode (default {config.BATCH_SIZE})")
    parser.add_argument("--mark-applied", type=int, metavar="JOB_ID",
                        help="Mark a job (by ID from applications.md) as applied, then exit")
    args = parser.parse_args()

    if args.mark_applied:
        conn = db.get_conn()
        row = db.mark_applied(conn, args.mark_applied)
        if row:
            logger.info(f"Marked as applied: [{row['id']}] {row['title']} - {row['company']}")
            write_applications_md(conn)
        else:
            logger.error(f"No job with ID {args.mark_applied}.")
        return

    countries = choose_countries(args.country) if args.mode == "jobs" else []

    logger.info(f"Starting Job Application Assistant Pipeline ({args.mode} mode)...")

    cv_text = extract_text_from_docx(config.CV_DOCX_PATH)
    if not cv_text:
        logger.error(f"Could not read base CV at {config.CV_DOCX_PATH}. Aborting.")
        return

    try:
        profile = load_profile(cv_text)
    except LLMError as e:
        logger.error(f"Could not analyze CV (no LLM available): {e}. Aborting.")
        return

    target_titles = build_target_titles(profile)
    logger.info(f"Target Roles Identified: {target_titles}")

    if args.mode == "jobs":
        for country in countries:
            run_jobs_mode(cv_text, profile, target_titles, country)
    else:
        run_scan_mode(cv_text, target_titles, args.batch_size)


if __name__ == "__main__":
    main()
