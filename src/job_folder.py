"""Per-job output folder housekeeping: what you see when you open a job folder.

Visible:  APPLY.txt, CV.pdf, CoverLetter.pdf, Mark as applied.bat
Hidden:   CV.md, CoverLetter.md (editable sources), email.json (for the
          Gmail drafter) - kept for the tools, out of the way for you.
"""
import ctypes
import os

HIDDEN_FILES = ("CV.md", "CoverLetter.md", "email.json")
MARK_APPLIED_BAT = "Mark as applied.bat"

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_NORMAL = 0x80


def _set_hidden(path, hidden):
    if os.name != "nt" or not os.path.exists(path):
        return
    attr = _FILE_ATTRIBUTE_HIDDEN if hidden else _FILE_ATTRIBUTE_NORMAL
    ctypes.windll.kernel32.SetFileAttributesW(str(path), attr)


def write_text(path, text):
    """Write a text file, even over a hidden one (Windows refuses to
    overwrite a hidden file opened in plain 'w' mode)."""
    _set_hidden(path, False)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# The .bat climbs from its own folder to the project root (wherever the
# folder has been moved), then calls --mark-applied. The final command is
# one line so cmd has read it all before Python moves the folder away.
_BAT = r"""@echo off
rem Double-click this AFTER you have applied for this job.
rem It marks the job as applied and moves this folder into Applied.
set "ROOT=%~dp0"
:find
if exist "%ROOT%src\main.py" goto run
for %%I in ("%ROOT%..") do set "NEXT=%%~fI\"
if /I "%NEXT%"=="%ROOT%" goto missing
set "ROOT=%NEXT%"
goto find
:missing
echo Could not find the Job Application Assistant folder above this one.
pause
exit /b 1
:run
cd /d "%ROOT%"
"venv\Scripts\python.exe" src\main.py --mark-applied {job_id} & echo. & pause & exit /b
"""


def finalize(job_dir, job_id=None):
    """Hide the tool-only files and add the double-click 'Mark as applied'
    file (only when the job has a database ID to mark)."""
    for name in HIDDEN_FILES:
        _set_hidden(os.path.join(job_dir, name), True)
    if job_id is not None:
        # CRLF line endings: cmd misreads labels in LF-only batch files
        with open(os.path.join(job_dir, MARK_APPLIED_BAT), "w", encoding="ascii", newline="\r\n") as f:
            f.write(_BAT.format(job_id=int(job_id)))
