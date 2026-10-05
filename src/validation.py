"""Cheap, LLM-free sanity checks on generated documents.

The CV/cover-letter prompts forbid several things (placeholders, conversational
filler, joined-up words, a missing name header). Models mostly obey, but "mostly"
means one application in a batch quietly ships with "[Hiring Manager]" or
"Here is your cover letter" at the top. These checks catch that before the PDF
is made, so the generator can retry once instead of producing a dud.

validate_document() returns (ok, issues). `ok` is False only for problems bad
enough to warrant a regeneration; softer issues are returned for logging but
leave `ok` True.
"""
import re

# Bracketed template placeholders a model leaves when it invents a field it was
# not given: [Hiring Manager], [Company Name], [Date], {{name}}, <your name>.
_PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]|\{\{[^}]{1,40}\}\}|<[a-z ]{2,30}>", re.I)

# Placeholder email/phone stubs.
_STUB_RE = re.compile(r"\byour\.email@|example\.com|\[email\]|xxx-xxx|123-456", re.I)

# Conversational lead-ins the prompt bans. Checked only at the very start.
_FILLER_STARTS = (
    "here is", "here's", "sure,", "sure!", "certainly", "of course",
    "below is", "i have", "i've", "as requested", "please find",
    "absolutely", "great,", "okay", "ok,", "note:", "output:",
)


