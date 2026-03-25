#!/usr/bin/env python3
"""Debug: click the Google Sign-In iframe button."""
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
    print("1) URL:", page.url)

    # Find the Google Sign-In iframe
    gsi_frame = None
    for f in page.frames:
        if "accounts.google.com/gsi" in f.url:
            gsi_frame = f
            break

    if not gsi_frame:
        print("2) Google Sign-In iframe NOT found")
        ctx.close()
        raise SystemExit(1)

    print("2) Found GSI iframe:", gsi_frame.url[:100])

    # Click inside the iframe (the entire iframe is the button)
    try:
        # The iframe content is a single button/div that's clickable
        gsi_frame.locator("div[role=button]").first.click(timeout=5000)
        print("3) Clicked div[role=button] inside iframe")
    except Exception as e1:
        print(f"3) div[role=button] failed: {e1}")
        try:
            # Fallback: click the iframe element itself from parent
            page.locator("iframe[id^='gsi_']").click(timeout=5000)
            print("3) Clicked iframe element directly")
        except Exception as e2:
            print(f"3) iframe click also failed: {e2}")

    # Wait and see what happens
    time.sleep(8)
    print("4) URL after click:", page.url)
    print(f"5) Total pages/tabs: {len(ctx.pages)}")
    for i, p in enumerate(ctx.pages):
        print(f"   Tab {i}: {p.url}")

    # Check if we got to Google OAuth
    for p in ctx.pages:
        if "accounts.google.com" in p.url and "/gsi/" not in p.url:
            print("6) On Google OAuth page!")
            print("   Content:", p.inner_text("body")[:1000])
            break
    else:
        print("6) Did not reach Google OAuth page")
        print("   Current page text:", page.inner_text("body")[:500])

    ctx.close()
