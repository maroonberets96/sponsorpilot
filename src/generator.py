"""Tailored CV and cover letter generation."""
import config
from llm_client import generate_content
from logger import get_logger

logger = get_logger()


def _job_context(job_title, job_link, job_description):
    context = f"I am applying for the '{job_title}' position (Link: {job_link})."
    if job_description:
        context += f"\n\n    JOB DESCRIPTION (from the posting):\n    {job_description[:2000]}"
    return context


def generate_tailored_cv(base_cv_text, job_title, job_link, job_description=None):
    """Generates a tailored CV in Markdown. Raises LLMError if all providers fail."""

    prompt = f"""
    You are an expert career coach and professional CV writer.
    {_job_context(job_title, job_link, job_description)}

    Here is my Base CV:
    {base_cv_text}

    TASK:
    Rewrite and tailor my CV specifically for this role.
    - Highlight the skills and experiences that are most relevant to '{job_title}'.
    - Do NOT invent fake experience. Only rephrase or emphasize what is already there.
    - NEVER describe me as holding the advertised job title (e.g. do not open the
      profile with "professional {job_title}"). Keep my professional identity as it
      appears in the Base CV; show fit by emphasizing relevant experience, not by
      relabeling me.
    - Job titles under Professional Experience MUST be copied verbatim from the
      Base CV. Never rename, upgrade, or reword a past position.
    - Keep my name and contact details EXACTLY as they appear in the Base CV. Do not alter, invent, or omit any contact information.

    FORMATTING RULES:
    1. The top header MUST be standard Markdown: '# <My Name>' on the first line, and my contact info from the Base CV as a single paragraph on the next line, separated by ' | '. Do NOT wrap it in <center> or any HTML tags.
    2. ALL section titles (e.g., Professional Profile, Core Competencies, Professional Experience, Education) MUST be formatted as Markdown Heading 2 (e.g., '## Professional Profile'). Do NOT use bold text (**) for section titles.
    3. ALL job titles/roles under Professional Experience MUST be formatted as Markdown Heading 3 (e.g., '### IT & Facilities Officer - Company'). Do NOT use bold text (**) for job titles.
    4. You MUST leave a blank empty line before starting any bulleted list so that it renders correctly as a list and not as a paragraph.
    5. Do NOT bold the text of the bullet points in the "Core Competencies" or "Skills" section. Keep them as plain text bullet points.
    6. ABSOLUTELY NO CONVERSATIONAL FILLER. Do not include any introductory text or concluding sentences. ONLY output the actual CV content and nothing else.
    7. Do NOT use Markdown tables (e.g., | Category |). Use standard bulleted lists instead.
    8. CRITICAL: DO NOT remove spaces between words! DO NOT combine words together (e.g., "end to end" must NOT become "endtoend"). Ensure perfect English grammar, spelling, and spacing.
    """

    return generate_content(
        prompt, is_json=False,
        temperature=config.WRITE_TEMPERATURE,
        model=config.WRITE_MODEL,
    )


def generate_cover_letter(base_cv_text, job_title, job_link, job_description=None):
    """Generates a tailored cover letter in Markdown. Raises LLMError if all providers fail."""

    prompt = f"""
    You are an expert career coach and professional writer.
    {_job_context(job_title, job_link, job_description)}

    Here is my Base CV:
    {base_cv_text}

    TASK:
    Write a compelling, professional cover letter specifically tailored for this role.
    - Highlight the skills and experiences that are most relevant to '{job_title}'.
    - Keep it concise (3-4 paragraphs max).
    - Do NOT invent fake experience. Only reference what is in the base CV.
    - NEVER claim I currently hold or have held the advertised job title, and never
      rename my past positions. Refer to my roles exactly as titled in the Base CV;
      express fit through relevant experience and skills instead.
    - Use my name and contact details EXACTLY as they appear in the Base CV.

    FORMATTING RULES:
    1. The top header MUST be standard Markdown: '# <My Name>' on the first line, and my contact info from the Base CV as a single paragraph on the next line, separated by ' | '. Do NOT wrap it in <center> or any HTML tags.
    2. Output the final Cover Letter in clean Markdown format. Do NOT use any HTML tags.
    3. ABSOLUTELY NO CONVERSATIONAL FILLER. Do not include any introductory or concluding text (e.g., "Here is your Cover Letter..."). ONLY output the actual Cover Letter content and nothing else.
    4. CRITICAL: DO NOT remove spaces between words! DO NOT combine words together (e.g., "firstcontact" must be "first contact"). Ensure perfect English grammar, spelling, and spacing.
    """

    return generate_content(
        prompt, is_json=False,
        temperature=config.WRITE_TEMPERATURE,
        model=config.WRITE_MODEL,
    )
