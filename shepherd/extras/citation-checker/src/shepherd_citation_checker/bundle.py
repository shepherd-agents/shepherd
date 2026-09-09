"""Freeze installed parser dependencies for the isolated reviewer workspace."""

import importlib.metadata
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def require_dependencies() -> None:
    """Explain how to install the optional parsers before creating a run."""
    for name in ("pdfplumber", "beautifulsoup4"):
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError(
                'Citation checking requires the parser extra: pip install "shepherd-ai[citation-checker]"'
            ) from exc


def build(path) -> Any:
    """Build the retained output artifact."""
    versions, seen = {}, set()
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name in (
            "beautifulsoup4",
            "soupsieve",
            "typing_extensions",
            "pdfplumber",
            "pdfminer.six",
            "Pillow",
            "pypdfium2",
            "cryptography",
            "cffi",
            "pycparser",
            "charset-normalizer",
        ):
            dist = importlib.metadata.distribution(name)
            versions[name] = dist.version
            for entry in dist.files or []:
                relative = str(entry)
                if (
                    relative.startswith("../")
                    or relative.endswith(".pyc")
                    or "__pycache__" in relative
                    or relative in seen
                ):
                    continue
                source = dist.locate_file(entry)
                if source.is_file():
                    info = ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
                    info.compress_type = ZIP_DEFLATED
                    archive.writestr(info, source.read_bytes())
                    seen.add(relative)
    return versions
