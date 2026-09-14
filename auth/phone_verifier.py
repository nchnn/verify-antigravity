# File: auth/phone_verifier.py
# Purpose: Automated resolution and handling for Antigravity one-time phone / device verification

import re
import time
import json
import logging
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, Tuple
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from config.settings import get_settings
from loaders.account_loader import Account

logger = logging.getLogger("AntigravityAuditor")


class PhoneValidationHandler:
    """
    Handles or bypasses one-time phone / device verification (VALIDATION_REQUIRED)
    encountered during Antigravity onboarding and loadCodeAssist checks.
    """

    SUCCESS_MARKERS = [
        "auth_success_gemini",
        "auth_success",
        "myaccount.google.com",
        "cloudcode",
    ]

    CONSENT_BUTTON_TEXTS = [
        "continue",
        "i agree",
        "agree and continue",
        "accept",
        "get started",
        "next",
        "submit",
        "confirm",
    ]

    def __init__(self, driver=None):
        self.driver = driver
        self.settings = get_settings()

    @staticmethod
    def normalize_validation_url(url: str, email: Optional[str] = None) -> str:
        """
        Cleans and fixes Google validation URLs.
        Google Cloud Code returns URLs with trailing naked '&authuser', which
        causes accounts.google.com to discard the session and redirect to identifierId.
        Appends '=0' or '=email' to preserve the active profile session.
        """
        if not url:
            return ""

        fixed = url.strip()
        if fixed.endswith("&authuser"):
            fixed = fixed + "=0"
        elif "&authuser=" not in fixed and "?authuser=" not in fixed:
            separator = "&" if "?" in fixed else "?"
            fixed = f"{fixed}{separator}authuser=0"

        return fixed

    def extract_qr_challenge(self) -> Optional[Dict[str, str]]:
        """
        Detects if Google is presenting the idvanyphonechallengeqrcode screen
        and extracts the embedded base64 QR code and phone verification URL.
        """
        if not self.driver:
            return None

        try:
            current_url = (self.driver.current_url or "").lower()
            page_src = self.driver.page_source or ""

            is_qr_page = "challenge/iap/qrcode" in current_url or "idvanyphonechallengeqrcode" in page_src
            if not is_qr_page:
                return None

            details: Dict[str, str] = {
                "type": "qr_code",
                "page_url": self.driver.current_url,
                "deep_link": "",
                "image_base64": "",
            }

            qr_imgs = self.driver.find_elements(
                By.CSS_SELECTOR, "img[alt*='QR code'], img.pSHvwe, img[src^='data:image/png']"
            )
            for img in qr_imgs:
                src = img.get_attribute("src") or ""
                if "data:image" in src:
                    details["image_base64"] = src
                    break

            deep_link_match = re.search(
                r'https://accounts\.google\.com/devicephoneverification/begin\?[^\s"\'<>]+',
                page_src,
            )
            if deep_link_match:
                raw_link = deep_link_match.group(0)
                clean_link = (
                    raw_link.replace(r"\u003d", "=")
                    .replace(r"\u0026", "&")
                    .replace("&amp;", "&")
                )
                details["deep_link"] = clean_link

            return details
        except Exception as e:
            logger.debug(f"Error inspecting QR challenge: {e}")
            return None

    def click_consent_or_next(self) -> bool:
        """Finds and clicks any terms, upleveling confirmation, or Next buttons."""
        if not self.driver:
            return False

        try:
            buttons = self.driver.find_elements(By.TAG_NAME, "button")
            for btn in buttons:
                if not btn.is_displayed():
                    continue
                txt = (btn.text or "").strip().lower()
                if any(target in txt for target in self.CONSENT_BUTTON_TEXTS):
                    logger.info(f"Clicking validation consent button: '{btn.text}'")
                    self.driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                    return True

            for cid in ["identifierNext", "passwordNext", "confirmNext"]:
                elems = self.driver.find_elements(By.ID, cid)
                for elem in elems:
                    if elem.is_displayed():
                        logger.info(f"Clicking next container: #{cid}")
                        elem.click()
                        time.sleep(2)
                        return True
        except Exception as e:
            logger.debug(f"Consent click attempt error: {e}")

        return False

    def handle_phone_number_input(self, account: Account) -> bool:
        """Detects phone number input fields during verification."""
        if not self.driver:
            return False

        try:
            phone_inputs = self.driver.find_elements(
                By.CSS_SELECTOR, "input[type='tel'], input#phoneNumberId, input[name='phoneNumber']"
            )
            phone_field = next((p for p in phone_inputs if p.is_displayed()), None)
            if not phone_field:
                return False

            logger.info(f"Phone number input screen encountered for {account.email}.")
            # ponytail: Automated SMS provider integration ceiling; add full webhook matrix when needed
            return False
        except Exception as e:
            logger.debug(f"Phone form check error: {e}")
            return False

    def is_success_url(self, url: str) -> bool:
        """
        Checks whether the current URL represents true completion of validation.

        IMPORTANT: The validation URL itself contains 'auth_success_gemini' and other
        success markers inside its '&continue=' query parameter. We must only match
        against the URL host (netloc) and path — never the raw query string — to avoid
        a false-positive that fires the moment Chrome loads the validation page.
        """
        if not url:
            return False
        try:
            parsed = urllib.parse.urlparse(url.lower())
            host = parsed.netloc      # e.g. "developers.google.com"
            path = parsed.path        # e.g. "/gemini-code-assist/auth/auth_success_gemini"
            host_path = host + path   # compare only against host + path
        except Exception:
            host_path = url.lower()

        # accounts.google.com/signin/continue is where we START — never a success URL.
        # Reject it explicitly so the query-param substring can never match.
        if "accounts.google.com/signin/continue" in host_path:
            return False

        HOST_PATH_SUCCESS_MARKERS = [
            "developers.google.com/gemini-code-assist/auth/auth_success_gemini",
            "developers.google.com/gemini-code-assist/auth/auth_success",
            "myaccount.google.com",
            "cloudcode",
        ]
        return any(marker in host_path for marker in HOST_PATH_SUCCESS_MARKERS)

    def resolve_validation(
        self,
        account: Account,
        validation_url: str,
        timeout: Optional[int] = None,
    ) -> bool:
        """
        Automates resolution of the validation URL.
        Navigates within the authenticated browser session, handles on-screen consent,
        monitors for QR / phone completion, and waits for successful verification redirect.
        """
        if not self.driver or not validation_url:
            return False

        norm_url = self.normalize_validation_url(validation_url, account.email)
        wait_seconds = timeout or self.settings.phone_validation_timeout_seconds

        logger.info(f"Navigating to Antigravity validation URL for: {account.email}")
        try:
            self.driver.get(norm_url)
            time.sleep(self.settings.action_delay_seconds)
        except Exception as e:
            logger.error(f"Failed to navigate to validation URL: {e}")
            return False

        start_time = time.time()
        last_logged = 0

        while time.time() - start_time < wait_seconds:
            try:
                current_url = (self.driver.current_url or "").lower()

                if self.is_success_url(current_url):
                    logger.info(f"[OK] Validation successfully completed for {account.email}")
                    return True

                if "uplevelingstep" in current_url or "signin" in current_url or "consent" in current_url:
                    clicked = self.click_consent_or_next()
                    if clicked:
                        time.sleep(2)
                        continue

                self.handle_phone_number_input(account)

                qr_info = self.extract_qr_challenge()
                if qr_info:
                    elapsed = int(time.time() - start_time)
                    if elapsed > 0 and elapsed % 20 == 0 and elapsed != last_logged:
                        deep_link = qr_info.get("deep_link")
                        print(f"\n[!] ONE-TIME VERIFICATION REQUIRED FOR: {account.email}")
                        if deep_link:
                            print(f"[!] Direct Verification Link: {deep_link}")
                        else:
                            print("[!] Please scan the QR code displayed in Chrome to verify device.")
                        print(f"[*] Waiting for confirmation... ({elapsed}/{wait_seconds}s)\n")
                        last_logged = elapsed

                if any(marker in current_url for marker in ["myaccount.google.com", "accounts.google.com/checkcookie"]):
                    logger.info(f"[OK] Account session authenticated: {current_url}")
                    return True

            except Exception as e:
                logger.debug(f"Validation polling error: {e}")

            time.sleep(2)

        logger.warning(f"Validation flow timed out after {wait_seconds}s for {account.email}")
        return False
