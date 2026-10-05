"""Tailored CV, cover letter and application-email generation.

Each document is generated with the quality-first write chain, then validated
(validation.py) and regenerated once if it comes back with a placeholder,
filler opening, or missing header. The CV prompt is seeded with the posting's
ATS keywords (ats.py) so the finished CV literally contains the terms the
employer's tracking system scans for - only where the candidate can honestly
claim them.
"""
import json

import config
from llm_client import generate_content, LLMError
from validation import validate_document, strip_placeholder_lines, ensure_letter_frame
from logger import get_logger

logger = get_logger()


def _job_context(job_title, job_link, job_description):
    context = f"I am applying for the '{job_title}' position (Link: {job_link})."
    if job_description:
        context += f"\n\n    JOB DESCRIPTION (from the posting):\n    {job_description[:2000]}"
    return context


def _keyword_block(keywords):
    if not keywords:
        return ""
    return (
        "\n    ATS KEYWORDS: the employer's tracking system scans for these terms: "
        + ", ".join(keywords)
        + ".\n    Where - and ONLY where - my Base CV genuinely supports them, use the"
        " exact wording of these terms so they are picked up. Never claim a skill"
        " the Base CV does not evidence.\n"
    )


def _generate_validated(prompt, kind, temperature):
    """Generate with the quality-first write chain, then validate. On a hard
    failure (placeholder/filler/missing header) retry ONCE with a corrective
    note appended. Returns the best text produced either way."""
    text = generate_content(
        prompt, is_json=False, temperature=temperature,
        prefer=config.WRITE_PREFERENCES,
    )
    ok, issues = validate_document(text, kind)
    if ok:
        if issues:
            logger.info(f"   {kind} note: {issues}")
        return strip_placeholder_lines(text)

    logger.warning(f"   {kind} failed validation ({issues}); regenerating once.")
    retry_prompt = prompt + (
        "\n\n    IMPORTANT: Output ONLY the finished document. Do not include any"
        " bracketed placeholders like [Name], [Company] or [Date], do not open"
        " with phrases like 'Here is', and start directly with the '# <My Name>'"
        " Markdown header. Fill every field from the Base CV; omit anything you"
        " cannot fill rather than leaving a placeholder."
    )
    retry = generate_content(
        retry_prompt, is_json=False, temperature=temperature,
        prefer=config.WRITE_PREFERENCES,
    )
    ok2, issues2 = validate_document(retry, kind)
    chosen = retry if ok2 else text
    if not ok2:
        logger.warning(f"   {kind} still imperfect after retry ({issues2}); "
                       f"sanitising placeholder lines and using best effort.")
    # Final safety net: drop any lone placeholder line (e.g. a stubborn [Date])
    # so nothing with a bracketed stub ever reaches the PDF.
    return strip_placeholder_lines(chosen)


def generate_tailored_cv(base_cv_text, job_title, job_link, job_description=None, keywords=None):
    """Generates a tailored CV in Markdown. Raises LLMError if all providers fail."""

    prompt = f"""
    You are an expert career coach and professional CV writer.
    {_job_context(job_title, job_link, job_description)}
    {_keyword_block(keywords)}
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
    5. Do NOT use bold (**) anywhere - not for keywords, skills or phrases inside bullets or paragraphs. Plain text throughout; headings carry the structure.
    6. ABSOLUTELY NO CONVERSATIONAL FILLER. Do not include any introductory text or concluding sentences. ONLY output the actual CV content and nothing else.
    7. Do NOT use Markdown tables (e.g., | Category |). Use standard bulleted lists instead.
    8. CRITICAL: DO NOT remove spaces between words! DO NOT combine words together (e.g., "end to end" must NOT become "endtoend"). Ensure perfect English grammar, spelling, and spacing.
    """

    return _generate_validated(prompt, "CV", config.WRITE_TEMPERATURE)


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
    5. Do NOT include a date line, an address block, or ANY bracketed placeholder such as [Date], [Address], [Hiring Manager] or [Company]. Omit anything you cannot fill from the Base CV - never leave a placeholder.
    6. Open with 'Dear Hiring Manager,' on its own line, and close with 'Kind regards,' followed by my name on its own line. Do NOT use bold (**) anywhere.
    """

    letter =_generate_validated(prompt, "cover letter", config.WRITE_TEMPERATURE)
    return ensure_letter_frame(letter)


def generate_application_email(base_cv_text, job_title, company, job_link,
                               job_description=None, contact_email=None,
                               work_eligibility=None):
    """Generate a short application email. Returns (subject, body).

    `work_eligibility`, when given, is a short authorization statement woven in
    verbatim-in-meaning (e.g. UK Skilled Worker visa, or a pending Canada PR).
    Falls back to a plain template if the LLM is unavailable, so an APPLY.txt is
    always produced.
    """
    greeting_hint = (
        f"The email goes to {contact_email}." if contact_email
        else "No named contact is known; address it 'Dear Hiring Manager,'."
    )
    eligibility_rule = (
        f"- Include this work-authorization statement, kept accurate (you may "
        f"rephrase lightly for flow but NOT change its meaning): \"{work_eligibility}\" "
        f"Place it as a brief, natural sentence near the end - do not overemphasise it."
        if work_eligibility else
        "- Do not discuss visas or work authorization."
    )
    prompt = f"""
    You are writing a concise, professional job-application email on my behalf.
    {_job_context(job_title, job_link, job_description)}
    Company: {company}. {greeting_hint}

    Here is my Base CV:
    {base_cv_text}

    TASK: Write a short email (3 short paragraphs max) expressing interest in the
    '{job_title}' role at {company}, highlighting my most relevant experience from
    the Base CV. State that my CV and cover letter are attached.
    - Do NOT invent experience. Do NOT claim to currently hold the advertised title.
    {eligibility_rule}
    - No bracketed placeholders. Sign off with my name from the Base CV.
    - Plain text body (no markdown, no HTML).

    Return ONLY JSON: {{"subject": "<email subject line>", "body": "<email body>"}}
    """
    try:
        raw = generate_content(
            prompt, is_json=True, temperature=config.WRITE_TEMPERATURE,
            prefer=config.WRITE_PREFERENCES,
        )
        data = json.loads(raw)
        subject = str(data.get("subject", "")).strip()
        body = str(data.get("body", "")).strip()
        if subject and body:
            return subject, body
    except (LLMError, json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.info(f"   Email generation fell back to template: {e}")

    subject = f"Application for {job_title} - {company}"
    elig = f" {work_eligibility}" if work_eligibility else ""
    body = (
        f"Dear Hiring Manager,\n\n"
        f"I am writing to express my interest in the {job_title} position at "
        f"{company}. Please find my CV and cover letter attached.\n\n"
        f"I would welcome the opportunity to discuss how my experience fits this "
        f"role.{elig}\n\nKind regards"
    )
    return subject, body
