"""LLM scoring of sponsor-matched jobs against the candidate profile."""
import json

import config
from llm_client import generate_content, LLMError
from logger import get_logger

logger = get_logger()


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


def score_jobs(jobs, profile, target_titles):
    """Scores jobs 1-10 for fit. `jobs` is a list of sqlite Rows (or dicts)
    with id, title, company, location, description, salary fields.

    Returns {job_id: (score, reason)}, or None if the LLM never answered.
    """
    results = {}
    for start in range(0, len(jobs), config.SCORE_BATCH_SIZE):
        batch = jobs[start:start + config.SCORE_BATCH_SIZE]
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

        prompt = f"""
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

        try:
            result_str = generate_content(
                prompt, is_json=True,
                temperature=config.MATCH_TEMPERATURE,
                model=config.MATCH_MODEL,
            )
        except LLMError as e:
            logger.error(f"LLM unavailable while scoring jobs: {e}")
            return None

        try:
            parsed = json.loads(result_str)
            for entry in parsed.get("scores", []):
                job_id = entry.get("id")
                score = entry.get("score")
                if isinstance(job_id, int) and isinstance(score, (int, float)):
                    results[job_id] = (int(score), str(entry.get("reason", ""))[:500])
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"Could not parse scoring response: {e}. Raw: {result_str[:300]}")

    return results
