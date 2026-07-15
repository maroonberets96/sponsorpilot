"""First-run setup for non-technical users.

Two jobs, both aimed at someone who has never touched a terminal before:

1. Make sure the folders a run needs actually exist. git can't track an
   empty folder, so ``data/input/`` (where the user drops their CV) is absent
   on a fresh clone — creating it here avoids a confusing "file not found".
2. If there's no ``.env`` yet, walk the user through building one by asking a
   few plain-English questions, instead of making them edit a file by hand.

Stdlib only, so this can run before anything heavier is imported.
"""
import os

import config
from logger import get_logger

logger = get_logger()

ENV_PATH = os.path.join(config.BASE_DIR, ".env")


def ensure_ready():
    """Return True if the tool has everything it needs to run.

    On a genuine first run (no ``.env``) this walks the user through setup and
    returns False, because they'll still need to drop their CV into
    ``data/input/`` and start the tool again. Returns False (with guidance)
    if the CV is missing too. Existing users with a ``.env`` and a CV are
    unaffected — this just returns True.
    """
    _ensure_dirs()

    if not os.path.exists(ENV_PATH):
        return _run_wizard()

    if not os.path.exists(config.CV_DOCX_PATH):
        _print_cv_missing()
        return False

    return True


def _ensure_dirs():
    """Create the data folders a run writes into. Safe to call every time."""
    os.makedirs(config.INPUT_DIR, exist_ok=True)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)


def _print_cv_missing():
    print(
        "\n" + "=" * 64 + "\n"
        "  Your CV wasn't found, so there's nothing to tailor yet.\n\n"
        f"  Put your CV (a Word .docx file) here:\n"
        f"    {config.INPUT_DIR}\n\n"
        f"  It should be named:  {os.path.basename(config.CV_DOCX_PATH)}\n"
        "  (or change CV_FILENAME in your .env to match your file's name)\n\n"
        "  Then run the tool again.\n"
        + "=" * 64 + "\n"
    )


# --- Setup wizard ---

def _ask(prompt, default=""):
    """Prompt the user; return their answer or the default on blank/EOF."""
    try:
        answer = input(prompt).strip()
    except EOFError:
        raise _NoConsole()
    return answer or default


class _NoConsole(Exception):
    """Raised when there's no interactive console to run the wizard in."""


def _run_wizard():
    """Interactively build a .env, or explain the manual path. Returns False
    (setup done, but the user still needs to add their CV and rerun)."""
    print(
        "\n" + "=" * 64 + "\n"
        "  Welcome! Let's set up the Job Application Assistant.\n\n"
        "  This is a one-time setup. You'll paste in a few free API keys\n"
        "  (links below). Press Enter to skip any you don't have yet — you\n"
        "  can always add them later by editing the .env file.\n\n"
        "  You need at least ONE job board and ONE AI provider for the tool\n"
        "  to do anything useful.\n"
        + "=" * 64
    )

    try:
        values = _collect_values()
    except _NoConsole:
        print(
            "\nNo interactive console available, so I can't run setup here.\n"
            "Copy .env.example to .env and fill in your keys by hand:\n"
            f"    {ENV_PATH}\n"
        )
        return False

    _write_env(values)

    print(
        "\n" + "=" * 64 + "\n"
        f"  Saved your settings to:\n    {ENV_PATH}\n\n"
        "  Last step: put your CV (a Word .docx file) here:\n"
        f"    {config.INPUT_DIR}\n"
        f"  named  {values['cv_filename']}\n"
    )
    if values.get("sponsor_xlsx"):
        print(
            "  For UK jobs, also drop the sponsor-licence .xlsx from gov.uk in\n"
            "  that same folder (see the README for the download link), named\n"
            f"    {values['sponsor_xlsx']}\n"
        )
    print(
        "  Then start the tool again to run your first search.\n"
        + "=" * 64 + "\n"
    )
    return False


