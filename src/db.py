"""SQLite persistence for the jobs-first pipeline.

Gives the pipeline job-level memory across runs: a vacancy seen yesterday is
never re-scored or re-generated, and every job carries a status through
found -> shortlisted -> generated -> applied.
"""
import os
import re
import sqlite3
from datetime import datetime

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'uk',
    dedup_key TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    url TEXT,
    description TEXT,
    salary_min REAL,
    salary_max REAL,
    posted_date TEXT,
    found_date TEXT NOT NULL,
    sponsor_match TEXT,          -- 'exact' | 'probable' | NULL
    sponsor_name TEXT,           -- name as it appears in the register
    match_score INTEGER,
    match_reason TEXT,
    contact_email TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    -- new | no_sponsor | excluded_title | low_score | shortlisted |
    -- generated | applied | rejected
    docs_dir TEXT,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT NOT NULL,
    finished TEXT,
    mode TEXT,
    jobs_fetched INTEGER DEFAULT 0,
    jobs_new INTEGER DEFAULT 0,
    docs_generated INTEGER DEFAULT 0
);
"""


def get_conn(db_path=None):
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Adds columns introduced after the table was first created."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "country" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN country TEXT NOT NULL DEFAULT 'uk'")
        conn.commit()
    if "contact_email" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN contact_email TEXT")
        conn.commit()


def dedup_key(title, company):
    """Stable key so the same vacancy from two boards counts once."""
    text = f"{title} {company}".lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def insert_job(conn, job):
    """Inserts a job if unseen. Returns the row id, or None if it was a
    duplicate (same source posting, or same title+company from another source)."""
    key = dedup_key(job["title"], job["company"])
    country = job.get("country", "uk")
    existing = conn.execute(
        "SELECT id FROM jobs WHERE (source = ? AND source_id = ?) OR (dedup_key = ? AND country = ?)",
        (job["source"], str(job["source_id"]), key, country),
    ).fetchone()
    if existing:
        return None

    cur = conn.execute(
        """INSERT INTO jobs (source, source_id, country, dedup_key, title, company,
               location, url, description, salary_min, salary_max,
               posted_date, found_date, sponsor_match, sponsor_name, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["source"], str(job["source_id"]), country, key, job["title"], job["company"],
            job.get("location"), job.get("url"), job.get("description"),
            job.get("salary_min"), job.get("salary_max"),
            job.get("posted_date"), datetime.now().strftime("%Y-%m-%d"),
            job.get("sponsor_match"), job.get("sponsor_name"),
            job.get("status", "new"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_status(conn, job_id, status, **fields):
    sets = ["status = ?"]
    values = [status]
    for col in ("match_score", "match_reason", "docs_dir", "contact_email"):
        if col in fields:
            sets.append(f"{col} = ?")
            values.append(fields[col])
    values.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def jobs_with_status(conn, status, country=None):
    if country:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND country = ? ORDER BY id",
            (status, country),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY id", (status,)
    ).fetchall()


def pending_applications(conn):
    return conn.execute(
        "SELECT * FROM jobs WHERE status = 'generated' ORDER BY match_score DESC, found_date DESC"
    ).fetchall()


def mark_applied(conn, job_id):
    row = conn.execute("SELECT id, title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row:
        set_status(conn, job_id, "applied")
    return row


def start_run(conn, mode):
    cur = conn.execute(
        "INSERT INTO runs (started, mode) VALUES (?, ?)",
        (datetime.now().isoformat(timespec="seconds"), mode),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, jobs_fetched, jobs_new, docs_generated):
    conn.execute(
        "UPDATE runs SET finished = ?, jobs_fetched = ?, jobs_new = ?, docs_generated = ? WHERE id = ?",
        (datetime.now().isoformat(timespec="seconds"), jobs_fetched, jobs_new, docs_generated, run_id),
    )
    conn.commit()
