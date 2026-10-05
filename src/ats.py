"""ATS keyword extraction and coverage.

Applicant Tracking Systems rank a CV largely on whether it literally contains
the skills and tools the posting asks for. Two cheap steps raise that score:

  1. extract_keywords() pulls the must-have terms out of a job description so
     the CV-writing prompt can be told to weave in the ones the candidate can
     honestly claim.
  2. coverage() reports which of those terms actually made it into the finished
     CV, so a thin match is visible in the report rather than silent.

Both are best-effort: extraction falls back to an empty list if the LLM is
unavailable (the CV is still generated, just without the keyword hint), and
coverage is a plain substring check - no LLM, no cost.
"""
import json
import re

import config
from llm_client import generate_content, LLMError
from logger import get_logger

logger = get_logger()


def extract_keywords(job_description, job_title=""):
    """Return a list of must-have keywords/skills from the posting (<=15).

    Empty list if there is no description or the LLM is unavailable.
    """
    jd = (job_description or "").strip()
    if len(jd) < 40:
        return []

    prompt = f"""
    You are an ATS (Applicant Tracking System) analyst. From the job posting
    below, extract the concrete skills, tools, technologies, certifications and
    qualifications an ATS would scan for. Role: "{job_title}".

    Rules:
    - Only terms actually stated or clearly implied by the posting.
    - Prefer specific nouns (e.g. "Power BI", "SQL", "ITIL", "Office 365",
      "stakeholder management") over vague phrases ("team player").
    - Return at most 15, most important first. No duplicates.

    JOB POSTING:
    {jd[:2500]}

    Return ONLY JSON: {{"keywords": ["term1", "term2", ...]}}
    """
    try:
        raw = generate_content(
            prompt, is_json=True,
            temperature=config.MATCH_TEMPERATURE,
            model=config.MATCH_MODEL,
        )
        data = json.loads(raw)
        seen, out = set(), []
        for kw in data.get("keywords", []):
            kw = str(kw).strip()
            key = kw.lower()
            if kw and key not in seen:
                seen.add(key)
                out.append(kw)
        return out[:15]
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.info(f"   Keyword extraction skipped: {e}")
        return []


def coverage(cv_text, keywords):
    """Which keywords appear in the CV. Returns (covered, missing).

    Word-boundary, case-insensitive, punctuation-insensitive so "Office 365"
    matches "office 365" and "Power-BI" matches "Power BI".
    """
    if not keywords:
        return [], []
    haystack = re.sub(r"[^a-z0-9 ]+", " ", (cv_text or "").lower())
    haystack = re.sub(r"\s+", " ", haystack)
    covered, missing = [], []
    for kw in keywords:
        needle = re.sub(r"[^a-z0-9 ]+", " ", kw.lower())
        needle = re.sub(r"\s+", " ", needle).strip()
        if needle and needle in haystack:
            covered.append(kw)
        else:
            missing.append(kw)
    return covered, missing
