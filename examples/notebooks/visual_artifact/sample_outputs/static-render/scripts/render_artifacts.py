"""Render static visual-artifact gallery assets."""

# ruff: noqa: INP001

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

EXAMPLE_ROOT = Path(__file__).resolve().parents[3]
STATIC_RENDER_DIR = Path(__file__).resolve().parents[1]
CHROME = Path("/home/vscode/.cache/ms-playwright/chromium-1223/chrome-linux/chrome")

sys.path.insert(0, str(EXAMPLE_ROOT))

from playwright.sync_api import sync_playwright
from shepherd_usecases.visual_artifact.tile import (
    ARTIFACT_PATH,
    DEFAULT_STRATEGIES,
    evaluate_gate,
    load_brief,
    render_static_tile,
)

brief = load_brief()

tiles = {}
for strategy in DEFAULT_STRATEGIES:
    html = render_static_tile(brief.to_payload(), strategy)
    tiles[strategy] = html
    report = evaluate_gate(branch=strategy, html=html, changed_paths=(ARTIFACT_PATH,), brief=brief)
    print(f"{strategy:12} gate direction={report.direction!r} failures={report.failures}")

with TemporaryDirectory(prefix="visual-artifact-gallery-") as tmp:
    tmp_dir = Path(tmp)
    with sync_playwright() as p:
        launch_kwargs = {"args": ["--no-sandbox"]}
        if CHROME.exists():
            launch_kwargs["executable_path"] = str(CHROME)
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 720, "height": 540}, device_scale_factor=2)
        paths = {}
        for strategy, html in tiles.items():
            page.set_content(html)
            page.wait_for_timeout(300)
            out = tmp_dir / f"tile_{strategy}.png"
            page.screenshot(path=out)
            paths[strategy] = out
        browser.close()

    from PIL import Image, ImageDraw

    images = [Image.open(paths[strategy]) for strategy in DEFAULT_STRATEGIES]
    w, h = images[0].size
    labelh = 56
    grid = Image.new("RGB", (w * len(images), h + labelh), "white")
    draw = ImageDraw.Draw(grid)
    notes = {"contour-map": "PASS - selected", "uphill-path": "FAIL direction - discarded"}
    for index, strategy in enumerate(DEFAULT_STRATEGIES):
        x = index * w
        color = (185, 28, 28) if strategy == "uphill-path" else (21, 128, 61)
        draw.rectangle([x, 0, x + w, labelh], fill=color)
        draw.text((x + 12, 18), f"{strategy} - {notes[strategy]}", fill="white")
        grid.paste(images[index], (x, labelh))
    gallery = STATIC_RENDER_DIR / "uc1_gallery.png"
    grid.save(gallery)
    print("wrote", gallery, grid.size)
