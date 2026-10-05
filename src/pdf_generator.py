"""Markdown CV / cover letter -> clean, professionally typeset PDF.

Lays the document out directly with fpdf2 (instead of its HTML renderer) so
bullets, spacing and fonts are fully controlled: bullets sit on the first
line of their text, wrapped lines hang-indent under the text, sections get a
small-caps heading with a rule, and a short skills list flows into two
columns. Understands the markdown the generator produces:

    # Name                      -> centred name
    first line after the name   -> centred contact line
    ## Section                  -> section heading + rule
    ### Role - Company          -> role line
    *Jan 2024 to Present*       -> muted italic date line
    * bullet / - bullet         -> bullet
    plain text                  -> paragraph
"""
import os
import re

from fpdf import FPDF

from logger import get_logger

logger = get_logger()

# Calibri (Windows) reads well and supports curly quotes / dashes; elsewhere
# fall back to the built-in Helvetica with a latin-1 text cleanup.
FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
TTF_FAMILY = {
    "": "calibri.ttf", "B": "calibrib.ttf", "I": "calibrii.ttf", "BI": "calibriz.ttf",
}

MARGIN = 18             # mm, all sides
ACCENT = (31, 58, 96)   # deep navy: name, headings, rules, bullets
TEXT = (33, 37, 41)
MUTED = (100, 108, 118)

# Point sizes; Helvetica runs larger than Calibri, so it is scaled down a touch
SIZES = {"name": 21, "contact": 9.5, "h2": 11.5, "h3": 10.8, "meta": 9.5, "body": 10.3}
HELVETICA_SCALE = 0.9

TWO_COLUMN_MIN_ITEMS = 6   # a list this long of short items flows into 2 columns
TWO_COLUMN_MAX_CHARS = 72


class _Doc(FPDF):
    def __init__(self):
        super().__init__(format="A4")
        self.set_margins(MARGIN, MARGIN, MARGIN)
        self.set_auto_page_break(auto=True, margin=MARGIN)
        self.family = self._load_font()
        self.scale = 1.0 if self.family == "Body" else HELVETICA_SCALE

    def _load_font(self):
        paths = {style: os.path.join(FONT_DIR, name) for style, name in TTF_FAMILY.items()}
        if all(os.path.exists(p) for p in paths.values()):
            for style, path in paths.items():
                self.add_font("Body", style, path)
            return "Body"
        return "Helvetica"

    def font(self, size_key, style="", color=TEXT):
        self.set_font(self.family, style, SIZES[size_key] * self.scale)
        self.set_text_color(*color)



# --- markdown parsing -------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_META_RE = re.compile(r"^(\*|_)(?!\*)(.+?)(?<!\*)\1$")   # whole line *italic*
_BULLET_RE = re.compile(r"^(\s*)[*\-+•]\s+(.*)")


def _inline(text):
    """Strip inline markdown (bold, italics, links) to plain text."""
    text = _LINK_RE.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    # Hyphen variants Calibri has no glyph for (they'd render as blanks)
    for odd in ("‐", "‑", "­"):
        text = text.replace(odd, "-" if odd != "­" else "")
    text = re.sub(r"(?<![\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])", r"\1", text)
    return text.replace("\\", "").strip()


def _blocks(md_text):
    """Yield (kind, text) blocks; consecutive bullets are grouped as a list."""
    md_text = re.sub(r"```(?:markdown)?", "", md_text).strip()
    blocks, para, items = [], [], []

    def flush():
        if para:
            blocks.append(("p", _inline(" ".join(para))))
            para.clear()
        if items:
            blocks.append(("list", list(items)))
            items.clear()

    for raw in md_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or re.fullmatch(r"[-*_]{3,}", stripped):
            flush()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)", stripped)
        bullet = _BULLET_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            kind = {1: "h1", 2: "h2"}.get(level, "h3")
            blocks.append((kind, _inline(heading.group(2))))
        elif bullet:
            if para:
                blocks.append(("p", _inline(" ".join(para))))
                para.clear()
            items.append(_inline(bullet.group(2)))
        elif _META_RE.match(stripped) and len(stripped) < 90:
            flush()
            blocks.append(("meta", _inline(stripped)))
        elif items and raw.startswith((" ", "\t")):
            items[-1] += " " + _inline(stripped)   # wrapped bullet continuation
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return blocks


def _clean_for_core_font(text):
    swaps = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2014": "-",
             "\u2013": "-", "\u2022": "-", "\u2026": "...", "\u00a0": " "}
    for k, v in swaps.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "ignore").decode("latin-1")


# --- layout -----------------------------------------------------------------

def _line_h(pdf, factor=1.24):
    return pdf.font_size * factor


def _keep_room(pdf, needed_mm):
    """Start a new page rather than strand a heading at the bottom."""
    if pdf.get_y() + needed_mm > pdf.h - MARGIN:
        pdf.add_page()


