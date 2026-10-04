"""Parsing for Responsa Advanced Search "print results" PDF exports.

Why a PDF at all: the results window in Responsa is a custom-drawn MFC
control with no accessible text API -- confirmed dead ends were
WM_GETTEXT (returns nothing) and UI Automation (only exposes generic
Pane/ScrollBar/TitleBar nodes, no text). Selecting all and copying to the
clipboard *does* work, but only captures whatever page happens to be
scrolled into view at the time. Printing the results window
(Ctrl+P -> "Microsoft Print to PDF") is the only extraction path found
during discovery that reliably captures the entire result set regardless
of scroll position, so `automation.py` prints to a temp PDF and this
module parses it.
"""
import re
from typing import List, Optional

import pdfplumber
from bidi.algorithm import get_display

from ..results import Hit, SearchResults, SourceText

# A hit header line looks like "1. משנה מסכת ברכות פרק ט משנה ב" once put
# back into logical reading order.
_HIT_HEADER_RE = re.compile(r"^(\d+)\.\s+(.+)$")
# The PDF's first line echoes the query, e.g. "החיפוש : *בראשית".
_QUERY_HEADER_RE = re.compile(r"^החיפוש\s*:\s*(.+)$")
# Fixed Bar-Ilan copyright/etiquette line printed at the end of every export.
_FOOTER_MARKER = "קדושת הגליון"
# Page-number line printed at the bottom of every page of a single
# source's full-text export (e.g. "- 13 -") -- confirmed live, not
# present in the multi-hit results-list export, only the single-source
# one parse_source_pdf handles.
_PAGE_NUMBER_RE = re.compile(r"^-\s*\d+\s*-$")


def _extract_display_lines(pdf_path: str) -> List[str]:
    """Extract every line of `pdf_path`'s text, converted from pdfplumber's
    raw *visual* (mirrored) RTL character order into logical reading order
    via python-bidi's get_display(). A naive whole-line character reversal
    would also flip multi-digit numbers (turning "10." into "01."), so the
    proper bidi algorithm is required, not just str[::-1]. Shared by both
    parse_results_pdf and parse_source_pdf.
    """
    raw_lines: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            raw_lines.extend(text.split("\n"))
    return [get_display(line) for line in raw_lines]


def parse_results_pdf(pdf_path: str) -> SearchResults:
    """Parse a Responsa results-export PDF into structured hits."""
    lines = _extract_display_lines(pdf_path)

    query = None
    hits: List[Hit] = []
    current_index: Optional[int] = None
    current_citation: Optional[str] = None
    current_snippet_parts: List[str] = []

    def flush():
        if current_index is not None:
            hits.append(Hit(
                index=current_index,
                citation=current_citation or "",
                snippet=" ".join(current_snippet_parts).strip(),
            ))

    for line in lines:
        stripped = line.strip()
        if not stripped or _FOOTER_MARKER in stripped:
            continue

        if query is None:
            query_match = _QUERY_HEADER_RE.match(stripped)
            if query_match:
                query = query_match.group(1).strip()
                continue

        header_match = _HIT_HEADER_RE.match(stripped)
        if header_match:
            flush()
            current_index = int(header_match.group(1))
            current_citation = header_match.group(2).strip()
            current_snippet_parts = []
            continue

        if current_index is not None:
            current_snippet_parts.append(stripped)

    flush()

    return SearchResults(query=query, hits=hits, raw_text="\n".join(lines))


def parse_source_pdf(pdf_path: str, citation: Optional[str] = None) -> SourceText:
    """Parse a single-source full-text export PDF (from
    ResponsaClient.get_result_text()) into a SourceText.

    Confirmed live against a real 15-page export: the PDF's
    very first line duplicates the source window's own title (already
    captured live as `citation` by the caller, more reliably than
    re-parsing it back out of the PDF) -- dropped from `text` here to
    avoid repeating it. Every page ends with a "- N -" page-number line
    (`_PAGE_NUMBER_RE`), not present in the multi-hit results export.
    """
    lines = _extract_display_lines(pdf_path)

    body_lines: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or _FOOTER_MARKER in stripped or _PAGE_NUMBER_RE.match(stripped):
            continue
        if not body_lines and citation is not None and stripped == citation.strip():
            continue
        body_lines.append(stripped)

    return SourceText(citation=citation, text="\n".join(body_lines), raw_text="\n".join(lines))
