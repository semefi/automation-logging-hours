#!/usr/bin/env python3
"""Open Google accounts page for manual login. Press Enter when done."""
import os
import subprocess
import sys

display = os.environ.get("DISPLAY", ":99")
user_data_dir = os.environ.get("USER_DATA_DIR", "/app/playwright-profile")
chromium_path = subprocess.run(
    ["python3", "-c", "from playwright.sync_api import sync_playwright; pw=sync_playwright().start(); print(pw.chromium.executable_path); pw.stop()"],
    capture_output=True, text=True
).stdout.strip()

print(f"Using DISPLAY={display}")
print(f"Using Chromium: {chromium_path}")
print(f"Using profile: {user_data_dir}")

# Launch Chromium directly (not via Playwright) so it respects DISPLAY
proc = subprocess.Popen([
    chromium_path,
    "--no-sandbox",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    f"--user-data-dir={user_data_dir}",
    "https://accounts.google.com/",
], env={**os.environ, "DISPLAY": display})

print("Browser abierto en VNC. Haz login en Google.")
print("Presiona Enter aqui cuando termines...")
try:
    input()
except KeyboardInterrupt:
    pass

proc.terminate()
proc.wait(timeout=10)
print("Browser cerrado.")
