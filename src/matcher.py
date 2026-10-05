"""LLM scoring of sponsor-matched jobs against the candidate profile."""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from llm_client import generate_content, LLMError
from logger import get_logger

logger = get_logger()

# Batches are independent LLM calls. Running a few at once lets the provider
# waterfall spread them (and overlaps the network latency) without hammering a
# single free tier; failover absorbs any rate limit that does hit.
SCORE_WORKERS = 4


def title_prefilter(title):
    """Cheap in-code filter mirroring the matching rules.

    Returns None if the title is acceptable, or a reason string if excluded.
    """
    t = f" {title.lower()} "
    for kw in config.SENIOR_TITLE_KEYWORDS:
        if kw in t:
            return f"senior-level keyword '{kw.strip()}'"
    if any(allow in t for allow in config.DEV_TITLE_ALLOW):
        return None
    for kw in config.DEV_TITLE_KEYWORDS:
        if kw in t:
            return f"development keyword '{kw.strip()}'"
    return None


def _score_prompt(payload, profile, target_titles):
    return f"""
        You are an expert recruiter scoring job vacancies for a candidate.

        CANDIDATE PROFILE:
        {profile.get('summary', '')}
        Core skills: {json.dumps(profile.get('skills', []))}
        Target roles: {json.dumps(target_titles)}

        SCORING RULES:
        - The candidate wants entry-level to mid-level roles. Senior / Lead /
          Head / Principal / Director roles score at most 3.
        - Software development roles (backend, frontend, mobile, ML engineering)
          score at most 3. Data analytics, IT support, business analysis,
          process automation, and Power Platform roles are all good fits.
        - 9-10: title and description align directly with the target roles and skills.
        - 7-8: strong overlap, worth applying.
        - 4-6: partial overlap.
        - 1-3: poor fit or excluded category.

        JOBS:
        {json.dumps(payload, ensure_ascii=False)}

        Return a JSON object: {{"scores": [{{"id": <job id>, "score": <1-10>,
        "reason": "<one sentence>"}}]}}. Include every job exactly once.
        Output ONLY valid JSON.
        """


def _score_batch(batch, profile, target_titles):
    """Scores one batch. Returns {job_id: (score, reason)}, or raises LLMError."""
    payload = [
        {
            "id": j["id"],
            "title": j["title"],
            "company": j["company"],
            "location": j["location"],
            "salary": f"{j['salary_min'] or '?'}-{j['salary_max'] or '?'}",
            "description": (j["description"] or "")[:400],
        }
        for j in batch
    ]
    result_str = generate_content(
        _score_prompt(payload, profile, target_titles),
        is_json=True,
        temperature=config.MATCH_TEMPERATURE,
        prefer=config.SCORE_PREFERENCES,
    )
    out = {}
    try:
        parsed = json.loads(result_str)
        for entry in parsed.get("scores", []):
            job_id = entry.get("id")
            score = entry.get("score")
            if isinstance(job_id, int) and isinstance(score, (int, float)):
                out[job_id] = (int(score), str(entry.get("reason", ""))[:500])
    except (json.JSONDecodeError, AttributeError) as e:
        logger.error(f"Could not parse scoring response: {e}. Raw: {result_str[:300]}")
    return out


def score_jobs(jobs, profile, target_titles):
    """Scores jobs 1-10 for fit. `jobs` is a list of sqlite Rows (or dicts)
    with id, title, company, location, description, salary fields.

    Batches are scored concurrently. Returns {job_id: (score, reason)}, or None
    if the LLM never answered any batch (so the caller can leave jobs 'new').
    """
    batches = [
        jobs[start:start + config.SCORE_BATCH_SIZE]
        for start in range(0, len(jobs), config.SCORE_BATCH_SIZE)
    ]
    results = {}
    answered = False
    workers = max(1, min(SCORE_WORKERS, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_score_batch, b, profile, target_titles) for b in batches]
        for future in as_completed(futures):
            try:
                results.update(future.result())
                answered = True
            except LLMError as e:
                logger.error(f"LLM unavailable while scoring a batch: {e}")

    # Only "never got any answer" returns None; a partial result still lets the
    # answered jobs through, and the rest stay 'new' for the next run.
    return results if answered else None
