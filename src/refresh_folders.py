"""Bring already-generated job folders up to the current layout.

Usage (from the project folder, while the main tool is NOT running):
    venv\\Scripts\\python.exe src\\refresh_folders.py

For every job folder the database knows about it:
  - adds a greeting / sign-off to the cover letter if the model left them out
  - rebuilds CV.pdf and CoverLetter.pdf with the current PDF layout
    (from the saved .md files - no LLM calls, nothing is rewritten)
  - removes the old Email.md (the email is already in APPLY.txt)
  - hides CV.md / CoverLetter.md / email.json and adds 'Mark as applied.bat'
Safe to run more than once.
"""
import os

import db
import job_folder
from logger import get_logger
from pdf_generator import convert_markdown_to_pdf
from validation import ensure_letter_frame

logger = get_logger()

OLD_FILES_LINE = "Files in this folder:  CV.pdf, CoverLetter.pdf, Email.md"
NEW_FILES_LINE = ("Files in this folder:  CV.pdf, CoverLetter.pdf\n"
                  "Applied? Double-click 'Mark as applied.bat' in this folder.")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def refresh(job_id, job_dir):
    problems = []
    cl_md = os.path.join(job_dir, "CoverLetter.md")
    if os.path.exists(cl_md):
        letter = _read(cl_md)
        framed = ensure_letter_frame(letter)
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
    if os.path.exists(apply_txt):
        text = _read(apply_txt)
        if OLD_FILES_LINE in text:
            job_folder.write_text(apply_txt, text.replace(OLD_FILES_LINE, NEW_FILES_LINE))
    job_folder.finalize(job_dir, job_id)
    return problems


def main():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT id, title, company, docs_dir FROM jobs "
        "WHERE docs_dir IS NOT NULL AND docs_dir != ''"
    ).fetchall()
    done = 0
    for row in rows:
        if not os.path.isdir(row["docs_dir"]):
            continue
        problems = refresh(row["id"], row["docs_dir"])
        done += 1
        for p in problems:
            logger.warning(f"[{row['id']}] {row['title']} - {row['company']}: {p}")
    logger.info(f"Refreshed {done} job folder(s).")


if __name__ == "__main__":
    main()
