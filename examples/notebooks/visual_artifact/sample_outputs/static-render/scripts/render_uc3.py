"""Render static recovery before/after assets."""

# ruff: noqa: INP001

import sys
from pathlib import Path

EXAMPLE_ROOT = Path(__file__).resolve().parents[3]
STATIC_RENDER_DIR = Path(__file__).resolve().parents[1]
CHROME = Path("/home/vscode/.cache/ms-playwright/chromium-1223/chrome-linux/chrome")

sys.path.insert(0, str(EXAMPLE_ROOT))

from playwright.sync_api import sync_playwright
from shepherd_usecases.visual_artifact.tile import load_brief, plant_defect, render_static_tile

brief = load_brief()

draft_v1 = plant_defect(render_static_tile(brief.to_payload(), "draft_v1"), "uphill-path", True)
retry = render_static_tile(brief.to_payload(), "draft_retry")

with sync_playwright() as p:
    launch_kwargs = {"args": ["--no-sandbox"]}
    if CHROME.exists():
        launch_kwargs["executable_path"] = str(CHROME)
    browser = p.chromium.launch(**launch_kwargs)
    page = browser.new_page(viewport={"width": 720, "height": 540}, device_scale_factor=2)
    shots = []
    for name, html in [("v1", draft_v1), ("retry", retry)]:
        page.set_content(html)
        page.wait_for_timeout(300)
        out = STATIC_RENDER_DIR / f"uc3_{name}.png"
        page.screenshot(path=out)
        shots.append(out)
    browser.close()

from PIL import Image, ImageDraw

tiles = [Image.open(path) for path in shots]
w, h = tiles[0].size
labelh = 56
canvas = Image.new("RGB", (w * 2, h + labelh), "white")
draw = ImageDraw.Draw(canvas)
labels = [
    ("draft v1 - FAIL direction - discarded", (185, 28, 28)),
    ("draft retry - PASS - selected", (21, 128, 61)),
]
for index, (label, color) in enumerate(labels):
    x = index * w
    draw.rectangle([x, 0, x + w, labelh], fill=color)
    draw.text((x + 12, 18), label, fill="white")
    canvas.paste(tiles[index], (x, labelh))
before_after = STATIC_RENDER_DIR / "uc3_before_after.png"
canvas.save(before_after)
print("wrote", before_after, canvas.size)
