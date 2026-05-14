#!/usr/bin/env python3
"""
Fetches a fresh Dollar General guest session by loading dollargeneral.com
in a headless browser and reading the auth cookies DG's JavaScript mints.
"""

import time


# Cookie names DG sets that we need for API calls
NEEDED = ["idToken", "customerGuid", "uniqueDeviceId", "appSessionToken",
          "appToken", "partnerApiToken"]


def fetch_session(timeout_s=30, headless=True):
    """
    Returns a dict with idToken + the x-dg-* header values, or raises RuntimeError.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/147.0.0.0 Safari/537.36")
        )
        page = ctx.new_page()
        page.goto("https://www.dollargeneral.com/", wait_until="domcontentloaded",
                  timeout=timeout_s * 1000)

        # DG's JS mints the session cookies a moment after load. Poll for idToken.
        deadline = time.time() + timeout_s
        cookies = {}
        while time.time() < deadline:
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            if cookies.get("idToken"):
                break
            page.wait_for_timeout(500)

        browser.close()

    if not cookies.get("idToken"):
        raise RuntimeError(
            "Could not obtain a session token from dollargeneral.com. "
            "DG may have changed their site, or the page failed to load."
        )

    return {
        "idToken": cookies["idToken"],
        "customerGuid": cookies.get("customerGuid", ""),
        "deviceId": cookies.get("uniqueDeviceId", ""),
        "appSessionToken": cookies.get("appSessionToken", ""),
        "appToken": cookies.get("appToken", ""),
        "partnerApiToken": cookies.get("partnerApiToken", ""),
    }


if __name__ == "__main__":
    s = fetch_session()
    for k, v in s.items():
        shown = v if len(v) < 60 else v[:57] + "..."
        print(f"{k:18} = {shown}")
