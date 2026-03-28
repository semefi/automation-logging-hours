#!/usr/bin/env python3
"""Open Google accounts page for manual login. Press Enter when done."""
import os
from playwright.sync_api import sync_playwright

display = os.environ.get("DISPLAY", ":99")
user_data_dir = os.environ.get("USER_DATA_DIR", "/app/playwright-profile")

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir,
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            f"--display={display}",
        ],
        viewport={"width": 1280, "height": 900},
        ignore_default_args=["--headless"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://accounts.google.com/")
    print("En accounts.google.com - haz login en el VNC.")
    print("Presiona Enter aqui cuando termines...")
    input()
    ctx.close()
