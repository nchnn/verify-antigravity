
import re
import time
import json
import random
import logging
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any, Tuple
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from config.settings import get_settings
from loaders.account_loader import Account

logger = logging.getLogger("AntigravityAuditor")


class PhoneValidationHandler:

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
                    ActionChains(self.driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
                    time.sleep(2)
                    return True

            for cid in ["identifierNext", "passwordNext", "confirmNext"]:
                elems = self.driver.find_elements(By.ID, cid)
                for elem in elems:
                    if elem.is_displayed():
                        logger.info(f"Clicking next container: #{cid}")
                        ActionChains(self.driver).move_to_element(elem).pause(random.uniform(0.1, 0.3)).click().perform()
                        time.sleep(2)
                        return True
        except Exception as e:
            logger.debug(f"Consent click attempt error: {e}")

        return False

    def handle_phone_number_input(self, account: Account) -> bool:
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
            return False
        except Exception as e:
            logger.debug(f"Phone form check error: {e}")
            return False

    def is_success_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urllib.parse.urlparse(url.lower())
            host = parsed.netloc
            path = parsed.path
            host_path = host + path
        except Exception:
            host_path = url.lower()

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
