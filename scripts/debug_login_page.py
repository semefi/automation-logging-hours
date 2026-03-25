#!/usr/bin/env python3
"""Debug: click Google login and see what happens next."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "/app/playwright-profile",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://erp.developers.net/", wait_until="domcontentloaded")
    time.sleep(2)
    print("1) URL after nav:", page.url)

    # Click Google button
    btn = page.locator("button:has-text('Google')")
    if btn.count() > 0:
        btn.first.click(timeout=5000)
        print("2) Clicked Google button")
    else:
        print("2) No Google button found")

    # Wait and check where we end up
    time.sleep(5)
    print("3) URL after click:", page.url)

    # Check all pages/tabs
    for i, p in enumerate(ctx.pages):
        print(f"4) Tab {i}: {p.url}")

    # If on Google page, look for account selector
    current = page if "google" in page.url else None
    for p in ctx.pages:
        if "google" in p.url:
            current = p
            break

    if current and "google" in current.url:
        print("5) On Google page. HTML snippet:")
        print(current.content()[:3000])
    else:
        print("5) Not on Google page. Current page HTML snippet:")
        print(page.content()[:3000])

    ctx.close()
