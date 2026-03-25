#!/usr/bin/env python3
"""Login to Google account first, then to ERP. Run with HEADLESS=false."""
import time
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "/app/playwright-profile",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1440, "height": 960},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # Step 1: Go to Google accounts to establish Google session cookies
    print("Step 1: Navigating to Google accounts...")
    print("        Please sign in to your Google account if prompted.")
    page.goto("https://accounts.google.com/", wait_until="networkidle")
    time.sleep(2)
    print(f"  URL: {page.url}")

    # Wait for user to complete Google login
    print("\n  Waiting up to 120s for Google login...")
    deadline = time.time() + 120
    while time.time() < deadline:
        cookies = ctx.cookies("https://accounts.google.com")
        cookie_names = [c["name"] for c in cookies]
        if any(c in cookie_names for c in ["SID", "SSID", "HSID"]):
            print("  Google session cookies found!")
            break
        time.sleep(2)
    else:
        print("  WARNING: Timeout waiting for Google cookies")

    # Step 2: Now go to ERP
    print("\nStep 2: Navigating to ERP...")
    page.goto("https://erp.developers.net/", wait_until="networkidle")
    time.sleep(3)
    print(f"  URL: {page.url}")
    print("  If on login page, click the Google button to sign in.")

    # Wait for ERP bearer
    print("\n  Waiting up to 120s for ERP login to complete...")
    deadline = time.time() + 120
    while time.time() < deadline:
        url = page.url
        if "/user/login" not in url and "erp.developers.net" in url:
            print(f"  Logged in! URL: {url}")
            break
        time.sleep(2)
    else:
        print(f"  Timeout. Final URL: {page.url}")

    # Save state
    ctx.storage_state(path="/app/playwright-profile/playwright_storage_state.json")
    print("\nDone. Storage state saved.")
    ctx.close()
