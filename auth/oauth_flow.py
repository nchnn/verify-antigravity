import os
import re
import time
import json
import random
import logging
import threading
import urllib.parse
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional, Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains

from config.settings import get_settings
from loaders.account_loader import Account
from audit.auditor import VerificationStatus
from auth.challenge_handlers import (
    ensure_active_window,
    current_url,
    page_source,
    is_qr_challenge_present,
    wait_for_qr_challenge_resolution,
)

logger = logging.getLogger("AntigravityAuditor")

_AG_CLIENT_ID = os.environ.get("AG_CLIENT_ID", "")
_AG_CLIENT_SECRET = os.environ.get("AG_CLIENT_SECRET", "")
_AG_SCOPES = " ".join([
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
])
_AG_REDIRECT_PORT = 19876
_AG_REDIRECT_URI = f"http://127.0.0.1:{_AG_REDIRECT_PORT}/callback"
_AG_USER_AGENT = "antigravity/ide/2.11.0 darwin/arm64"
_ONBOARD_USER_ENDPOINTS = [
    "https://cloudcode-pa.googleapis.com/v1internal:onboardUser",
    "https://daily-cloudcode-pa.googleapis.com/v1internal:onboardUser",
]
_LOAD_CODE_ASSIST_ENDPOINTS = [
    "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
    "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
]
_STANDARD_IDE_METADATA = {"ideType": 9, "platform": 1, "pluginType": 2}
_AUTH_SUCCESS_LANDING_PAGE = "developers.google.com/gemini-code-assist/auth/auth_success_gemini"


def call_onboard_user(access_token: str, account_email: str) -> Optional[dict]:
    payload = {
        "tierId": "standard-tier",
        "cloudaicompanionProject": "aicode-consumers",
        "metadata": _STANDARD_IDE_METADATA,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": _AG_USER_AGENT,
        "x-request-source": "local",
    }

    for endpoint in _ONBOARD_USER_ENDPOINTS:
        try:
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                logger.debug(f"onboardUser succeeded for {account_email} via {endpoint}")
                return resp_data
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            logger.debug(f"onboardUser {endpoint} HTTP {he.code}: {err_body[:200]}")
            try:
                return json.loads(err_body)
            except Exception:
                pass
            if he.code in (400, 403):
                break
        except Exception as e:
            logger.debug(f"onboardUser {endpoint} error: {e}")
    return None


def call_load_code_assist(access_token: str, account_email: str) -> Tuple[Optional[dict], Optional[int], str]:
    payload = {"metadata": _STANDARD_IDE_METADATA}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": _AG_USER_AGENT,
        "x-request-source": "local",
    }

    last_err = ""
    for endpoint in _LOAD_CODE_ASSIST_ENDPOINTS:
        try:
            req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data, 200, ""
        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")
            logger.debug(f"loadCodeAssist {endpoint} HTTP {he.code}: {err_body[:250]}")
            try:
                err_json = json.loads(err_body)
                return err_json, he.code, err_body
            except Exception:
                pass
            last_err = f"HTTP {he.code}: {err_body[:150]}"
            if he.code == 403:
                return None, 403, err_body
        except Exception as e:
            logger.debug(f"loadCodeAssist {endpoint} error: {e}")
            last_err = str(e)
    return None, None, last_err


def extract_validation_url(data: Optional[dict], raw_err: str = "") -> Optional[str]:
    if isinstance(data, dict):
        ineligible = data.get("ineligibleTiers", [])
        for tier in ineligible:
            if tier.get("reasonCode") == "VALIDATION_REQUIRED" and tier.get("validationUrl"):
                return tier.get("validationUrl")
            if tier.get("validationUrl"):
                return tier.get("validationUrl")

        error_obj = data.get("error", {})
        if isinstance(error_obj, dict):
            details = error_obj.get("details", [])
            for det in details:
                if isinstance(det, dict) and det.get("validationUrl"):
                    return det.get("validationUrl")

    if raw_err:
        match = re.search(r'https://accounts\.google\.com/signin/continue[^\s"\'\\\}]+', raw_err)
        if match:
            return match.group(0)
    return None


