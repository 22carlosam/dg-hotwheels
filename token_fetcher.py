#!/usr/bin/env python3
"""
Fetches a fresh Dollar General guest session by loading dollargeneral.com
in a headless browser and reading the auth cookies DG's JavaScript mints.

DG's site (as of mid-2026) packs the JWT, app session token, and customer
GUID into the single `omniSession` cookie, formatted as:
    appSessionToken|customerGuid|null|false|JWT
The static app credentials are hardcoded into DG's web bundle and don't
rotate per session, so we treat them as constants.
"""

import time

# Static, public app identifiers from DG's web bundle. These are the same
# for every guest session — they're not secrets, just client identifiers.
APP_TOKEN = "6dinqus4908fkssw9h7aa8ldcgkimn3p"
PARTNER_API_TOKEN = "11619A82-8E80-4A6F-8AD2-A14F4A8FFD74"


def _parse_omni_session(value):
    """omniSession = 'appSessionToken|customerGuid|null|false|JWT'."""
    parts = value.split("|")
    if len(parts) < 5:
        return None
    app_session_token, customer_guid, _, _, jwt = parts[0], parts[1], parts[2], parts[3], "|".join(parts[4:])
    if not jwt.startswith("eyJ"):
        return None
    return {
        "appSessionToken": app_session_token,
        "customerGuid": customer_guid,
        "idToken": jwt,
    }


def fetch_session(timeout_s=45, headless=True):
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

        # Poll for the omniSession cookie containing a JWT.
        deadline = time.time() + timeout_s
        parsed = None
        device_id = ""
        while time.time() < deadline:
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            device_id = cookies.get("uniqueDeviceId", device_id)
            omni = cookies.get("omniSession")
            if omni:
                parsed = _parse_omni_session(omni)
                if parsed and device_id:
                    break
            page.wait_for_timeout(500)

        browser.close()

    if not parsed:
        raise RuntimeError(
            "Could not obtain a session token from dollargeneral.com. "
            "DG may have changed their site, or the page failed to load."
        )

    return {
        "idToken": parsed["idToken"],
        "customerGuid": parsed["customerGuid"],
        "deviceId": device_id,
        "appSessionToken": parsed["appSessionToken"],
        "appToken": APP_TOKEN,
        "partnerApiToken": PARTNER_API_TOKEN,
    }


if __name__ == "__main__":
    s = fetch_session()
    for k, v in s.items():
        shown = v if len(v) < 60 else v[:57] + "..."
        print(f"{k:18} = {shown}")
