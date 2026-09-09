"""Geometry-based bibliography extraction for common conference PDF layouts."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

import pdfplumber

if TYPE_CHECKING:
    from pathlib import Path


class ExtractionError(ValueError):
    """The bibliography cannot be segmented with sufficient confidence."""


@dataclass
class Line:
    text: str
    page: int
    column: int
    x: float
    top: float
    bottom: float
    size: float
    bold: bool


def _lines(words: list[dict], page: int, column: int) -> list[Line]:
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if rows and abs(word["top"] - median(w["top"] for w in rows[-1])) < 3:
            rows[-1].append(word)
        else:
            rows.append([word])
    result = []
    for row in rows:
        row.sort(key=lambda w: w["x0"])
        result.append(
            Line(
                " ".join(w["text"] for w in row),
                page,
                column,
                min(w["x0"] for w in row),
                min(w["top"] for w in row),
                max(w["bottom"] for w in row),
                median(w["size"] for w in row),
                sum(len(w["text"]) for w in row if any(s in w["fontname"].lower() for s in ["bold", "-b", "cmbx"]))
                > sum(len(w["text"]) for w in row) / 2,
            )
        )
    return result


def read_lines(pdf: Path) -> tuple[list[Line], list[dict]]:
    """Read columns independently, preserving coordinates and PDF page numbers."""
    ordered: list[Line] = []
    pages = []
    with pdfplumber.open(pdf, unicode_norm="NFKC") as document:
        for number, page in enumerate(document.pages, 1):
            words = page.extract_words(extra_attrs=["fontname", "size"], x_tolerance=2, y_tolerance=3)
            words = [w for w in words if 35 < w["top"] < page.height - 35 and w["text"].strip()]
            # A two-column page has a consistently empty central gutter. A few
            # full-width headings may cross it; ordinary single-column prose does not.
            middle = page.width / 2
            crossing = [w for w in words if w["x0"] < middle < w["x1"]]
            left = [w for w in words if w["x1"] < middle - 4]
            right = [w for w in words if w["x0"] > middle + 4]
            right_starts = sum(abs(w["x0"] - (middle + 9)) < 15 for w in right)
            two_columns = len(left) > 25 and len(right) > 25 and len(crossing) <= 3 and right_starts >= 8
            columns = (
                [[w for w in words if w["x0"] < middle], [w for w in words if w["x0"] >= middle]]
                if two_columns
                else [words]
            )
            lines = [line for col, items in enumerate(columns) for line in _lines(items, number, col)]
            # Page numbers and conference running heads are not bibliography text.
            lines = [
                line
                for line in lines
                if not re.fullmatch(r"\d+", line.text)
                and not re.match(
                    r"Published as a conference paper|Under review as a conference paper", line.text, re.IGNORECASE
                )
            ]
            ordered.extend(lines)
            pages.append({"page": number, "columns": len(columns), "text": "\n".join(line.text for line in lines)})
            page.close()
    if not ordered:
        raise ExtractionError("No extractable PDF text; scanned PDFs require OCR, which is not supported")
    return ordered, pages


def _heading(text: str) -> bool:
    # Small caps can be split into separate words by font-size changes.
    return bool(
        re.fullmatch(
            r"(?:\d+\.?)?(?:references|bibliography)(?:\(continued\))?", re.sub(r"\s+", "", text), re.IGNORECASE
        )
    )


def _end(line: Line, body_size: float) -> bool:
    if re.match(
        r"^(?:appendix|appendices|supplementary material|neurips(?: \d{4})? (?:paper )?checklist)\b",
        line.text,
        re.IGNORECASE,
    ):
        return True
    section = re.match(r"^(?:[A-Z](?:\.\d+)*[.\s]+|\d+[.\s]+)\S", line.text)
    return bool(section and (line.bold or line.size > body_size + 0.6))


def extract_references(pdf: Path) -> tuple[list[dict], list[dict], dict]:
    """Extract numbered or hanging-indent references; stop at appendix or EOF."""
    lines, pages = read_lines(pdf)
    starts = [i for i, line in enumerate(lines) if _heading(line.text)]
    if not starts:
        raise ExtractionError("No References/Bibliography heading found")
    start = starts[0] + 1
    candidates = lines[start:]
    if not candidates:
        raise ExtractionError("Bibliography heading has no entries")
    body_size = median(line.size for line in candidates[:25])
    bibliography = []
    ended = "end_of_pdf"
    for line in candidates:
        if _heading(line.text):
            continue
        if _end(line, body_size):
            ended = "section_heading"
            break
        bibliography.append(line)
    if not bibliography:
        raise ExtractionError("Empty bibliography")

    numbered = bool(re.match(r"^(?:\[\d+\]|\d+\.)\s+", bibliography[0].text))
    marker = r"^\[(\d+)\]\s+(.*)" if bibliography[0].text.startswith("[") else r"^(\d+)\.\s+(.*)"
    refs: list[dict] = []
    # Per-column anchors also work when a reference continues across a column
    # or page break. The small tolerance absorbs glyph-position rounding.
    anchors = {}
    for line in bibliography:
        key = (line.page, line.column)
        anchors[key] = min(anchors.get(key, line.x), line.x)
    indented = any(line.x > anchors[(line.page, line.column)] + 4 for line in bibliography)
    if not numbered and not indented:
        raise ExtractionError("Unnumbered bibliography has no detectable hanging indentation; review segmentation")

    # A continuation-only page/column has no true entry-start anchor of its
    # own. Reuse the leftmost anchor for that column on every line, not just
    # the first line after the break, or a trailing URL becomes a new entry.
    column_anchors = {column: min(value for (_, c), value in anchors.items() if c == column) for _, column in anchors}
    for line in bibliography:
        match = re.match(marker, line.text) if numbered else None
        begins = bool(match) if numbered else line.x <= column_anchors[line.column] + 2
        if begins:
            refs.append(
                {
                    "id": match[1] if match else str(len(refs) + 1),
                    "raw": match[2] if match else line.text,
                    "pages": [line.page],
                }
            )
        elif refs:
            refs[-1]["raw"] += "\n" + line.text
            if line.page not in refs[-1]["pages"]:
                refs[-1]["pages"].append(line.page)
        else:
            raise ExtractionError("Bibliography starts with an unassigned continuation")
    if numbered and [int(r["id"]) for r in refs] != list(range(1, len(refs) + 1)):
        raise ExtractionError("Numbered bibliography contains gaps or duplicate labels")
    # Broken column order or excessive splitting generally produces fragments
    # with no year. Fail visibly instead of silently auditing an incomplete list.
    bad = [r["id"] for r in refs if not re.search(r"\b(?:18|19|20)\d{2}[a-z]?\b|https?\s*:", r["raw"])]
    if bad:
        raise ExtractionError(f"Reference fragments lack a year or URL: {', '.join(bad)}")
    return (
        refs,
        pages,
        {
            "method": "pdfplumber-columns-and-indentation",
            "style": "numbered" if numbered else "author_year",
            "boundary": ended,
            "columns": dict(Counter(p["columns"] for p in pages)),
            "warnings": [],
        },
    )
