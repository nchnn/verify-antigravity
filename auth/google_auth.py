# File: google_auth.py
# Created: 2026-09-12 11:56 PM
# Last Modified: 2026-09-13 02:26 AM

import time
import logging
from typing import Tuple
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from selenium.webdriver.common.keys import Keys

from config.settings import get_settings
from loaders.account_loader import Account
from audit.auditor import VerificationStatus

logger = logging.getLogger("AntigravityAuditor")


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
        """Clicks the specific Next button without clicking 'Forgot email' or 'Forgot password'."""
        try:
            btn = self.driver.find_element(
                By.CSS_SELECTOR, f"#{container_id} button, #{container_id}"
            )
            btn.click()
            return
        except Exception:
            pass

        try:
            btn = self.driver.find_element(
                By.XPATH, "//button[.//span[text()='Next'] or contains(., 'Next')]"
            )
            btn.click()
            return
        except Exception:
            pass

    def login_and_verify(self, account: Account) -> Tuple[VerificationStatus, str]:
        """
        Executes full authentication and Antigravity verification flow for an account.
        Checks the profile's existing cookies first before interacting with Google login.
        """
        if not account.password:
            return (
                VerificationStatus.SKIPPED_NO_PASSWORD,
                "No password provided and no DEFAULT_ACCOUNT_PASSWORD set in .env",
            )

        logger.info(f"Checking existing profile session for: {account.email}")

        # Step 1: Navigate to accounts.google.com to force Chrome to hydrate profile cookies
        # into memory. Without this, CDP Network.getAllCookies returns empty on a blank new tab
        # even when the profile has valid saved cookies on disk.
        is_already_authed = False
        try:
            self._ensure_active_window()
            self.driver.get("https://accounts.google.com/")
            time.sleep(self.settings.action_delay_seconds)
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
            login_status, login_msg = self._perform_google_login(account)
            if login_status != VerificationStatus.SUCCESS_VERIFIED:
                return login_status, login_msg

        # Step 3: Antigravity Portal Verification
        logger.info(f"Navigating to Antigravity Portal: {self.settings.antigravity_portal_url}")
        try:
            self._ensure_active_window()
            self.driver.get(self.settings.antigravity_portal_url)
            time.sleep(self.settings.action_delay_seconds * 2)
            self._ensure_active_window()

            # If redirected to Google sign-in (e.g. session expired), perform login
            if "accounts.google.com/signin" in self._current_url():
                logger.info("Session expired or sign-in prompt on portal. Performing login...")
                login_status, login_msg = self._perform_google_login(account)
                if login_status != VerificationStatus.SUCCESS_VERIFIED:
                    return login_status, login_msg
                self.driver.get(self.settings.antigravity_portal_url)
                time.sleep(self.settings.action_delay_seconds * 2)

            # Handle possible consent/OAuth authorization button
            self._handle_oauth_consent()
            self._ensure_active_window()

            # Detect Access forbidden vs Verified
            return self._check_antigravity_status()

        except Exception as e:
            return VerificationStatus.ERROR, f"Portal verification exception: {str(e)}"

    def _perform_google_login(self, account: Account) -> Tuple[VerificationStatus, str]:
        """Performs Google account login using undetected-chromedriver."""
        try:
            self._ensure_active_window()
            self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
            time.sleep(self.settings.action_delay_seconds)
            self._ensure_active_window()

            # Check for Google "toomanysessions"
            current_url = self._current_url()
            page_src = self._page_source()
            if "toomanysessions" in current_url or "maximum number of accounts" in page_src:
                logger.warning("Max accounts reached in session (toomanysessions). Resetting sessions...")
                try:
                    here_link = self.driver.find_element(By.PARTIAL_LINK_TEXT, "Here")
                    here_link.click()
                    time.sleep(self.settings.action_delay_seconds)
                except Exception:
                    self.driver.delete_all_cookies()
                    self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
                    time.sleep(self.settings.action_delay_seconds)
                self._ensure_active_window()
                page_src = self._page_source()

            # If on recovery page by accident, navigate back to identifier
            if "usernamerecovery" in self._current_url():
                self.driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
                time.sleep(self.settings.action_delay_seconds)
                self._ensure_active_window()
                page_src = self._page_source()

            # Handle account chooser if present
            if "choose an account" in page_src or "select an account" in page_src:
                try:
                    acc_elem = self.driver.find_element(
                        By.XPATH, f"//*[contains(text(), '{account.email}')]"
                    )
                    acc_elem.click()
                    time.sleep(self.settings.action_delay_seconds)
                except Exception:
                    try:
                        use_another = self.driver.find_element(
                            By.XPATH, "//*[contains(text(), 'Use another account')]"
                        )
                        use_another.click()
                        time.sleep(self.settings.action_delay_seconds)
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
                    time.sleep(0.02)
                time.sleep(0.3)

                # Submit via Enter key and fallback to exact #identifierNext button
                email_input.send_keys(Keys.ENTER)
                time.sleep(0.5)

                # If still on the same page, explicitly click the Next button container
                if "identifier" in self.driver.current_url.lower() and "usernamerecovery" not in self.driver.current_url.lower():
                    self._click_next("identifierNext")

                time.sleep(self.settings.action_delay_seconds)
            except TimeoutException:
                pass

            self._ensure_active_window()

            # Check for email errors
            current_src = self.driver.page_source.lower()
            if "couldn't find your google account" in current_src or "enter a valid email" in current_src:
                return VerificationStatus.INVALID_CREDENTIALS, "Google account not found"

            # Check for Google security rejection ("This browser or app may not be secure")
            if self._is_security_rejection_present():
                return (
                    VerificationStatus.ERROR,
                    "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                )

            # Check if Google presented 'Confirm you're not a robot' immediately after email
            if self._is_bot_challenge_present():
                resolved = self._wait_for_bot_challenge_resolution(account.email)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "Robot verification challenge ('Confirm you're not a robot') timed out",
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
                    )
                # Check if password field was blocked by 'Confirm you're not a robot'
                if self._is_bot_challenge_present():
                    resolved = self._wait_for_bot_challenge_resolution(account.email)
                    if resolved:
                        password_entered = self._enter_password(account.password)
                        if not password_entered:
                            if "myaccount.google.com" in self.driver.current_url or "gds.google.com" in self.driver.current_url:
                                return VerificationStatus.SUCCESS_VERIFIED, "Session already active"
                            return VerificationStatus.ERROR, "Password input field not encountered after robot challenge"
                    else:
                        return (
                            VerificationStatus.CHALLENGE_REQUIRED,
                            "Robot verification challenge ('Confirm you're not a robot') timed out",
                        )
                elif "myaccount.google.com" in self.driver.current_url or "gds.google.com" in self.driver.current_url:
                    return VerificationStatus.SUCCESS_VERIFIED, "Session already active"
                else:
                    return VerificationStatus.ERROR, "Password input field not encountered"

            self._ensure_active_window()
            # Check for invalid credentials
            current_src = self.driver.page_source.lower()
            if "wrong password" in current_src or "incorrect password" in current_src:
                return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password provided"

            # Check if Google presented 'Confirm you're not a robot' after password
            if self._is_bot_challenge_present():
                resolved = self._wait_for_bot_challenge_resolution(account.email)
                if not resolved:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "Robot verification challenge ('Confirm you're not a robot') timed out",
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
                ]
            ):
                logger.warning(
                    f"2FA/Phone prompt detected for {account.email}. Waiting up to {self.settings.challenge_timeout_seconds}s for resolution..."
                )
                resolved = self._wait_for_challenge_resolution(timeout=self.settings.challenge_timeout_seconds)
                if not resolved:
                    return VerificationStatus.CHALLENGE_REQUIRED, "Security verification or 2FA required"

            return VerificationStatus.SUCCESS_VERIFIED, "Google authentication successful"

        except Exception as e:
            return VerificationStatus.ERROR, f"Google login error: {str(e)}"

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
                time.sleep(0.02)
            time.sleep(0.3)

            pass_input.send_keys(Keys.ENTER)
            time.sleep(0.5)

            if "challenge" not in self.driver.current_url.lower() and "myaccount" not in self.driver.current_url.lower():
                self._click_next("passwordNext")

            time.sleep(self.settings.action_delay_seconds * 2)
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

    def _handle_oauth_consent(self):
        """Attempts to click 'Allow', 'Continue', or consent buttons on Google OAuth dialogs."""
        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                text = btn.text.strip().lower()
                if text in ["continue", "allow", "i agree", "accept", "get started"]:
                    logger.info(f"Clicking consent/activation button: '{btn.text}'")
                    btn.click()
                    time.sleep(2)
                    break
        except Exception:
            pass

    def _check_antigravity_status(self) -> Tuple[VerificationStatus, str]:
        """Examines the portal page to determine verification / access state."""
        src = self.driver.page_source.lower()
        title = self.driver.title.lower()

        # Check for forbidden markers
        forbidden_markers = [
            "access forbidden",
            "access_forbidden",
            "403 forbidden",
            "forbidden",
            "you do not have access to antigravity",
            "permission denied",
            "access denied",
        ]
        for marker in forbidden_markers:
            if marker in src or marker in title:
                return (
                    VerificationStatus.ACCESS_FORBIDDEN,
                    "Antigravity access is forbidden for this account (whitelist/enrollment required)",
                )

        return (
            VerificationStatus.SUCCESS_VERIFIED,
            "Account verified and Antigravity portal accessible",
        )
