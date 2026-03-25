#!/usr/bin/env python3
"""Debug: get Google ID token via programmatic GSI credential request."""
import time
import json
from playwright.sync_api import sync_playwright

CLIENT_ID = "954028736401-f68r8k7hn8h5kmu5n6lhm7uf74qcfrn6.apps.googleusercontent.com"

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        "/app/playwright-profile",
        headless=True,
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    # First check if Google session cookies exist
    cookies = ctx.cookies("https://accounts.google.com")
    google_cookies = [c["name"] for c in cookies]
    print(f"1) Google cookies: {google_cookies}")
    has_session = any(c in google_cookies for c in ["SID", "SSID", "HSID", "APISID", "SAPISID"])
    print(f"   Has Google session: {has_session}")

    if not has_session:
        print("NO GOOGLE SESSION - need manual login first")
        ctx.close()
        raise SystemExit(1)

    # Navigate to a Google page first to activate cookies
    page.goto("https://accounts.google.com/", wait_until="networkidle")
    time.sleep(2)
    print(f"2) Google account page URL: {page.url}")
    print(f"   Page text: {page.inner_text('body')[:300]}")

    # Try the GSI iframe approach - get credential via the endpoint
    # that the ERP's GSI library would normally call
    print("\n3) Trying GSI credential endpoint...")
    gsi_url = (
        "https://accounts.google.com/gsi/select?"
        f"client_id={CLIENT_ID}&"
        "ux_mode=popup&"
        "ui_mode=card&"
        "as=1&"
        "context=signin"
    )
    page.goto(gsi_url, wait_until="networkidle")
    time.sleep(3)
    print(f"   URL: {page.url}")
    print(f"   Content: {page.inner_text('body')[:500]}")

    # Check for any credential/token in the page
    html = page.content()
    if "credential" in html.lower():
        print("   Found 'credential' in page HTML!")
        # Look for hidden inputs or data attributes
        inputs = page.query_selector_all("input")
        for inp in inputs:
            name = inp.get_attribute("name") or ""
            val = inp.get_attribute("value") or ""
            print(f"   input: name='{name}' value='{val[:80]}...'")

    ctx.close()
