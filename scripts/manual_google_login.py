#!/usr/bin/env python3
"""Open Google accounts page for manual login. Press Enter when done."""
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "/app/playwright-profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://accounts.google.com/")
    print("En accounts.google.com - haz login en el VNC.")
    print("Presiona Enter aqui cuando termines...")
    input()
    ctx.close()
