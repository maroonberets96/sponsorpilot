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
import shutil
import argparse
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import config
import db
import first_run
import ats
import job_folder
from validation import international_phone
from scraper import Scraper, find_matching_jobs
from cv_analyzer import extract_text_from_docx, infer_job_titles_and_skills
from generator import generate_tailored_cv, generate_cover_letter, generate_application_email
from pdf_generator import convert_markdown_to_pdf
from job_boards import fetch_all_jobs
from contact_finder import find_contact
from sponsor_register import SponsorRegister, normalize, load_register_df
from matcher import title_prefilter, score_jobs
from llm_client import LLMError
from logger import get_logger

logger = get_logger()

# Each shortlisted job runs a few network-bound LLM calls plus a contact-page
# fetch, all independent between jobs - so they build concurrently. Kept modest
# so the free-tier providers are not all hit at once (the waterfall fails over
# if one does rate-limit).
GENERATE_WORKERS = 4


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


def _write_apply_txt(path, job, contact_email, subject, body, covered, missing,
                     contact_source=None):
    """Write the per-job APPLY.txt: everything needed to apply, in one place, so
    the report never has to be opened. `job` is a dict of posting fields."""
    cc = config.COUNTRIES.get(job.get("country", "uk"), config.COUNTRIES["uk"])
    lines = [
        f"{job['title']} - {job['company']}",
        "=" * 60,
        "",
    ]
    if job.get("score") is not None:
        lines.append(f"Score:      {job['score']}/10 ({cc['label']})")
    if job.get("posted_date") or job.get("found_date"):
        lines.append(f"Posted:     {job.get('posted_date') or '?'}    Found: {job.get('found_date') or '?'}")
    if cc.get("sponsor_filter") and job.get("sponsor_match"):
        lines.append(f"Sponsor:    {job['sponsor_match']} ({job.get('sponsor_name') or '?'})")
    if job.get("salary_min") or job.get("salary_max"):
        lines.append(f"Salary:     {cc['currency']}{int(job.get('salary_min') or 0):,} - "
                     f"{cc['currency']}{int(job.get('salary_max') or 0):,}")
    if job.get("location"):
        lines.append(f"Location:   {job['location']}")
    lines += [
        "",
        f"Job link:   {job.get('url') or '?'}",
        f"Contact:    {contact_email or 'not found'}",
    ]
    site_notes = {
        "company website": "found on the company website, not the posting -"
                           " may be a general inbox, worth a quick check",
        "company website - hiring person": "a named person listed near hiring/HR info on the"
                                           " company website - check their role before sending",
        "company website - person": "a named person on the company website, NOT clearly in"
                                    " hiring - look them up first, may be the wrong contact",
    }
    if contact_email and contact_source in site_notes:
        lines.append(f"            ({site_notes[contact_source]})")
    lines += [
        "",
    ]
    if job.get("match_reason"):
        lines += ["Why it matches:", f"  {job['match_reason']}", ""]
    if covered or missing:
        total = len(covered) + len(missing)
        lines.append(f"ATS keywords covered ({len(covered)}/{total}): {', '.join(covered) or '-'}")
        if missing:
            lines.append(f"ATS keywords MISSING ({len(missing)}):  {', '.join(missing)}")
            lines.append("  -> add these to your CV if you genuinely have them.")
        lines.append("")
    lines += [
        "Files in this folder:  CV.pdf, CoverLetter.pdf",
        "Applied? Double-click 'Mark as applied.bat' in this folder.",
        "",
        "-" * 60,
        "APPLICATION EMAIL (ready to send)",
        f"To:       {contact_email or '(no contact found - find one on the posting)'}",
        f"Subject:  {subject}",
        "",
        body,
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def build_application(cv_text, job, output_dir):
    """Build one application end to end (no DB writes - safe to run in a thread).

    `job` is a dict with at least title, company, url; optionally description,
    score, match_reason, sponsor_*, salary_*, location, posted_date, country.
    Produces CV + cover letter (md + pdf), an application email, and APPLY.txt.
    Returns a result dict, or None on failure.
    """
    title, company = job["title"], job["company"]
    link = job.get("url") or ""
    description = job.get("description")
    try:
        keywords = ats.extract_keywords(description, title)

        logger.info(f"   Writing CV for {title} at {company}...")
        tailored_cv = generate_tailored_cv(cv_text, title, link, description, keywords=keywords)
        covered, missing = ats.coverage(tailored_cv, keywords)

        logger.info(f"   Writing cover letter for {title}...")
        cover_letter = generate_cover_letter(cv_text, title, link, description)

        contact_email, contact_source = find_contact(link, description, company)
        work_eligibility = config.WORK_ELIGIBILITY.get(job.get("country", "uk"), "")
        # Name for the email sign-off, from the tailored CV's '# Name' header
        candidate_name = next((l[2:].strip() for l in tailored_cv.splitlines()
                               if l.startswith("# ")), "")
        subject, body = generate_application_email(
            cv_text, title, company, link, description, contact_email,
            work_eligibility=work_eligibility, candidate_name=candidate_name,
        )

        cc = config.COUNTRIES.get(job.get("country", "uk"), config.COUNTRIES["uk"])
        if cc.get("intl_phone"):
            # Applying from abroad: phone in international format (+44 ...)
            tailored_cv, cover_letter, body = (
                international_phone(t, config.PHONE_COUNTRY_CODE)
                for t in (tailored_cv, cover_letter, body)
            )

        job_dir_name = safe_dir_name(company, title)
        if cc.get("split_by_email"):
            # Forward slash so the report's markdown links still resolve
            job_dir_name = f"{'With_Email' if contact_email else 'No_Email'}/{job_dir_name}"
        job_dir_path = os.path.normpath(os.path.join(output_dir, job_dir_name))
        os.makedirs(job_dir_path, exist_ok=True)

        job_folder.write_text(os.path.join(job_dir_path, "CV.md"), tailored_cv)
        convert_markdown_to_pdf(tailored_cv, os.path.join(job_dir_path, "CV.pdf"))
        job_folder.write_text(os.path.join(job_dir_path, "CoverLetter.md"), cover_letter)
        convert_markdown_to_pdf(cover_letter, os.path.join(job_dir_path, "CoverLetter.pdf"))
        # Machine-readable copy for the Gmail drafter (draft_emails.py).
        job_folder.write_text(
            os.path.join(job_dir_path, "email.json"),
            json.dumps({"to": contact_email or "", "subject": subject, "body": body},
                       ensure_ascii=False, indent=2),
        )
        _write_apply_txt(
            os.path.join(job_dir_path, "APPLY.txt"),
            job, contact_email, subject, body, covered, missing, contact_source,
        )
        # Hide the tool-only files; add the double-click 'Mark as applied'
        job_folder.finalize(job_dir_path, job.get("id"))

        if missing:
            logger.info(f"   ATS: {len(covered)}/{len(covered) + len(missing)} keywords covered; "
                        f"missing {missing}")
        return {
            "job_dir": job_dir_name,
            "docs_dir": job_dir_path,
            "contact_email": contact_email,
            "subject": subject,
            "covered": covered,
            "missing": missing,
        }
    except Exception as ex:
        logger.error(f"   Error building application for {title}: {ex}")
        return None


def generate_documents(cv_text, company, job_title, job_link, output_dir, job_description=None):
    """Legacy scan-mode wrapper: build an application from the minimal fields a
    scraped match carries. Returns the job directory name, or None."""
    result = build_application(
        cv_text,
        {"title": job_title, "company": company, "url": job_link,
         "description": job_description, "country": "uk"},
        output_dir,
    )
    return result["job_dir"] if result else None


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


def move_to_applied(docs_dir):
    """Move a job's folder from its dated run folder into a sibling Applied/
    folder (data/output/<Country>/Applied/<job>), so the dated folders only
    hold applications still to send. Returns the new path, or None."""
    if not docs_dir or not os.path.isdir(docs_dir):
        return None
    # Climb to the YYYY-MM-DD run folder; Applied/ sits next to it
    date_dir = os.path.dirname(docs_dir)
    while not re.fullmatch(r"\d{4}-\d{2}-\d{2}", os.path.basename(date_dir)):
        parent = os.path.dirname(date_dir)
        if parent == date_dir:
            return None  # not inside a dated folder; leave it alone
        date_dir = parent
    applied_dir = os.path.join(os.path.dirname(date_dir), "Applied")
    os.makedirs(applied_dir, exist_ok=True)
    dest = os.path.join(applied_dir, os.path.basename(docs_dir))
    suffix = 2
    while os.path.exists(dest):
        dest = os.path.join(applied_dir, f"{os.path.basename(docs_dir)}_{suffix}")
        suffix += 1
    shutil.move(docs_dir, dest)
    return dest


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
            f"- **Apply pack:** `{os.path.join(r['docs_dir'] or '', 'APPLY.txt')}`\n"
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

    def job_dict(row):
        return {
            "title": row["title"], "company": row["company"], "url": row["url"],
            "description": row["description"], "score": row["match_score"],
            "match_reason": row["match_reason"],
            "sponsor_match": row["sponsor_match"], "sponsor_name": row["sponsor_name"],
            "salary_min": row["salary_min"], "salary_max": row["salary_max"],
            "location": row["location"], "posted_date": row["posted_date"],
            "found_date": row["found_date"], "country": country, "id": row["id"],
        }

    # Build the shortlist concurrently; persist each result in THIS thread as it
    # lands (sqlite connections are single-thread). A job that errors stays
    # 'shortlisted' and is retried next run.
    workers = max(1, min(GENERATE_WORKERS, len(shortlisted)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(build_application, cv_text, job_dict(row), output_dir): row
            for row in shortlisted
        }
        for future in as_completed(futures):
            row = futures[future]
            result = future.result()
            if not result:
                continue
            db.set_status(conn, row["id"], "generated",
                          docs_dir=result["docs_dir"],
                          contact_email=result["contact_email"])
            docs_generated += 1
            job_dir = result["job_dir"]
            sponsor_line = (
                f"- **Sponsor:** {row['sponsor_match']} ({row['sponsor_name']})\n"
                if cc["sponsor_filter"] else ""
            )
            ats_line = ""
            if result["covered"] or result["missing"]:
                total = len(result["covered"]) + len(result["missing"])
                ats_line = f"- **ATS keywords:** {len(result['covered'])}/{total} covered"
                if result["missing"]:
                    ats_line += f" (missing: {', '.join(result['missing'])})"
                ats_line += "\n"
            append_report(
                report_path,
                f"### {row['title']} - {row['company']} (score {row['match_score']}/10)\n"
                f"- **Link:** {row['url']}\n"
                f"{sponsor_line}"
                f"- **Contact:** {result['contact_email'] or 'not found'}\n"
                f"{ats_line}"
                f"- **Why:** {row['match_reason']}\n"
                f"- **Apply pack:** [APPLY.txt](./{job_dir}/APPLY.txt) | "
                f"[CV.pdf](./{job_dir}/CV.pdf) | [CoverLetter.pdf](./{job_dir}/CoverLetter.pdf)\n\n",
            )

    write_applications_md(conn)
    db.finish_run(conn, run_id, len(fetched), len(new_ids), docs_generated)
    logger.info(f"\n{cc['label']} pipeline complete! {docs_generated} application(s) generated. Report: {report_path}")
    send_toast(
        f"{cc['label']}: {len(new_ids)} new jobs, {docs_generated} applications generated.",
        output_dir,
    )


# --- Legacy scan mode ---

def get_companies_batch(batch_size):
    df = load_register_df(config.COMPANIES_XLSX_PATH)
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
    parser.add_argument("--mark-applied", type=int, nargs="+", metavar="JOB_ID",
                        help="Mark job(s) (by ID from applications.md) as applied, move their "
                             "folders into Applied/, then exit")
    args = parser.parse_args()

    if not first_run.ensure_ready():
        return  # first-time setup, or a missing CV — guidance already printed

    if args.mark_applied:
        conn = db.get_conn()
        for job_id in args.mark_applied:
            row = db.mark_applied(conn, job_id)
            if not row:
                logger.error(f"No job with ID {job_id}.")
                continue
            logger.info(f"Marked as applied: [{row['id']}] {row['title']} - {row['company']}")
            try:
                moved = move_to_applied(row["docs_dir"])
            except OSError as e:
                # Usually a file in the folder is open in another program
                moved = None
                logger.warning(f"   Could not move the folder ({e}); close any open files and retry.")
            if moved:
                db.set_status(conn, job_id, "applied", docs_dir=moved)
                logger.info(f"   Folder moved to {moved}")
        write_applications_md(conn)
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
