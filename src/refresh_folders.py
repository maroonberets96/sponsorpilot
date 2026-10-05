"""Bring already-generated job folders up to the current layout.

Usage (from the project folder, while the main tool is NOT running):
    venv\\Scripts\\python.exe src\\refresh_folders.py

For every job folder the database knows about it:
  - adds a greeting / sign-off to the cover letter and email if missing
  - writes your phone in international format for intl_phone countries
  - rebuilds CV.pdf and CoverLetter.pdf with the current PDF layout
    (from the saved .md files - no LLM calls, nothing is rewritten)
  - removes the old Email.md (the email is already in APPLY.txt)
  - hides CV.md / CoverLetter.md / email.json and adds 'Mark as applied.bat'
Safe to run more than once.
"""
import json
import os

import config
import db
import job_folder
from logger import get_logger
from pdf_generator import convert_markdown_to_pdf
from validation import ensure_letter_frame, ensure_email_frame, international_phone

logger = get_logger()

OLD_FILES_LINE = "Files in this folder:  CV.pdf, CoverLetter.pdf, Email.md"
NEW_FILES_LINE = ("Files in this folder:  CV.pdf, CoverLetter.pdf\n"
                  "Applied? Double-click 'Mark as applied.bat' in this folder.")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _candidate_name(job_dir):
    """The '# Name' header of the folder's CV, for the email sign-off."""
    cv_md = os.path.join(job_dir, "CV.md")
    if not os.path.exists(cv_md):
        return ""
    return next((l[2:].strip() for l in _read(cv_md).splitlines() if l.startswith("# ")), "")


def _fix_text(text, phone_code):
    return international_phone(text, phone_code) if phone_code else text


def refresh(job_id, job_dir, country="uk"):
    problems = []
    cc = config.COUNTRIES.get(country, config.COUNTRIES["uk"])
    phone_code = config.PHONE_COUNTRY_CODE if cc.get("intl_phone") else ""

    cv_md = os.path.join(job_dir, "CV.md")
    if os.path.exists(cv_md):
        cv = _read(cv_md)
        fixed = _fix_text(cv, phone_code)
        if fixed != cv:
            job_folder.write_text(cv_md, fixed)
    cl_md = os.path.join(job_dir, "CoverLetter.md")
    if os.path.exists(cl_md):
        letter = _read(cl_md)
        framed = _fix_text(ensure_letter_frame(letter), phone_code)
        if framed != letter:
            job_folder.write_text(cl_md, framed)
    for name in ("CV", "CoverLetter"):
        md = os.path.join(job_dir, f"{name}.md")
        if not os.path.exists(md):
            continue
        try:
            convert_markdown_to_pdf(_read(md), os.path.join(job_dir, f"{name}.pdf"))
        except Exception as e:  # usually the PDF is open in a viewer
            problems.append(f"{name}.pdf not rebuilt ({e})")
    email_md = os.path.join(job_dir, "Email.md")
    if os.path.exists(email_md):
        os.remove(email_md)
    apply_txt = os.path.join(job_dir, "APPLY.txt")
    apply_text = _read(apply_txt) if os.path.exists(apply_txt) else None
    if apply_text is not None:
        apply_text = apply_text.replace(OLD_FILES_LINE, NEW_FILES_LINE)

    # Email: make sure it greets and signs off (the copy in APPLY.txt too)
    email_json = os.path.join(job_dir, "email.json")
    if os.path.exists(email_json):
        email = json.loads(_read(email_json))
        name = _candidate_name(job_dir)
        framed = _fix_text(ensure_email_frame(email.get("body", ""), name), phone_code)
        if framed != email.get("body"):
            if apply_text is not None and email.get("body") and email["body"] in apply_text:
                apply_text = apply_text.replace(email["body"], framed)
            email["body"] = framed
            job_folder.write_text(email_json, json.dumps(email, ensure_ascii=False, indent=2))

    if apply_text is not None:
        job_folder.write_text(apply_txt, apply_text)
    job_folder.finalize(job_dir, job_id)
    return problems


def main():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, title, company, country, docs_dir FROM jobs "
        "WHERE docs_dir IS NOT NULL AND docs_dir != ''"
    ).fetchall()
    done = 0
    for row in rows:
        if not os.path.isdir(row["docs_dir"]):
            continue
        problems = refresh(row["id"], row["docs_dir"], row["country"] or "uk")
        done += 1
        for p in problems:
            logger.warning(f"[{row['id']}] {row['title']} - {row['company']}: {p}")
    logger.info(f"Refreshed {done} job folder(s).")


if __name__ == "__main__":
    main()