def strip_placeholder_lines(text):
    """Remove lines that are nothing but a bracketed placeholder (e.g. a lone
    '[Date]', '[Address]', '{{Company}}'). Safety net for when a model insists
    on a template field we told it to omit - dropping the whole line is safe
    because such a line carries no real content. Inline placeholders inside a
    sentence are left for validation/regeneration to catch."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and _PLACEHOLDER_RE.fullmatch(s) and "](" not in s:
            continue  # drop the placeholder-only line
        out.append(line)
    # collapse any blank-line run left behind to a single blank line
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return cleaned.strip()


def _joined_words(text):
    """Flag obvious word-joining ('endtoend', 'firstcontact'). Returns a few
    examples. Conservative: only very long all-lowercase runs, to avoid firing
    on real long words or URLs."""
    suspects = []
    for token in re.findall(r"\b[a-z]{16,}\b", text):
        if "://" in token or token.startswith("www"):
            continue
        suspects.append(token)
    return suspects[:5]


def validate_document(text, kind="document"):
    """Return (ok, issues). `kind` is 'CV' or 'cover letter' for messages."""
    issues = []
    hard_fail = False

    stripped = (text or "").strip()
    if len(stripped) < 200:
        return False, [f"{kind} is empty or too short ({len(stripped)} chars)"]

    if not stripped.lstrip("`").lstrip().startswith("#"):
        issues.append(f"{kind} does not start with a Markdown '# Name' header")
        hard_fail = True

    low_start = stripped.lstrip("# ").lower()
    if low_start.startswith(_FILLER_STARTS):
        issues.append(f"{kind} opens with conversational filler")
        hard_fail = True

    placeholders = _PLACEHOLDER_RE.findall(stripped)
    # Markdown links [text](url) are legitimate; the regex above excludes "](".
    placeholders = [p for p in placeholders if "](" not in p]
    if placeholders:
        issues.append(f"{kind} contains unfilled placeholders: {placeholders[:4]}")
        hard_fail = True

    if _STUB_RE.search(stripped):
        issues.append(f"{kind} contains a placeholder email/phone stub")
        hard_fail = True

    joined = _joined_words(stripped)
    if joined:
        # Soft: surface it, but a false positive should not block a good doc.
        issues.append(f"{kind} may have joined-up words: {joined}")

    return (not hard_fail), issues


_GREETING_RE = re.compile(r"^\s*(dear|hello|hi|to whom)\b", re.I)
_SIGNOFF_RE = re.compile(
    r"^\s*((kind|best|warm|warmest)\s+regards|regards|(yours\s+)?(sincerely|faithfully)"
    r"|yours\s+(sincerely|faithfully)|thank you)\b[,.]?\s*$", re.I | re.M)


def ensure_letter_frame(text):
    """Make sure a cover letter opens with a greeting and closes with a
    sign-off and the candidate's name (taken from the '# Name' header).
    Leaves letters that already have them untouched."""
    lines = text.strip().splitlines()
    name = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
    # Body starts after the '# Name' line and the contact line beneath it
    start = 0
    if lines and lines[0].startswith("# "):
        start = 1
        while start < len(lines) and not lines[start].strip():
            start += 1
        start += 1  # skip contact line
    body = "\n".join(lines[start:]).strip()
    head = "\n".join(lines[:start]).strip()
    if not _GREETING_RE.match(body):
        body = "Dear Hiring Manager,\n\n" + body
    if not _SIGNOFF_RE.search(body):
        body += "\n\nKind regards,"
        if name:
            body += f"\n\n{name}"
    return (head + "\n\n" + body + "\n") if head else body + "\n"


def ensure_email_frame(body, name=""):
    """Make sure a plain-text application email opens with a greeting and
    ends with a sign-off followed by the candidate's name. Keeps whatever the
    model wrote (e.g. 'Dear Tom Clark,' or a signature block) when present."""
    lines = body.strip().splitlines()
    if not lines:
        return body
    if not _GREETING_RE.match(lines[0]):
        lines = ["Dear Hiring Manager,", ""] + lines
    tail_start = max(0, len(lines) - 6)
    signoff = next((i for i in range(len(lines) - 1, tail_start - 1, -1)
                    if _SIGNOFF_RE.match(lines[i].strip())), None)
    if signoff is None:
        # Signature block without a sign-off: put one just above the name
        name_at = next((i for i in range(len(lines) - 1, tail_start - 1, -1)
                        if name and lines[i].strip() == name), None)
        if name_at is not None:
            lines.insert(name_at, "Kind regards,")
        else:
            lines += ["", "Kind regards,"] + ([name] if name else [])
    else:
        if not lines[signoff].rstrip().endswith(","):
            lines[signoff] = lines[signoff].rstrip() + ","
        after = [l.strip() for l in lines[signoff + 1:] if l.strip()]
        if name and name not in after:
            lines.insert(signoff + 1, name)
    return "\n".join(lines).strip()


# A national-format phone number: leading 0, then 9-10 more digits, spaces or
# dashes allowed (e.g. '07700 900123', '020 7946 0958'). Not already '+..'.
_NATIONAL_PHONE_RE = re.compile(r"(?<![\d+])0(\d(?:[ -]?\d){8,9})(?![\d])")


def international_phone(text, country_code):
    """Rewrite national-format phone numbers on contact lines (the lines that
    carry an email address) into international format, e.g. with '+44':
    '07700 900123' -> '+44 7700 900123'. Other lines are never touched, so
    figures elsewhere in a document can't be mistaken for a phone number.
    No-op when country_code is empty."""
    if not country_code:
        return text
    code = country_code.strip()
    if not code.startswith("+"):
        code = "+" + code
    return "\n".join(
        _NATIONAL_PHONE_RE.sub(lambda m: f"{code} {m.group(1)}", line) if "@" in line else line
        for line in text.split("\n")
    )


# Statements about the candidate's immigration / work-authorization status.
# A CV must never carry one: the model has no reliable source for it and has
# invented 'settled status', 'citizen' and 'no sponsorship required' claims.
# The accurate statement lives in .env (WORK_ELIGIBILITY_*) and goes only in
# the application email.
_WORK_STATUS_RE = re.compile(
    r"right[\s-]to[\s-]work|work(ing)?\s+(rights|authori[sz]ation|permit|visa)|"
    r"settled\s+status|pre-settled|indefinite\s+leave|\bILR\b|"
    r"(no|without|not\s+require|not\s+requiring)\s+(visa\s+)?sponsorship|"
    r"sponsorship\s+(is\s+)?(not\s+)?(required|needed|eligible)|"
    r"\b(british|uk|canadian)\s+(citizen|national|passport)|citizenship|"
    r"(graduate|skilled\s+worker|tier\s*2|working\s+holiday)\s+(route|visa)|"
    r"visa\s+(status|holder|type)|immigration\s+status|permanent\s+resid|"
    r"security\s+clearance",
    re.I)


def strip_work_status_claims(text):
    """Remove CV lines that state a right-to-work / visa / citizenship /
    clearance status. Only short lines (a bullet or a one-line field) are
    dropped; a long experience paragraph that merely mentions e.g. 'visa'
    is left alone."""
    out = []
    for line in (text or "").splitlines():
        if len(line) < 220 and _WORK_STATUS_RE.search(line):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
