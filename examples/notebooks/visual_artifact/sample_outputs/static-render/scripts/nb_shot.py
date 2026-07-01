"""Render executed notebook HTML exports to PNG screenshots and PDFs."""

# ruff: noqa: INP001

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

CHROME = Path("/home/vscode/.cache/ms-playwright/chromium-1223/chrome-linux/chrome")
files = sys.argv[1:]
with sync_playwright() as p:
    launch_kwargs = {"args": ["--no-sandbox"]}
    if CHROME.exists():
        launch_kwargs["executable_path"] = str(CHROME)
    b = p.chromium.launch(**launch_kwargs)
    pg = b.new_page(viewport={"width": 1200, "height": 1000}, device_scale_factor=1)
    for f in files:
        html = Path(f)
        pg.goto(html.resolve().as_uri())
        pg.wait_for_timeout(2000)
        png = html.with_suffix(".png")
        pdf = html.with_suffix(".pdf")
        pg.screenshot(path=png, full_page=True)
        pg.pdf(path=pdf, format="Letter", print_background=True)
        print("wrote", png)
        print("wrote", pdf)
    b.close()