def _bullet(pdf, x, text, width):
    """One bullet: dot vertically centred on the first line, text hang-indented."""
    lh = _line_h(pdf)
    lines = pdf.multi_cell(width - 4.5, lh, text, dry_run=True, output="LINES")
    if pdf.get_y() + lh * len(lines) > pdf.h - MARGIN:
        pdf.add_page()
    y = pdf.get_y()
    radius = 0.55
    pdf.set_fill_color(*ACCENT)
    pdf.ellipse(x + 1.0, y + lh / 2 - radius, radius * 2, radius * 2, style="F")
    pdf.set_xy(x + 4.5, y)
    pdf.multi_cell(width - 4.5, lh, text, align="L", new_x="LMARGIN", new_y="NEXT")


def _render_list(pdf, items, two_columns):
    width = pdf.epw
    if not two_columns:
        for item in items:
            _bullet(pdf, pdf.l_margin, item, width)
            pdf.ln(0.7)
        return
    # Two independent columns (each flows on its own, so a wrapped item never
    # leaves a gap beside it). Falls back to one column if it won't fit the page.
    col_w = (width - 6) / 2
    lh = _line_h(pdf)

    def height(column):
        return sum(len(pdf.multi_cell(col_w - 4.5, lh, t, dry_run=True, output="LINES")) * lh + 0.5
                   for t in column)

    half = (len(items) + 1) // 2
    columns = [items[:half], items[half:]]
    if pdf.get_y() + max(height(c) for c in columns) > pdf.h - MARGIN:
        _render_list(pdf, items, two_columns=False)
        return
    top, bottom = pdf.get_y(), pdf.get_y()
    for i, column in enumerate(columns):
        pdf.set_y(top)
        for item in column:
            _bullet(pdf, pdf.l_margin + i * (col_w + 6), item, col_w)
            pdf.ln(0.5)
        bottom = max(bottom, pdf.get_y())
    pdf.set_y(bottom)


def convert_markdown_to_pdf(md_text, output_pdf_path):
    """Render a markdown CV or cover letter to a typeset A4 PDF."""
    pdf = _Doc()
    blocks = _blocks(md_text)
    if pdf.family == "Helvetica":
        blocks = [(k, [_clean_for_core_font(i) for i in t] if k == "list"
                   else _clean_for_core_font(t)) for k, t in blocks]
    pdf.add_page()

    after_name = False
    prev = None
    for kind, text in blocks:
        if kind == "h1":
            pdf.font("name", "B", ACCENT)
            pdf.cell(0, _line_h(pdf, 1.15), text, align="C", new_x="LMARGIN", new_y="NEXT")
            after_name = True
        elif after_name and kind == "p":
            pdf.font("contact", color=MUTED)
            pdf.ln(0.8)
            pdf.multi_cell(0, _line_h(pdf), text, align="C", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
            after_name = False
        elif kind == "h2":
            after_name = False
            _keep_room(pdf, 22)
            pdf.ln(2.6 if prev else 0)
            pdf.font("h2", "B", ACCENT)
            pdf.set_char_spacing(0.6)
            pdf.cell(0, _line_h(pdf, 1.2), text.upper(), new_x="LMARGIN", new_y="NEXT")
            pdf.set_char_spacing(0)
            pdf.set_draw_color(*ACCENT)
            pdf.set_line_width(0.35)
            y = pdf.get_y() + 0.4
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
            pdf.ln(2.0)
        elif kind == "h3":
            after_name = False
            _keep_room(pdf, 18)
            pdf.ln(1.6 if prev not in ("h2", None) else 0)
            pdf.font("h3", "B")
            pdf.multi_cell(0, _line_h(pdf, 1.25), text, align="L", new_x="LMARGIN", new_y="NEXT")
        elif kind == "meta":
            pdf.font("meta", "I", MUTED)
            pdf.cell(0, _line_h(pdf, 1.25), text, new_x="LMARGIN", new_y="NEXT")
            pdf.ln(0.8)
        elif kind == "list":
            after_name = False
            pdf.font("body")
            short = (len(text) >= TWO_COLUMN_MIN_ITEMS
                     and max(len(i) for i in text) <= TWO_COLUMN_MAX_CHARS)
            _render_list(pdf, text, short)
            pdf.ln(0.8)
        else:  # paragraph
            after_name = False
            pdf.font("body")
            pdf.multi_cell(0, _line_h(pdf, 1.3), text, align="L", new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.8)
        prev = kind

    try:
        pdf.output(output_pdf_path)
        logger.info(f"Successfully generated PDF: {output_pdf_path}")
    except Exception as e:
        logger.error(f"Failed to generate PDF for {output_pdf_path}: {e}")
        raise
