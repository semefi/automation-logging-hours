#!/usr/bin/env python3
"""Debug: inspect the ERP login page to find the Google sign-in button."""
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
    time.sleep(3)

    print("URL:", page.url)

    buttons = page.query_selector_all(
        "button, a[href*=google], a[href*=oauth], a[href*=login], "
        "[class*=google], [class*=signin], [class*=Google]"
    )
    print("BUTTONS:", [b.text_content().strip() for b in buttons])

    links = page.query_selector_all("a")
    print("ALL_LINKS:", [
        (a.text_content().strip(), a.get_attribute("href"))
        for a in links if a.text_content().strip()
    ])

    # Also dump all clickable elements with "google" or "sign" in text
    all_clickable = page.query_selector_all("button, a, [role=button], input[type=submit]")
    print("ALL_CLICKABLE:", [
        (el.text_content().strip(), el.get_attribute("class") or "")
        for el in all_clickable if el.text_content().strip()
    ])

    ctx.close()
