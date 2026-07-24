"""Verify a pygbag build actually runs, without a human looking at it.

    pip install playwright          # uses your installed Chrome, no download
    py web/probe.py http://127.0.0.1:8080/rbmk-sim/ [seconds]

Reports every document.title change (the app sets a caption once the display
exists, and the build writes "PYERR <traceback>" there if Python raises),
saves a screenshot, and prints the console tail.

Why title-watching: pygbag routes Python stdout to its xterm terminal, which
is drawn on a canvas -- print() output never reaches the JS console, so a
crash looks identical to a hang. document.title is the one channel readable
from outside.
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080/rbmk-sim/"
WAIT = int(sys.argv[2]) if len(sys.argv) > 2 else 75
SHOT = "web/probe.png"

logs: list[str] = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1000, "height": 900})
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text[:300]}"))
    page.on("pageerror", lambda e: logs.append(f"[PAGEERROR] {str(e)[:300]}"))
    page.goto(URL, wait_until="domcontentloaded")

    seen: list[str] = []
    t0 = time.time()
    while time.time() - t0 < WAIT:
        title = page.title()
        if not seen or seen[-1] != title:
            seen.append(title)
            print(f"  t={time.time() - t0:6.1f}s  {title}")
        page.wait_for_timeout(400)
    page.screenshot(path=SHOT)
    browser.close()

print(f"\nscreenshot: {SHOT}")
print("=== console tail ===")
for line in logs[-25:]:
    print(line)

ok = any(t.startswith("RBMK") for t in seen)
bad = any(t.startswith("PYERR") for t in seen)
print("\nRESULT:", "app started" if ok else ("python raised" if bad else "never started"))
sys.exit(0 if ok else 1)
