#!/usr/bin/env python3
"""Debug: find the exact clickable Google element."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "/app/playwright-profile",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://erp.developers.net/", wait_until="networkidle")
    time.sleep(5)

    # Get the full HTML of the google-button-container
    html = page.locator(".google-button-container").inner_html()
    print("--- GOOGLE BUTTON CONTAINER HTML ---")
    print(html)

    # Also check for iframes (Google Sign-In often uses an iframe)
    iframes = page.frames
    print(f"\n--- FRAMES ({len(iframes)}) ---")
    for f in iframes:
        print(f"  frame: name='{f.name}' url='{f.url}'")

    ctx.close()
