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