def _collect_values():
    print("\n-- Your CV --")
    cv_filename = _ask(
        "  Filename of your CV .docx [cv.docx]: ", "cv.docx"
    )

    print("\n-- Job boards (you need at least one) --")
    print("  Adzuna works for BOTH the UK and Canada. Get a free key at:")
    print("    https://developer.adzuna.com")
    adzuna_id = _ask("  Adzuna App ID (Enter to skip): ")
    adzuna_key = _ask("  Adzuna App Key (Enter to skip): ") if adzuna_id else ""
    print("  Reed (UK only) — free key at https://www.reed.co.uk/developers")
    reed_key = _ask("  Reed API Key (Enter to skip): ")
    print("  Jooble (Canada) — free key at https://jooble.org/api/about")
    jooble_key = _ask("  Jooble API Key (Enter to skip): ")

    print("\n-- AI provider (you need at least one) --")
    print("  NVIDIA NIM (primary) — free key at https://build.nvidia.com")
    nvidia_key = _ask("  NVIDIA API Key (Enter to skip): ")
    print("  Groq (fallback) — free key at https://console.groq.com")
    groq_key = _ask("  Groq API Key (Enter to skip): ")

    print("\n-- UK sponsor register (skip if you won't search the UK) --")
    print("  The 'Worker and Temporary Worker' .xlsx from gov.uk.")
    sponsor_xlsx = _ask(
        "  Its filename [Worker_and_Temporary_Worker.xlsx, Enter to skip]: "
    )

    values = {
        "cv_filename": cv_filename,
        "adzuna_id": adzuna_id,
        "adzuna_key": adzuna_key,
        "reed_key": reed_key,
        "jooble_key": jooble_key,
        "nvidia_key": nvidia_key,
        "groq_key": groq_key,
        "sponsor_xlsx": sponsor_xlsx,
    }
    _warn_if_incomplete(values)
    return values


def _warn_if_incomplete(values):
    if not (values["adzuna_id"] or values["reed_key"] or values["jooble_key"]):
        print(
            "\n  ! Heads up: you didn't add any job board keys, so no vacancies\n"
            "    will be fetched. Add at least one later in your .env file."
        )
    if not (values["nvidia_key"] or values["groq_key"]):
        print(
            "\n  ! Heads up: you didn't add any AI provider keys, so the tool\n"
            "    can't score jobs or write documents. Add at least one later\n"
            "    in your .env file. (Ollama Cloud is a third option — see the\n"
            "    .env comments.)"
        )


def _write_env(v):
    """Write a .env mirroring .env.example, with the user's answers filled in
    and placeholders left for anything they skipped."""
    lines = [
        "# Created by the first-run setup wizard. Edit any value by hand.",
        "",
        "# --- Input files (in data/input/) ---",
        f"CV_FILENAME={v['cv_filename']}",
        f"SPONSOR_XLSX_FILENAME={v['sponsor_xlsx'] or 'Worker_and_Temporary_Worker.xlsx'}",
        "",
        "# --- Job boards (at least one required) ---",
        f"ADZUNA_APP_ID={v['adzuna_id'] or 'your_adzuna_app_id'}",
        f"ADZUNA_APP_KEY={v['adzuna_key'] or 'your_adzuna_app_key'}",
        f"REED_API_KEY={v['reed_key'] or 'your_reed_api_key'}",
        f"JOOBLE_API_KEY={v['jooble_key'] or 'your_jooble_api_key'}",
        "",
        "# --- AI providers (waterfall: NVIDIA -> Groq -> Ollama Cloud) ---",
        f"NVIDIA_API_KEY={v['nvidia_key'] or 'your_nvidia_api_key_here'}",
        f"GROQ_API_KEY={v['groq_key'] or 'your_groq_api_key_here'}",
        "# Ollama Cloud (optional 3rd fallback) — run `ollama signin` first.",
        "OLLAMA_CLOUD_MODEL=gpt-oss:120b-cloud",
        "",
    ]
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