def handle_phone_validation_challenge(
    driver, account: Account, validation_url: str, access_token: str, refresh_token: str, settings
) -> Tuple[VerificationStatus, str, dict]:
    clean_url = validation_url
    if clean_url.endswith("&authuser"):
        clean_url = clean_url + "=0"
    elif "&authuser=" not in clean_url:
        clean_url = clean_url + "&authuser=0"

    wait_seconds = getattr(settings, "challenge_timeout_seconds", 120) or 120

    logger.warning(
        f"[VALIDATION_REQUIRED] Phone verification required for {account.email}. "
        f"Waiting up to {wait_seconds}s for resolution..."
    )

    print("\n" + "=" * 70)
    print(f"[!] ACTION REQUIRED: PHONE VERIFICATION REQUIRED FOR: {account.email}")
    print("=" * 70)
    print("[*] Google account security check requires a one-time phone verification")
    print("[*] to unlock Antigravity / Gemini Code Assist standard-tier access.")
    print("")
    print(f"[*] Validation URL:\n    {clean_url}\n")
    if settings.browser_headless:
        print("[!] Headless mode detected. Please open the URL above in your browser")
        print("    to complete phone verification.")
    else:
        print("[*] The verification page is opening in your Chrome browser window.")
        print("[*] Please complete the on-screen phone verification steps in Chrome.")
        print("    Upon completion, Google will redirect to:")
        print(f"    {_AUTH_SUCCESS_LANDING_PAGE}")
    print(f"[*] The suite will automatically monitor and retry (Timeout: {wait_seconds}s)...")
    print("=" * 70 + "\n")

    if not settings.browser_headless:
        try:
            ensure_active_window(driver)
            driver.get(clean_url)
        except Exception as e:
            logger.debug(f"Error loading validation URL in browser: {e}")

    from auth.phone_verifier import PhoneValidationHandler
    pv_handler = PhoneValidationHandler(driver)

    start_time = time.time()
    last_logged = 0
    last_api_poll = 0
    landing_page_reached = False

    while time.time() - start_time < wait_seconds:
        if not settings.browser_headless:
            ensure_active_window(driver)
            curr_url = current_url(driver)

            if pv_handler.is_success_url(curr_url):
                logger.info(f"[OK] Phone verification landing page reached for {account.email}!")
                print(f"\n[OK] Authorization success page confirmed for {account.email}!")
                landing_page_reached = True
                break

            pv_handler.click_consent_or_next()

            qr_details = pv_handler.extract_qr_challenge()
            if qr_details and qr_details.get("deep_link"):
                elapsed = int(time.time() - start_time)
                if elapsed > 0 and elapsed % 20 == 0 and elapsed != last_logged:
                    print(f"\n[!] Direct Device Verification Link: {qr_details['deep_link']}")
                    print(f"[*] Open link on your phone camera or mobile browser to verify.\n")
                    last_logged = elapsed

            if ("signin/continue" not in curr_url and "signin/challenge" not in curr_url):
                try:
                    _parsed = urllib.parse.urlparse(curr_url)
                    _hp = _parsed.netloc + _parsed.path
                except Exception:
                    _hp = curr_url
                if any(
                    marker in _hp
                    for marker in ["developers.google.com", "myaccount.google.com", "cloud.google.com"]
                ):
                    logger.info(f"[OK] Redirected away from validation challenge: {curr_url}")
                    landing_page_reached = True
                    break

        now = time.time()
        if now - last_api_poll >= 15:
            last_api_poll = now
            logger.debug(f"Checking server-side verification status for {account.email}...")
            poll_data, poll_status, _ = call_load_code_assist(access_token, account.email)
            if poll_data:
                allowed = poll_data.get("allowedTiers", [])
                inelig = poll_data.get("ineligibleTiers", [])
                has_val = any(t.get("reasonCode") == "VALIDATION_REQUIRED" for t in inelig)
                if allowed and not has_val:
                    logger.info(f"[OK] Server-side verification confirmed during poll for {account.email}!")
                    landing_page_reached = True
                    break

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 15 == 0 and elapsed != last_logged:
            print(f"[*] Still waiting for phone verification completion... ({elapsed}/{wait_seconds}s)")
            last_logged = elapsed

        time.sleep(1.0)

    print(f"\n[*] Re-evaluating Antigravity tiers for {account.email}...")
    call_onboard_user(access_token, account.email)
    time.sleep(1.0)

    res_data, status_code, err_msg = call_load_code_assist(access_token, account.email)
    if res_data:
        allowed_tiers = res_data.get("allowedTiers", [])
        ineligible_tiers = res_data.get("ineligibleTiers", [])
        has_validation = any(t.get("reasonCode") == "VALIDATION_REQUIRED" for t in ineligible_tiers)

        if allowed_tiers and not has_validation:
            tier_names = ", ".join(t.get("name", "") for t in allowed_tiers)
            print(f"[SUCCESS] Antigravity access unlocked for {account.email}! Tiers: {tier_names}\n")
            if settings.auto_register_9router and refresh_token:
                try:
                    from auth.db_sync import NineRouterDbSync
                    NineRouterDbSync().sync_account(account.email, access_token, refresh_token)
                except Exception as dbe:
                    logger.warning(f"Failed to sync {account.email} to 9router: {dbe}")

            return (
                VerificationStatus.SUCCESS_VERIFIED,
                f"Antigravity API confirmed access after phone verification. Tiers: {tier_names}",
                {"access_token": access_token, "refresh_token": refresh_token or ""},
            )

    if not landing_page_reached:
        print(f"[TIMEOUT] Phone verification timed out after {wait_seconds}s for {account.email}.\n")
        return (
            VerificationStatus.VALIDATION_REQUIRED,
            f"Phone verification challenge (VALIDATION_REQUIRED) timed out. Complete via: {clean_url}",
            {},
        )
    else:
        return (
            VerificationStatus.ACCESS_FORBIDDEN,
            "Phone verification submitted, but standard-tier quota remains locked on server.",
            {},
        )


