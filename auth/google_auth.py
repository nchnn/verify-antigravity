import time
import logging
from typing import Tuple

from selenium.webdriver.support.ui import WebDriverWait

from config.settings import get_settings
from loaders.account_loader import Account
from audit.auditor import VerificationStatus
from auth.challenge_handlers import ensure_active_window, current_url, page_source
from auth.google_login import perform_google_login
from auth.oauth_flow import verify_via_cloudcode_api

logger = logging.getLogger("AntigravityAuditor")


class GoogleAuthVerifier:

    def __init__(self, driver):
        self.driver = driver
        self.settings = get_settings()
        self.wait = WebDriverWait(self.driver, self.settings.page_timeout_seconds)

    def login_and_verify(self, account: Account) -> Tuple[VerificationStatus, str, dict]:
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

        is_already_authed = False
        try:
            ensure_active_window(self.driver)
            self.driver.get("https://accounts.google.com/")
            time.sleep(1.0)
            ensure_active_window(self.driver)

            cur_url = current_url(self.driver)
            page_src = page_source(self.driver)

            if (
                "myaccount.google.com" in cur_url
                or "choose an account" in page_src
                or "select an account" in page_src
                or ("accounts.google.com" in cur_url and account.email.lower() in page_src)
            ):
                logger.info(f"Active Google session detected via page state for: {account.email}")
                is_already_authed = True
            else:
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

        if not is_already_authed:
            login_status, login_msg, _ = perform_google_login(self.driver, account, self.settings)
            if login_status != VerificationStatus.SUCCESS_VERIFIED:
                return login_status, login_msg, {}

        logger.info(f"Starting Antigravity API verification for: {account.email}")
        status, message, token_data = verify_via_cloudcode_api(self.driver, account, self.settings)
        return status, message, token_data
