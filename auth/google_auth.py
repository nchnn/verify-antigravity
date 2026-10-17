# File: google_auth.py
# Created: 2026-09-12 11:56 PM
# Last Modified: 2026-09-14 05:18 PM

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
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from selenium.webdriver.common.keys import Keys

from config.settings import get_settings
from loaders.account_loader import Account
from audit.auditor import VerificationStatus

logger = logging.getLogger("AntigravityAuditor")

# --- Antigravity OAuth constants (same as AG Multi-Account Switchboard extension) ---
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


class GoogleAuthVerifier:
    """Handles Google OAuth sign-in and Antigravity access verification."""

    def __init__(self, driver):
        self.driver = driver
        self.settings = get_settings()
        self.wait = WebDriverWait(self.driver, self.settings.page_timeout_seconds)

    def _ensure_active_window(self) -> bool:
        """Ensures driver is focused on a valid open window handle."""
        try:
            handles = self.driver.window_handles
            if not handles:
                return False
            try:
                _ = self.driver.current_window_handle
            except Exception:
                self.driver.switch_to.window(handles[-1])
            return True
        except Exception:
            return False

    def _current_url(self) -> str:
        try:
            return (self.driver.current_url or "").lower()
        except Exception:
            return ""

    def _page_source(self) -> str:
        try:
            return (self.driver.page_source or "").lower()
        except Exception:
            return ""

    def _click_next(self, container_id: str):
        """Clicks the specific Next button natively with ActionChains and random pause."""
        try:
            btn = self.driver.find_element(
                By.CSS_SELECTOR, f"#{container_id} button, #{container_id}"
            )
            ActionChains(self.driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
            return
        except Exception:
            pass

        try:
            btn = self.driver.find_element(
                By.XPATH, "//button[.//span[text()='Next'] or contains(., 'Next')]"
            )
            ActionChains(self.driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
            return
        except Exception:
            pass

    def login_and_verify(self, account: Account) -> Tuple[VerificationStatus, str]:
        """
        Executes full authentication and Antigravity verification flow for an account.
        Checks the profile's existing cookies first before interacting with Google login.
        """
        if self.settings.browser_headless:
            logger.warning(
                f"[WARNING] Running Google authentication routines in HEADLESS mode for {account.email}! "
                "Google flags headless environments and blocks interactive CAPTCHAs/challenges. "
                "Headed mode is strongly recommended."
            )
            print(f"\n[!] WARNING: Google authentication routine running in HEADLESS mode for {account.email}!")
            print("[!] Headless execution significantly increases detection risk and blocks manual challenge resolution.\n")

        if not account.password:
            return (
                VerificationStatus.SKIPPED_NO_PASSWORD,
                "No password provided and no DEFAULT_ACCOUNT_PASSWORD set in .env",
                {},
            )

        logger.info(f"Checking existing profile session for: {account.email}")

        # Step 1: Navigate to accounts.google.com to force Chrome to hydrate profile cookies
        # into memory. Without this, CDP Network.getAllCookies returns empty on a blank new tab
        # even when the profile has valid saved cookies on disk.
        is_already_authed = False
        try:
            self._ensure_active_window()
            self.driver.get("https://accounts.google.com/")
            time.sleep(1.0)  # Quick page hydration check
            self._ensure_active_window()

            current_url = self._current_url()
            page_src = self._page_source()

            # If Chrome loaded the account chooser or myaccount → session is alive
            if (
                "myaccount.google.com" in current_url
                or "choose an account" in page_src
                or "select an account" in page_src
                or ("accounts.google.com" in current_url and account.email.lower() in page_src)
            ):
                logger.info(f"Active Google session detected via page state for: {account.email}")
                is_already_authed = True
            else:
                # Fallback: inspect CDP cookies (now populated after navigation)
                cdp_result = self.driver.execute_cdp_cmd("Network.getAllCookies", {})
                existing_cookies = {
                    c.get("name"): c.get("value")
                    for c in cdp_result.get("cookies", [])
                    if c.get("name")
                }
                if any(k in existing_cookies for k in ["SAPISID", "SID", "SSID", "__Secure-1PSID"]):
                    logger.info(f"Cached Google session detected via CDP cookies for: {account.email}")
                    is_already_authed = True
                else:
                    logger.info(f"No saved Google session in profile for {account.email}. Sign-in required.")
        except Exception as e:
            logger.debug(f"Profile cookie inspection error: {e}")

        # Step 2: If not already authenticated in this profile, go directly to Google Sign-in
        if not is_already_authed:
            login_status, login_msg, _ = self._perform_google_login(account)
            if login_status != VerificationStatus.SUCCESS_VERIFIED:
                return login_status, login_msg, {}

        # Step 3: Antigravity API Verification via OAuth token exchange
        logger.info(f"Starting Antigravity API verification for: {account.email}")
        status, message, token_data = self._verify_via_cloudcode_api(account)
        return status, message, token_data

    def _perform_google_login(self, account: Account) -> Tuple[VerificationStatus, str, dict]:
        """Performs Google account login using undetected-chromedriver."""
        try:
            self._ensure_active_window()
            self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
            time.sleep(1.0)  # Wait for sign-in page load
            self._ensure_active_window()

            # Check for Google "toomanysessions"
            current_url = self._current_url()
            page_src = self._page_source()
            if "toomanysessions" in current_url or "maximum number of accounts" in page_src:
                logger.warning("Max accounts reached in session (toomanysessions). Resetting sessions...")
                try:
                    here_link = self.driver.find_element(By.PARTIAL_LINK_TEXT, "Here")
                    here_link.click()
                    time.sleep(1.0)
                except Exception:
                    self.driver.delete_all_cookies()
                    self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
                    time.sleep(1.0)
                self._ensure_active_window()
                page_src = self._page_source()

            # If on recovery page by accident, navigate back to identifier
            if "usernamerecovery" in self._current_url():
                self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
                time.sleep(1.0)
                self._ensure_active_window()
                page_src = self._page_source()

            # Handle account chooser if present
            if "choose an account" in page_src or "select an account" in page_src:
                try:
                    acc_elem = self.driver.find_element(
                        By.XPATH, f"//*[contains(text(), '{account.email}')]"
                    )
                    acc_elem.click()
                    time.sleep(0.8)
                except Exception:
                    try:
                        use_another = self.driver.find_element(
                            By.XPATH, "//*[contains(text(), 'Use another account')]"
                        )
                        use_another.click()
                        time.sleep(0.8)
                    except Exception:
                        pass
                self._ensure_active_window()
                page_src = self._page_source()

            # Enter Email
            try:
                email_input = WebDriverWait(self.driver, 8).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, "input[type='email'], #identifierId")
                    )
                )
                email_input.clear()
                for ch in account.email:
                    email_input.send_keys(ch)
                    time.sleep(random.uniform(0.02, 0.07))
                time.sleep(random.uniform(0.2, 0.5))

                # Submit via Enter key and fallback to exact #identifierNext button
                email_input.send_keys(Keys.ENTER)
                time.sleep(0.3)

                # If still on the same page, explicitly click the Next button container
                if "identifier" in self.driver.current_url.lower() and "usernamerecovery" not in self.driver.current_url.lower():
                    time.sleep(random.uniform(0.2, 0.5))
                    self._click_next("identifierNext")

                time.sleep(1.0)  # Wait for page transition after email submit
            except TimeoutException:
                pass

            self._ensure_active_window()

            # Check for email errors
            current_src = self.driver.page_source.lower()
            if "couldn't find your google account" in current_src or "enter a valid email" in current_src:
                return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

            # Check for Google security rejection ("This browser or app may not be secure")
            if self._is_security_rejection_present():
                return (
                    VerificationStatus.ERROR,
                    "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                    {},
                )

            # Check if Google presented 'Confirm you're not a robot' immediately after email
            if self._is_bot_challenge_present():
                resolved = self._wait_for_bot_challenge_resolution(account.email)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "Robot verification challenge ('Confirm you're not a robot') timed out",
                        {},
                    )

            # Enter Password
            password_entered = self._enter_password(account.password)
            if not password_entered:
                self._ensure_active_window()
                # Check if blocked by security rejection
                if self._is_security_rejection_present():
                    return (
                        VerificationStatus.ERROR,
                        "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                        {},
                    )
                # Check if password field was blocked by 'Confirm you're not a robot'
                if self._is_bot_challenge_present():
                    resolved = self._wait_for_bot_challenge_resolution(account.email)
                    if resolved:
                        password_entered = self._enter_password(account.password)
                        if not password_entered:
                            if "myaccount.google.com" in self.driver.current_url or "gds.google.com" in self.driver.current_url:
                                return VerificationStatus.SUCCESS_VERIFIED, "Session already active", {}
                            return VerificationStatus.ERROR, "Password input field not encountered after robot challenge", {}
                    else:
                        return (
                            VerificationStatus.CHALLENGE_REQUIRED,
                            "Robot verification challenge ('Confirm you're not a robot') timed out",
                            {},
                        )
                elif "myaccount.google.com" in self.driver.current_url or "gds.google.com" in self.driver.current_url:
                    return VerificationStatus.SUCCESS_VERIFIED, "Session already active", {}
                else:
                    return VerificationStatus.ERROR, "Password input field not encountered", {}

            self._ensure_active_window()
            # Check for invalid credentials
            current_src = self.driver.page_source.lower()
            if "wrong password" in current_src or "incorrect password" in current_src:
                return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password provided", {}

            # Check if Google presented 'Confirm you're not a robot' after password
            if self._is_bot_challenge_present():
                resolved = self._wait_for_bot_challenge_resolution(account.email)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "Robot verification challenge ('Confirm you're not a robot') timed out",
                    )

            # Check for Google IAP QR-code verification challenge
            if self._is_qr_challenge_present():
                resolved = self._wait_for_qr_challenge_resolution(account.email)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "QR code verification challenge timed out — please scan the QR code on your phone and retry",
                        {},
                    )

            # Check for 2FA / Challenges / Recovery Phone Prompts
            current_src = self.driver.page_source.lower()
            if any(
                term in current_src
                for term in [
                    "2-step verification",
                    "verify it's you",
                    "challenge",
                    "check your phone",
                    "enter the code",
                    "confirm your recovery phone",
                    "verify your info to continue",
                    "scan the qr code",
                ]
            ):
                logger.warning(
                    f"2FA/Phone prompt detected for {account.email}. Waiting up to {self.settings.challenge_timeout_seconds}s for resolution..."
                )
                resolved = self._wait_for_challenge_resolution(timeout=self.settings.challenge_timeout_seconds)
                if not resolved:
                    return VerificationStatus.CHALLENGE_REQUIRED, "Security verification or 2FA required", {}

            return VerificationStatus.SUCCESS_VERIFIED, "Google authentication successful", {}

        except Exception as e:
            return VerificationStatus.ERROR, f"Google login error: {str(e)}", {}

    def _enter_password(self, password: str) -> bool:
        """Attempts to locate, type into, and submit the password input field."""
        try:
            pass_input = WebDriverWait(self.driver, 6).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']")
                )
            )
            pass_input.clear()
            for ch in password:
                pass_input.send_keys(ch)
                time.sleep(random.uniform(0.02, 0.07))
            time.sleep(random.uniform(0.2, 0.5))

            pass_input.send_keys(Keys.ENTER)
            time.sleep(0.3)

            if "challenge" not in self.driver.current_url.lower() and "myaccount" not in self.driver.current_url.lower():
                time.sleep(random.uniform(0.2, 0.5))
                self._click_next("passwordNext")

            time.sleep(1.5)  # Wait for post-password page transition
            return True
        except Exception:
            return False

    def _is_security_rejection_present(self) -> bool:
        """Detects whether Google blocked the sign-in with 'This browser or app may not be secure'."""
        try:
            url = self._current_url()
            src = self._page_source()

            if "signin/rejected" in url or "/rejected" in url:
                return True

            reject_phrases = [
                "this browser or app may not be secure",
                "couldn't sign you in",
                "try using a different browser",
            ]
            return any(phrase in src for phrase in reject_phrases)
        except Exception:
            return False

    def _is_bot_challenge_present(self) -> bool:
        """Detects whether Google has presented a 'Confirm you're not a robot' or reCAPTCHA screen."""
        try:
            url = self._current_url()
            src = self._page_source()

            # URL-based detection
            if "challenge/recaptcha" in url or "/recaptcha" in url:
                return True

            # Content-based detection
            bot_phrases = [
                "confirm you're not a robot",
                "please verify that you are not a robot",
                "not a robot",
            ]
            if any(phrase in src for phrase in bot_phrases):
                return True

            # DOM-based detection for reCAPTCHA iframes
            if self.driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha'], #recaptcha, .g-recaptcha"):
                return True

            return False
        except Exception:
            return False

    def _wait_for_bot_challenge_resolution(self, account_email: str, timeout: Optional[int] = None) -> bool:
        """
        Alerts the user and pauses execution in headed mode to allow manual completion
        of the 'Confirm you're not a robot' (reCAPTCHA) challenge in Chrome.
        Actively monitors for page navigation or resolution.
        """
        if self.settings.browser_headless:
            logger.error(
                f"[HEADLESS CAPTCHA] 'Confirm you're not a robot' triggered for {account_email} in headless mode. "
                f"Headless mode cannot solve on-screen CAPTCHAs interactively. "
                f"Please run with headed mode (set BROWSER_HEADLESS=false in .env or run without --headless) to solve."
            )
            return False

        wait_seconds = timeout or self.settings.challenge_timeout_seconds

        logger.warning(
            f"[ACTION REQUIRED] 'Confirm you're not a robot' challenge detected for {account_email}. "
            f"Waiting up to {wait_seconds}s for on-screen completion..."
        )
        print("\n" + "!" * 65)
        print(f"[!] ACTION REQUIRED FOR: {account_email}")
        print("[!] Google displayed 'Confirm you're not a robot' (reCAPTCHA).")
        print("[!] Please switch to the Chrome browser window and solve the verification.")
        print(f"[!] The script will automatically continue once completed (timeout: {wait_seconds}s)...")
        print("!" * 65 + "\n")

        start_time = time.time()
        last_logged = 0

        while time.time() - start_time < wait_seconds:
            self._ensure_active_window()
            current_url = self._current_url()
            current_src = self._page_source()

            # 1. Check if page transitioned away from the robot challenge
            if "challenge/recaptcha" not in current_url and "confirm you're not a robot" not in current_src:
                logger.info(f"[OK] Robot challenge resolved: page moved away from reCAPTCHA for {account_email}.")
                return True

            # 2. Check if password field is now visible
            try:
                pw_elements = self.driver.find_elements(
                    By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']"
                )
                if any(el.is_displayed() for el in pw_elements):
                    logger.info(f"[OK] Robot challenge resolved: Password field encountered for {account_email}.")
                    return True
            except Exception:
                pass

            # 3. Check if session already authenticated
            if any(marker in current_url for marker in ["myaccount.google.com", "antigravity.google", "checkcookie", "gds.google.com"]):
                logger.info(f"[OK] Robot challenge resolved: Authenticated session detected for {account_email}.")
                return True

            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 15 == 0 and elapsed != last_logged:
                print(f"[*] Still waiting for robot verification resolution... ({elapsed}/{wait_seconds}s)")
                last_logged = elapsed

            time.sleep(2)

        logger.warning(f"[FAIL] Robot challenge timed out after {wait_seconds}s for {account_email}.")
        return False

    def _wait_for_challenge_resolution(self, timeout: int = 60) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            self._ensure_active_window()
            current_url = self.driver.current_url.lower()
            if (
                "myaccount.google.com" in current_url
                or "accounts.google.com/checkcookie" in current_url
                or "gds.google.com" in current_url
            ):
                return True
            time.sleep(2)
        return False

    # ------------------------------------------------------------------
    # Google IAP QR-code challenge detection & handler
    # ------------------------------------------------------------------

    def _is_qr_challenge_present(self) -> bool:
        """
        Detects whether Google is showing the IAP QR-code verification page:
          accounts.google.com/v3/signin/challenge/iap/qrcode
        This is the 'Verify your info to continue — Scan the QR code with
        your phone' screen that appears when Google suspects bot/automation.
        """
        try:
            url = self._current_url()
            src = self._page_source()

            # URL-based detection (most reliable)
            if "challenge/iap/qrcode" in url:
                return True

            # Content-based fallback
            qr_phrases = [
                "scan the qr code with your phone",
                "scan the qr code",
                "verify your info to continue",
            ]
            if any(phrase in src for phrase in qr_phrases):
                return True

            return False
        except Exception:
            return False

    def _wait_for_qr_challenge_resolution(self, account_email: str, timeout: Optional[int] = None) -> bool:
        """
        Pauses execution and guides the user to scan the QR code on their phone.
        Polls every 2 s until Google redirects away from the QR challenge page,
        indicating the phone approved the request and verification succeeded.

        Works in both headed and headless modes:
        - Headed  : user sees the QR code right in the Chrome window.
        - Headless: user must open the URL printed to the console in a browser.
        """
        wait_seconds = timeout or self.settings.challenge_timeout_seconds

        try:
            qr_url = self.driver.current_url
        except Exception:
            qr_url = "https://accounts.google.com/"

        logger.warning(
            f"[QR CHALLENGE] Google QR-code verification triggered for {account_email}. "
            f"Waiting up to {wait_seconds}s for resolution..."
        )

        print("\n" + "=" * 65)
        print(f"[!] QR CODE VERIFICATION REQUIRED FOR: {account_email}")
        print("="  * 65)
        print("[!] Google needs to verify your device via a QR code.")
        print("")
        if self.settings.browser_headless:
            print(f"[!] Headless mode detected. Open this URL in your browser:")
            print(f"    {qr_url}")
            print("")
        else:
            print("[!] A QR code is now visible in the Chrome browser window.")
        print("[!] Steps:")
        print("    1. Open your phone's Camera app (or Google app).")
        print("    2. Scan the QR code shown on screen.")
        print("    3. Tap the link that appears on your phone.")
        print("    4. Follow the on-screen instructions on your phone.")
        print("    5. Switch BACK to your computer — the script will auto-continue.")
        print(f"[!] Timeout: {wait_seconds}s")
        print("=" * 65 + "\n")

        start_time = time.time()
        last_logged = 0

        while time.time() - start_time < wait_seconds:
            self._ensure_active_window()
            current_url = self._current_url()
            current_src = self._page_source()

            # 1. Redirected away from the QR challenge page → resolved
            if "challenge/iap/qrcode" not in current_url and "scan the qr code" not in current_src:
                logger.info(f"[OK] QR challenge resolved: page moved away from QR screen for {account_email}.")
                return True

            # 2. Landed on a post-auth page → definitely resolved
            auth_markers = [
                "myaccount.google.com",
                "accounts.google.com/checkcookie",
                "gds.google.com",
                "uplevelingstep",
                "auth_success",
            ]
            if any(marker in current_url for marker in auth_markers):
                logger.info(f"[OK] QR challenge resolved: authenticated page detected for {account_email}.")
                return True

            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 15 == 0 and elapsed != last_logged:
                print(f"[*] Still waiting for QR code scan... ({elapsed}/{wait_seconds}s)")
                last_logged = elapsed

            time.sleep(2)

        logger.warning(f"[FAIL] QR challenge timed out after {wait_seconds}s for {account_email}.")
        print(f"[FAIL] QR code verification timed out for {account_email}.")
        return False

    def _handle_oauth_consent(self):
        """Attempts to click 'Allow', 'Continue', or consent buttons on Google OAuth dialogs."""
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                text = btn.text.strip().lower()
                if text in ["continue", "allow", "i agree", "accept", "get started"]:
                    logger.info(f"Clicking consent/activation button: '{btn.text}'")
                    ActionChains(self.driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
                    time.sleep(1.0)
                    break
        except Exception:
            pass

    # ------------------------------------------------------------------
    # API-based Antigravity verification (replaces browser page scraping)
    # ------------------------------------------------------------------

    def _verify_via_cloudcode_api(self, account: Account) -> Tuple[VerificationStatus, str]:
        """
        Verifies Antigravity access by:
        1. Navigating to Google OAuth consent using Antigravity's own client credentials.
        2. Clicking the 'Sign in' button on the nativeapp consent page.
        3. Capturing the authorization code via a local callback server.
        4. Exchanging the code for an access_token.
        5. Calling cloudcode-pa.googleapis.com/v1internal:loadCodeAssist.
           - HTTP 200 + allowedTiers present  → SUCCESS_VERIFIED
           - HTTP 403                          → ACCESS_FORBIDDEN
        """
        captured_code: list = []  # mutable container for thread sharing
        server_error: list = []

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
                pass  # suppress noisy HTTP server logs

        # Start local callback server
        try:
            callback_server = HTTPServer(("127.0.0.1", _AG_REDIRECT_PORT), _CallbackHandler)
            callback_server.timeout = 1
        except OSError as e:
            return VerificationStatus.ERROR, f"Cannot bind callback port {_AG_REDIRECT_PORT}: {e}"

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

        # Build OAuth URL with login_hint so Chrome skips account chooser
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
            self._ensure_active_window()
            self.driver.get(oauth_url)

            # Poll: click 'Sign in' on the nativeapp consent page, capture code redirect
            deadline = time.time() + 60
            while time.time() < deadline:
                self._ensure_active_window()
                current_url = self._current_url()

                # Code already captured by server
                if captured_code:
                    break

                # Code in URL (fallback — shouldn't normally redirect to localhost in browser)
                if "code=" in current_url and "127.0.0.1" in current_url:
                    parsed = urllib.parse.urlparse(current_url)
                    params = urllib.parse.parse_qs(parsed.query)
                    code = params.get("code", [None])[0]
                    if code:
                        captured_code.append(code)
                    break

                # Handle Google IAP QR-code challenge mid-OAuth flow
                if self._is_qr_challenge_present():
                    resolved = self._wait_for_qr_challenge_resolution(account.email)
                    if not resolved:
                        return (
                            VerificationStatus.CHALLENGE_REQUIRED,
                            "QR code verification challenge timed out during OAuth consent flow",
                        )
                    # After QR challenge is resolved, Google may redirect to the consent page;
                    # extend the deadline and keep polling
                    deadline = time.time() + 30
                    continue

                # Click 'Sign in' or 'Allow' / 'Continue' buttons on the consent page
                try:
                    buttons = self.driver.find_elements(By.TAG_NAME, "button")
                    for btn in buttons:
                        txt = btn.text.strip().lower()
                        if txt in ["sign in", "continue", "allow", "i agree", "accept", "get started"]:
                            logger.info(f"Clicking OAuth consent button: '{btn.text}'")
                            ActionChains(self.driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
                            time.sleep(1.5)
                            break
                except Exception:
                    pass

                time.sleep(0.5)

            server_thread.join(timeout=5)

        except Exception as e:
            return VerificationStatus.ERROR, f"OAuth navigation error: {e}"

        if not captured_code:
            return VerificationStatus.ERROR, "OAuth authorization code not captured (timeout or user did not consent)", {}

        # Exchange code for access_token
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

        # Step 1: Initialize account via onboardUser
        logger.info(f"Initiating standard-tier onboarding for: {account.email}")
        self._call_onboard_user(access_token, account.email)

        # Step 2: Query loadCodeAssist for active and ineligible tiers
        logger.info(f"Querying loadCodeAssist for: {account.email}")
        response_data, status_code, err_msg = self._call_load_code_assist(access_token, account.email)

        if response_data:
            allowed_tiers = response_data.get("allowedTiers", [])
            ineligible = response_data.get("ineligibleTiers", [])

            # Check if one-time phone verification is required
            val_url = self._extract_validation_url(response_data)
            if val_url:
                logger.info(f"VALIDATION_REQUIRED detected for {account.email}. Opening phone verification handler...")
                return self._handle_phone_validation_challenge(account, val_url, access_token, refresh_token)

            if allowed_tiers:
                tier_names = ", ".join(t.get("name", "") for t in allowed_tiers)
                if self.settings.auto_register_9router and refresh_token:
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

        # If HTTP error occurred, check if response payload contains VALIDATION_REQUIRED URL
        val_url = self._extract_validation_url(None, err_msg)
        if val_url:
            logger.info(f"VALIDATION_REQUIRED detected from error payload for {account.email}. Opening handler...")
            return self._handle_phone_validation_challenge(account, val_url, access_token, refresh_token)

        if status_code == 403:
            logger.warning(f"Antigravity API 403 for {account.email}: {err_msg[:200]}")
            return (
                VerificationStatus.ACCESS_FORBIDDEN,
                "Antigravity access forbidden (account not whitelisted or enrolled)",
                {},
            )

        return VerificationStatus.ERROR, f"API error: {err_msg[:120]}", {}

    def _call_onboard_user(self, access_token: str, account_email: str) -> Optional[dict]:
        """Calls cloudcode-pa.googleapis.com/v1internal:onboardUser to register standard-tier."""
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

    def _call_load_code_assist(self, access_token: str, account_email: str) -> Tuple[Optional[dict], Optional[int], str]:
        """Calls cloudcode-pa.googleapis.com/v1internal:loadCodeAssist."""
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

    def _extract_validation_url(self, data: Optional[dict], raw_err: str = "") -> Optional[str]:
        """Extracts validationUrl from ineligibleTiers or from error payloads."""
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
            match = re.search(r'https://accounts\.google\.com/signin/continue[^\s"\'\\}]+', raw_err)
            if match:
                return match.group(0)
        return None

    def _handle_phone_validation_challenge(
        self, account: Account, validation_url: str, access_token: str, refresh_token: str
    ) -> Tuple[VerificationStatus, str, dict]:
        """
        Handles Google account phone verification (VALIDATION_REQUIRED):
        1. Formats validation URL with authuser=0 so Google targets the right active account.
        2. In headed mode: navigates active browser to validation URL.
        3. Prints clear conversational steps and links to the console.
        4. Monitors browser navigation for the success landing page
           (developers.google.com/gemini-code-assist/auth/auth_success_gemini)
           and simultaneously polls loadCodeAssist.
        5. Upon completion: triggers retry of onboardUser and loadCodeAssist to confirm quota unlocks.
        """
        clean_url = validation_url
        if clean_url.endswith("&authuser"):
            clean_url = clean_url + "=0"
        elif "&authuser=" not in clean_url:
            clean_url = clean_url + "&authuser=0"

        wait_seconds = getattr(self.settings, "challenge_timeout_seconds", 120) or 120

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
        if self.settings.browser_headless:
            print("[!] Headless mode detected. Please open the URL above in your browser")
            print("    to complete phone verification.")
        else:
            print("[*] The verification page is opening in your Chrome browser window.")
            print("[*] Please complete the on-screen phone verification steps in Chrome.")
            print("    Upon completion, Google will redirect to:")
            print(f"    {_AUTH_SUCCESS_LANDING_PAGE}")
        print(f"[*] The suite will automatically monitor and retry (Timeout: {wait_seconds}s)...")
        print("=" * 70 + "\n")

        if not self.settings.browser_headless:
            try:
                self._ensure_active_window()
                self.driver.get(clean_url)
            except Exception as e:
                logger.debug(f"Error loading validation URL in browser: {e}")

        from auth.phone_verifier import PhoneValidationHandler
        pv_handler = PhoneValidationHandler(self.driver)

        start_time = time.time()
        last_logged = 0
        last_api_poll = 0
        landing_page_reached = False

        while time.time() - start_time < wait_seconds:
            if not self.settings.browser_headless:
                self._ensure_active_window()
                curr_url = self._current_url()

                if pv_handler.is_success_url(curr_url):
                    logger.info(f"[OK] Phone verification landing page reached for {account.email}!")
                    print(f"\n[OK] Authorization success page confirmed for {account.email}!")
                    landing_page_reached = True
                    break

                # Auto-click consent / terms / upleveling buttons if on screen
                pv_handler.click_consent_or_next()

                # Extract and display QR deep link if present
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
                poll_data, poll_status, _ = self._call_load_code_assist(access_token, account.email)
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
        self._call_onboard_user(access_token, account.email)
        time.sleep(1.0)

        res_data, status_code, err_msg = self._call_load_code_assist(access_token, account.email)
        if res_data:
            allowed_tiers = res_data.get("allowedTiers", [])
            ineligible_tiers = res_data.get("ineligibleTiers", [])
            has_validation = any(t.get("reasonCode") == "VALIDATION_REQUIRED" for t in ineligible_tiers)

            if allowed_tiers and not has_validation:
                tier_names = ", ".join(t.get("name", "") for t in allowed_tiers)
                print(f"[SUCCESS] Antigravity access unlocked for {account.email}! Tiers: {tier_names}\n")
                if self.settings.auto_register_9router and refresh_token:
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
