"""CV parsing and profile inference."""
import docx2txt
import config
from llm_client import generate_content
from logger import get_logger

logger = get_logger()


def extract_text_from_docx(file_path):
    """Extracts raw text from a given docx file."""
    try:
        return docx2txt.process(file_path)
    except Exception as e:
        logger.error(f"Error reading docx: {e}")
        return ""


def infer_job_titles_and_skills(cv_text):
    """Extracts core skills and infers target job titles.

    Returns a JSON string. Raises LLMError if all providers fail.
    """
    prompt = f"""
    You are an expert technical recruiter and career coach.
    Analyze the following CV.
    1. Extract the core skills.
    2. Infer the top 10 best matching job titles/roles for this candidate.

    Return the result exactly in this JSON format without any markdown blocks or extra text:
    {{
        "skills": ["skill1", "skill2", ...],
        "inferred_titles": ["title1", "title2", "title3", "...", "title10"],
        "summary": "A brief 2-sentence summary of the candidate's profile."
    }}

    CV TEXT:
    {cv_text}
    """

    return generate_content(
        prompt, is_json=True,
        temperature=config.MATCH_TEMPERATURE,
        model=config.MATCH_MODEL,
    )