def verify_via_cloudcode_api(driver, account: Account, settings) -> Tuple[VerificationStatus, str, dict]:
    captured_code: list = []

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            if code:
                captured_code.append(code)
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Auth complete. You may close this tab.</h2></body></html>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"No code received.")

        def log_message(self, fmt, *args):
            pass

    try:
        callback_server = HTTPServer(("127.0.0.1", _AG_REDIRECT_PORT), _CallbackHandler)
        callback_server.timeout = 1
    except OSError as e:
        return VerificationStatus.ERROR, f"Cannot bind callback port {_AG_REDIRECT_PORT}: {e}", {}

    def _serve_until_code():
        deadline = time.time() + 30
        while time.time() < deadline and not captured_code:
            try:
                callback_server.handle_request()
            except Exception:
                break
        callback_server.server_close()

    server_thread = threading.Thread(target=_serve_until_code, daemon=True)
    server_thread.start()

    oauth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode({
            "client_id": _AG_CLIENT_ID,
            "redirect_uri": _AG_REDIRECT_URI,
            "response_type": "code",
            "scope": _AG_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "login_hint": account.email,
        })
    )

    try:
        logger.info(f"Navigating to Antigravity OAuth consent for: {account.email}")
        ensure_active_window(driver)
        driver.get(oauth_url)

        deadline = time.time() + 60
        while time.time() < deadline:
            ensure_active_window(driver)
            cur_url = current_url(driver)

            if captured_code:
                break

            if "code=" in cur_url and "127.0.0.1" in cur_url:
                parsed = urllib.parse.urlparse(cur_url)
                params = urllib.parse.parse_qs(parsed.query)
                code = params.get("code", [None])[0]
                if code:
                    captured_code.append(code)
                break

            if is_qr_challenge_present(driver):
                resolved = wait_for_qr_challenge_resolution(driver, account.email, settings)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "QR code verification challenge timed out during OAuth consent flow",
                        {},
                    )
                deadline = time.time() + 30
                continue

            try:
                buttons = driver.find_elements(By.TAG_NAME, "button")
                for btn in buttons:
                    txt = btn.text.strip().lower()
                    if txt in ["sign in", "continue", "allow", "i agree", "accept", "get started"]:
                        logger.info(f"Clicking OAuth consent button: '{btn.text}'")
                        ActionChains(driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
                        time.sleep(1.5)
                        break
            except Exception:
                pass

            time.sleep(0.5)

        server_thread.join(timeout=5)

    except Exception as e:
        return VerificationStatus.ERROR, f"OAuth navigation error: {e}", {}

    if not captured_code:
        return VerificationStatus.ERROR, "OAuth authorization code not captured (timeout or user did not consent)", {}

    try:
        token_body = urllib.parse.urlencode({
            "client_id": _AG_CLIENT_ID,
            "client_secret": _AG_CLIENT_SECRET,
            "code": captured_code[0],
            "grant_type": "authorization_code",
            "redirect_uri": _AG_REDIRECT_URI,
        }).encode("utf-8")
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=15) as resp:
            token_data = json.loads(resp.read().decode("utf-8"))
        access_token: Optional[str] = token_data.get("access_token")
        refresh_token: Optional[str] = token_data.get("refresh_token", "")
        if not access_token:
            return VerificationStatus.ERROR, "Token exchange succeeded but no access_token returned", {}
        logger.info(f"Access token obtained for: {account.email}")
    except Exception as e:
        return VerificationStatus.ERROR, f"Token exchange failed: {e}", {}

    logger.info(f"Initiating standard-tier onboarding for: {account.email}")
    call_onboard_user(access_token, account.email)

    logger.info(f"Querying loadCodeAssist for: {account.email}")
    response_data, status_code, err_msg = call_load_code_assist(access_token, account.email)

    if response_data:
        allowed_tiers = response_data.get("allowedTiers", [])
        ineligible = response_data.get("ineligibleTiers", [])

        val_url = extract_validation_url(response_data)
        if val_url:
            logger.info(f"VALIDATION_REQUIRED detected for {account.email}. Opening phone verification handler...")
            return handle_phone_validation_challenge(driver, account, val_url, access_token, refresh_token, settings)

        if allowed_tiers:
            tier_names = ", ".join(t.get("name", "") for t in allowed_tiers)
            if settings.auto_register_9router and refresh_token:
                try:
                    from auth.db_sync import NineRouterDbSync
                    NineRouterDbSync().sync_account(account.email, access_token, refresh_token)
                except Exception as dbe:
                    logger.warning(f"Failed to sync {account.email} to 9router: {dbe}")

            return (
                VerificationStatus.SUCCESS_VERIFIED,
                f"Antigravity API confirmed access. Tiers: {tier_names}",
                {"access_token": access_token, "refresh_token": refresh_token or ""},
            )

        reason = ineligible[0].get("reasonMessage", "No allowed tiers") if ineligible else "No allowed tiers"
        return VerificationStatus.ACCESS_FORBIDDEN, f"Antigravity access denied: {reason[:120]}", {}

    val_url = extract_validation_url(None, err_msg)
    if val_url:
        logger.info(f"VALIDATION_REQUIRED detected from error payload for {account.email}. Opening handler...")
        return handle_phone_validation_challenge(driver, account, val_url, access_token, refresh_token, settings)

    if status_code == 403:
        logger.warning(f"Antigravity API 403 for {account.email}: {err_msg[:200]}")
        return (
            VerificationStatus.ACCESS_FORBIDDEN,
            "Antigravity access forbidden (account not whitelisted or enrolled)",
            {},
        )

    return VerificationStatus.ERROR, f"API error: {err_msg[:120]}", {}
