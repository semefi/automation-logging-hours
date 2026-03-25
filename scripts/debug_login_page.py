#!/usr/bin/env python3
"""Debug: find the Google login button on ERP login page."""
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
    print("URL:", page.url)

    # Dump all visible text and interactive elements
    print("\n--- ALL BUTTONS ---")
    for el in page.query_selector_all("button"):
        print(f"  button: text='{el.text_content().strip()}' class='{el.get_attribute('class') or ''}' id='{el.get_attribute('id') or ''}'")

    print("\n--- ALL INPUTS ---")
    for el in page.query_selector_all("input"):
        print(f"  input: type='{el.get_attribute('type')}' value='{el.get_attribute('value') or ''}' placeholder='{el.get_attribute('placeholder') or ''}'")

    print("\n--- ALL DIVS/SPANS WITH CLICK OR GOOGLE ---")
    for el in page.query_selector_all("div, span, a"):
        text = (el.text_content() or "").strip()
        cls = el.get_attribute("class") or ""
        onclick = el.get_attribute("onclick") or ""
        role = el.get_attribute("role") or ""
        if any(kw in (text + cls + onclick + role).lower() for kw in ["google", "click", "login", "sign", "btn", "button"]):
            tag = el.evaluate("el => el.tagName")
            print(f"  {tag}: text='{text[:80]}' class='{cls[:80]}' role='{role}'")

    print("\n--- PAGE TEXT (first 2000 chars) ---")
    print(page.inner_text("body")[:2000])

    ctx.close()
