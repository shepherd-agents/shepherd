"""Render artifact HTML to PNG with headless chromium (optional dependency).

Produces preview screenshots used by galleries/filmstrips so a reviewer can see
at a glance which tile is clean and which sends the update path uphill. This is a
display helper; the mechanical gates in ``tile`` do not depend on it.

Requires playwright + chromium, which are NOT workspace dependencies. Install on
demand:

    uv run --with playwright python -m playwright install chromium

Then render a directory of ``<id>/index.html`` exports, or a single file:

    uv run --with playwright python examples/notebooks/visual_artifact/render.py /tmp/out
"""

# ruff: noqa: D103

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except Exception:
        return False
    return True


def render_file(html_path: Path, png_path: Path | None = None, *, width: int = 1280, height: int = 720, scale: int = 2) -> Path:
    """Screenshot one HTML file. Defaults to a sibling ``preview.png``."""
    from playwright.sync_api import sync_playwright

    html_path = Path(html_path)
    png_path = Path(png_path) if png_path else html_path.with_name("preview.png")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale)
        page.goto(html_path.resolve().as_uri())
        page.wait_for_timeout(250)
        page.screenshot(path=str(png_path), full_page=True)
        browser.close()
    return png_path


def render_dir(base: Path, *, artifact: str = "index.html") -> list[Path]:
    """Screenshot every ``<sub>/<artifact>`` under ``base`` to ``<sub>/preview.png``."""
    base = Path(base)
    out: list[Path] = []
    for sub in sorted(p for p in base.iterdir() if p.is_dir()):
        html = sub / artifact
        if html.exists():
            out.append(render_file(html))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="a directory of <id>/index.html exports, or a single .html file")
    parser.add_argument("--artifact", default="index.html")
    args = parser.parse_args(argv)

    if not available():
        print(
            "playwright not installed. Run:\n"
            "  uv run --with playwright python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    if args.path.is_file():
        print("rendered", render_file(args.path))
    else:
        rendered = render_dir(args.path, artifact=args.artifact)
        for png in rendered:
            print("rendered", png)
        if not rendered:
            print(f"no {args.artifact} found under {args.path}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
