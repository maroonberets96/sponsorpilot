"""Create Gmail drafts for generated applications that have a contact email.

Run after a jobs-mode run:

    python src/draft_emails.py

For every job the pipeline marked 'generated' that has a found contact email
and no draft yet, this creates a Gmail draft addressed to that contact, with
the tailored CV and cover letter PDFs attached and the body from email.json.
The draft id is stored in the DB so re-running never duplicates. Nothing is
ever sent - you review and send each draft yourself from Gmail.

One-time setup (Google OAuth, same kind JARVIS uses):
  1. In Google Cloud Console create an OAuth client (type: Desktop app) for the
     Gmail API, download its JSON, and save it as:
         data/google/client_secret.json      (or set GOOGLE_CLIENT_SECRET)
  2. Run this script; a browser opens once to grant "compose" access. The token
     is cached at data/google/gmail_token.json for future headless runs.

If the libraries or client secret are missing, the script explains what to do
and exits without touching anything.
"""
import base64
import json
import os
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(__file__))

import config
import db
from logger import get_logger

logger = get_logger()

SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
GOOGLE_DIR = os.path.join(config.DATA_DIR, "google")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", os.path.join(GOOGLE_DIR, "client_secret.json"))
TOKEN_PATH = os.path.join(GOOGLE_DIR, "gmail_token.json")


def _gmail_service():
    """Authenticate and return a Gmail API service, or None with guidance."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("Google libraries missing. Install them with:\n"
                     "  venv\\Scripts\\pip install google-api-python-client "
                     "google-auth-oauthlib")
        return None

    os.makedirs(GOOGLE_DIR, exist_ok=True)
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                logger.error(
                    "No Google OAuth client secret found.\n"
                    f"  Save your OAuth 'Desktop app' client JSON at:\n    {CLIENT_SECRET}\n"
                    "  (Google Cloud Console -> APIs & Services -> Credentials -> "
                    "Create credentials -> OAuth client ID -> Desktop app, enable the Gmail API.)"
                )
                return None
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _build_message(to_addr, subject, body, attachments):
    msg = EmailMessage()
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as f:
            msg.add_attachment(
                f.read(), maintype="application", subtype="pdf",
                filename=os.path.basename(path),
            )
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return {"message": {"raw": raw}}


def _load_email(docs_dir):
    """Read {to, subject, body} from the job folder's email.json."""
    path = os.path.join(docs_dir, "email.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    conn = db.get_conn()
    jobs = db.jobs_to_draft(conn)
    if not jobs:
        logger.info("No generated applications awaiting a draft (need a found contact email).")
        return

    service = _gmail_service()
    if not service:
        return  # guidance already logged

    created = 0
    for row in jobs:
        docs_dir = row["docs_dir"]
        if not docs_dir or not os.path.isdir(docs_dir):
            logger.warning(f"[{row['id']}] {row['title']}: docs folder missing, skipping.")
            continue
        email = _load_email(docs_dir)
        to_addr = (email or {}).get("to") or row["contact_email"]
        if not email or not to_addr:
            logger.warning(f"[{row['id']}] {row['title']}: no email.json/recipient, skipping.")
            continue

        attachments = [os.path.join(docs_dir, "CV.pdf"),
                       os.path.join(docs_dir, "CoverLetter.pdf")]
        try:
            body = _build_message(to_addr, email["subject"], email["body"], attachments)
            draft = service.users().drafts().create(userId="me", body=body).execute()
            db.mark_email_drafted(conn, row["id"], draft["id"])
            created += 1
            logger.info(f"[{row['id']}] Draft created -> {to_addr}  ({row['title']} at {row['company']})")
        except Exception as e:
            logger.error(f"[{row['id']}] Draft failed for {row['title']}: {e}")

    logger.info(f"\nDone. {created} Gmail draft(s) created. Review and send them from Gmail "
                f"(Drafts folder).")


if __name__ == "__main__":
    main()
