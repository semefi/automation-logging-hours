#!/usr/bin/env python3
"""Debug: try OAuth flow directly using Google session cookies."""
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

    # Navigate directly to Google OAuth consent endpoint
    # This should auto-authenticate if Google session cookies are valid
    oauth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={CLIENT_ID}&"
        "response_type=id_token&"
        "scope=openid%20email%20profile&"
        "redirect_uri=https://erp.developers.net/&"
        "nonce=debug123&"
        "prompt=none"
    )

    print("1) Navigating to OAuth URL...")
    resp = page.goto(oauth_url, wait_until="networkidle")
    time.sleep(3)

    final_url = page.url
    print(f"2) Final URL: {final_url[:200]}")

    # Check if we got redirected back with a token
    if "id_token=" in final_url:
        print("3) SUCCESS - Got id_token in redirect!")
        # Extract token from URL fragment
        fragment = final_url.split("#", 1)[1] if "#" in final_url else ""
        params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)
        token = params.get("id_token", "")
        print(f"   Token (first 50 chars): {token[:50]}...")
    elif "error=" in final_url:
        fragment = final_url.split("#", 1)[1] if "#" in final_url else final_url.split("?", 1)[1] if "?" in final_url else ""
        print(f"3) OAuth error: {fragment[:300]}")
    else:
        print("3) No token in URL. Page content:")
        print(page.inner_text("body")[:1500])

    ctx.close()
