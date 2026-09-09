"""Synthetic PDF layout and continuation regressions; no model or network."""

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from shepherd_citation_checker._engine.extraction import ExtractionError, extract_references


def make_pdf(path: Path, *, columns: int = 1, numbered: bool = False, appendix: bool = False) -> Path:
    """Generate original fixture content in conference-like geometry."""
    doc = canvas.Canvas(str(path), pagesize=(612, 792))
    for col in range(columns):
        x = 55 if col == 0 and columns == 2 else 108 if columns == 1 else 315
        y = 710
        if col == 0:
            doc.setFont("Times-Bold", 12)
            doc.drawString(x, y, "R EFERENCES" if columns == 1 else "References")
            y -= 24
        doc.setFont("Times-Roman", 10)
        for i in range(3):
            n = col * 3 + i + 1
            prefix = f"[{n}] " if numbered else ""
            doc.drawString(x, y, f"{prefix}Author{n}, Alice and Bob Writer. 2024.")
            doc.drawString(x + 11, y - 12, f"A Version Control Study Number {n}.")
            doc.drawString(x + 11, y - 24, "Proceedings of the Example Conference.")
            doc.drawString(x + 11, y - 36, "A careful study of methods and outcomes.")
            y -= 55
        if appendix and col == columns - 1:
            doc.setFont("Times-Bold", 12)
            doc.drawString(x, y, "A Additional Experiments")
            doc.setFont("Times-Roman", 10)
            doc.drawString(x, y - 15, "This appendix must not enter the last reference.")
    doc.setFont("Times-Roman", 10)
    doc.drawString(300, 22, "1")
    doc.save()
    return path


@pytest.mark.parametrize(
    ("venue", "columns", "numbered", "appendix"),
    [
        ("ACL", 2, False, True),
        ("ICML", 2, False, False),
        ("ICLR", 1, False, True),
        ("NeurIPS", 1, True, False),
    ],
)
def test_conference_layouts(tmp_path, venue, columns, numbered, appendix):
    pdf = make_pdf(tmp_path / f"{venue}.pdf", columns=columns, numbered=numbered, appendix=appendix)
    refs, pages, info = extract_references(pdf)
    assert len(refs) == columns * 3
    assert pages[0]["columns"] == columns
    assert [r["id"] for r in refs] == [str(i) for i in range(1, len(refs) + 1)]
    assert all(f"Study Number {r['id']}" in r["raw"] for r in refs)
    assert all("appendix" not in r["raw"] for r in refs)
    assert info["boundary"] == ("section_heading" if appendix else "end_of_pdf")


def test_unnumbered_cross_page_continuation(tmp_path):
    path = tmp_path / "cross.pdf"
    doc = canvas.Canvas(str(path))
    doc.setFont("Times-Bold", 12)
    doc.drawString(72, 760, "References")
    doc.setFont("Times-Roman", 10)
    doc.drawString(72, 735, "Alpha, Alice. 2024. A long paper.")
    doc.drawString(84, 723, "The reference continues onto another page")
    doc.showPage()
    doc.setFont("Times-Roman", 10)
    doc.drawString(84, 760, "with its publisher and page numbers.")
    doc.drawString(72, 735, "Beta, Bob. 2023. Another paper.")
    doc.drawString(84, 723, "Proceedings of Example Conference.")
    doc.save()
    refs, _, _ = extract_references(path)
    assert len(refs) == 2
    assert refs[0]["pages"] == [1, 2]
    assert "publisher" in refs[0]["raw"]


def test_scan_fails_explicitly(tmp_path):
    path = tmp_path / "blank.pdf"
    doc = canvas.Canvas(str(path))
    doc.rect(20, 20, 100, 100)
    doc.showPage()
    doc.save()
    with pytest.raises(ExtractionError, match="OCR"):
        extract_references(path)


def test_continuation_only_last_page_does_not_invent_url_reference(tmp_path):
    path = tmp_path / "tail.pdf"
    doc = canvas.Canvas(str(path))
    doc.setFont("Times-Bold", 12)
    doc.drawString(72, 760, "References")
    doc.setFont("Times-Roman", 10)
    doc.drawString(72, 735, "Alice Author. 2024. A long paper.")
    doc.drawString(84, 723, "Proceedings of Example Conference.")
    doc.showPage()
    doc.setFont("Times-Roman", 10)
    doc.drawString(84, 760, "Published by Example Press.")
    doc.drawString(84, 748, "https://example.org/paper")
    doc.save()
    refs, _, _ = extract_references(path)
    assert len(refs) == 1
    assert refs[0]["pages"] == [1, 2]
    assert refs[0]["raw"].endswith("https://example.org/paper")


def test_numbered_year_continuation_and_gaps(tmp_path):
    def document(name, second_label):
        path = tmp_path / name
        doc = canvas.Canvas(str(path))
        doc.setFont("Times-Bold", 12)
        doc.drawString(72, 760, "References")
        doc.setFont("Times-Roman", 10)
        doc.drawString(72, 735, "[1] Alpha, Alice. A long paper.")
        doc.drawString(84, 723, "2025. Proceedings of Example Conference.")
        doc.drawString(72, 700, f"[{second_label}] Beta, Bob. 2023. Another paper.")
        doc.save()
        return path

    refs, _, _ = extract_references(document("years.pdf", 2))
    assert len(refs) == 2
    assert "2025." in refs[0]["raw"]
    with pytest.raises(ExtractionError, match="gaps"):
        extract_references(document("gap.pdf", 3))


def test_bibliography_starts_in_right_column(tmp_path):
    path = tmp_path / "right.pdf"
    doc = canvas.Canvas(str(path), pagesize=(612, 792))
    doc.setFont("Times-Roman", 10)
    for i in range(22):
        doc.drawString(55, 710 - i * 12, "This is the body of the paper before references.")
    doc.setFont("Times-Bold", 12)
    doc.drawString(315, 710, "References")
    doc.setFont("Times-Roman", 10)
    for i in range(3):
        y = 686 - i * 60
        doc.drawString(315, y, f"Author{i}, Alice. 2024. A paper.")
        doc.drawString(326, y - 12, "Proceedings of Example Conference.")
        doc.drawString(326, y - 24, "A study of methods and outcomes.")
        doc.drawString(326, y - 36, "Published by Example Press.")
    doc.save()
    refs, pages, _ = extract_references(path)
    assert pages[0]["columns"] == 2
    assert len(refs) == 3
    assert all("body of the paper" not in ref["raw"] for ref in refs)
