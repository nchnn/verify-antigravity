import time
import random
import logging
from typing import Tuple

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from loaders.account_loader import Account
from audit.auditor import VerificationStatus
from auth.challenge_handlers import (
    ensure_active_window,
    current_url,
    page_source,
    is_security_rejection_present,
    is_bot_challenge_present,
    is_qr_challenge_present,
    wait_for_bot_challenge_resolution,
    wait_for_qr_challenge_resolution,
    wait_for_challenge_resolution,
    wait_for_transition,
)

logger = logging.getLogger("AntigravityAuditor")


def click_next(driver, container_id: str):
    try:
        btn = driver.find_element(
            By.CSS_SELECTOR, f"#{container_id} button, #{container_id}"
        )
        ActionChains(driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
        return
    except Exception:
        pass

    try:
        btn = driver.find_element(
            By.XPATH, "//button[.//span[text()='Next'] or contains(., 'Next')]"
        )
        ActionChains(driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
        return
    except Exception:
        pass


def enter_password(driver, password: str) -> bool:
    try:
        pass_input = WebDriverWait(driver, 6).until(
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

        if "challenge" not in driver.current_url.lower() and "myaccount" not in driver.current_url.lower():
            time.sleep(random.uniform(0.2, 0.5))
            click_next(driver, "passwordNext")

        return True
    except Exception:
        return False


def perform_google_login(driver, account: Account, settings) -> Tuple[VerificationStatus, str, dict]:
    try:
        ensure_active_window(driver)
        driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
        time.sleep(1.0)
        ensure_active_window(driver)

        cur_url = current_url(driver)
        page_src = page_source(driver)
        if "toomanysessions" in cur_url or "maximum number of accounts" in page_src:
            logger.warning("Max accounts reached in session (toomanysessions). Resetting sessions...")
            try:
                here_link = driver.find_element(By.PARTIAL_LINK_TEXT, "Here")
                here_link.click()
                time.sleep(1.0)
            except Exception:
                driver.delete_all_cookies()
                driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
                time.sleep(1.0)
            ensure_active_window(driver)
            page_src = page_source(driver)

        if "usernamerecovery" in current_url(driver):
            driver.get("https://accounts.google.com/signin/v2/identifier?hl=en")
            time.sleep(1.0)
            ensure_active_window(driver)
            page_src = page_source(driver)

        if "choose an account" in page_src or "select an account" in page_src:
            try:
                acc_elem = driver.find_element(
                    By.XPATH, f"//*[contains(text(), '{account.email}')]"
                )
                acc_elem.click()
                time.sleep(0.8)
            except Exception:
                try:
                    use_another = driver.find_element(
                        By.XPATH, "//*[contains(text(), 'Use another account')]"
                    )
                    use_another.click()
                    time.sleep(0.8)
                except Exception:
                    pass
            ensure_active_window(driver)
            page_src = page_source(driver)

        try:
            email_input = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "input[type='email'], #identifierId")
                )
            )
            email_input.clear()
            for ch in account.email:
                email_input.send_keys(ch)
                time.sleep(random.uniform(0.02, 0.07))
            time.sleep(random.uniform(0.2, 0.5))

            email_input.send_keys(Keys.ENTER)
            time.sleep(0.3)

            if "identifier" in driver.current_url.lower() and "usernamerecovery" not in driver.current_url.lower():
                time.sleep(random.uniform(0.2, 0.5))
                click_next(driver, "identifierNext")

            time.sleep(1.0)
        except TimeoutException:
            pass

        ensure_active_window(driver)

        # --- Wait for Google to transition after email submit ---
        outcome = wait_for_transition(driver, timeout=8.0, poll=0.3)

        if outcome == "not_found":
            return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

        if outcome == "rejected":
            return (
                VerificationStatus.ERROR,
                "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                {},
            )

        if outcome == "authed":
            return VerificationStatus.SUCCESS_VERIFIED, "Session already active", {}

        if outcome == "bot":
            resolved = wait_for_bot_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "Robot verification challenge ('Confirm you're not a robot') timed out",
                    {},
                )
            # challenge resolved — fall through to password

        elif outcome == "qr":
            resolved = wait_for_qr_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "QR code verification challenge timed out — please scan the QR code on your phone and retry",
                    {},
                )

        elif outcome == "2fa":
            logger.warning(
                f"2FA/Phone prompt detected for {account.email}. Waiting up to {settings.challenge_timeout_seconds}s for resolution..."
            )
            resolved = wait_for_challenge_resolution(driver, timeout=settings.challenge_timeout_seconds)
            if not resolved:
                return VerificationStatus.CHALLENGE_REQUIRED, "Security verification or 2FA required", {}

        elif outcome == "timeout":
            cur_url = current_url(driver)
            logger.info(
                f"No password field after email submit for {account.email}. "
                f"URL: {cur_url[:120]}"
            )
            return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

        # outcome == "password" — fall through to enter password

        password_entered = enter_password(driver, account.password)
        ensure_active_window(driver)

        # --- Wait for Google to transition after password submit ---
        post_outcome = wait_for_transition(driver, timeout=8.0, poll=0.3)

        if post_outcome == "wrong_pass":
            return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password provided", {}

        if post_outcome == "not_found":
            return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

        if post_outcome == "authed":
            return VerificationStatus.SUCCESS_VERIFIED, "Google authentication successful", {}

        if post_outcome == "password":
            # Password field still visible — could be a brief DOM transition or a real rejection.
            # Distinguish by URL: if still on identifier page, Google rejected credentials.
            # If URL moved away, the field is a DOM remnant during animation — treat as success.
            cur_url = current_url(driver)
            if "accounts.google.com/v3/signin/identifier" in cur_url or "accounts.google.com/signin/v2/identifier" in cur_url:
                return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password or account not found", {}

        if post_outcome == "bot":
            resolved = wait_for_bot_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "Robot verification challenge ('Confirm you're not a robot') timed out",
                    {},
                )
            # resolved — fall through to success

        elif post_outcome == "qr":
            resolved = wait_for_qr_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "QR code verification challenge timed out — please scan the QR code on your phone and retry",
                    {},
                )

        elif post_outcome == "2fa":
            logger.warning(
                f"2FA/Phone prompt detected for {account.email}. Waiting up to {settings.challenge_timeout_seconds}s for resolution..."
            )
            resolved = wait_for_challenge_resolution(driver, timeout=settings.challenge_timeout_seconds)
            if not resolved:
                return VerificationStatus.CHALLENGE_REQUIRED, "Security verification or 2FA required", {}

        elif post_outcome == "rejected":
            return (
                VerificationStatus.ERROR,
                "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                {},
            )

        elif post_outcome == "timeout":
            if not password_entered:
                return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}
            # Password was entered but page didn't resolve cleanly within 8s.
            # Do a final explicit check before assuming success.
            ensure_active_window(driver)
            final_src = (driver.page_source or "").lower()
            final_url = current_url(driver)
            if any(p in final_src for p in ["wrong password", "incorrect password", "wrong password. try again"]):
                return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password provided", {}
            if any(p in final_src for p in [
                "couldn't find your google account", "enter a valid email",
                "find your google account", "no account found", "account doesn't exist",
            ]):
                return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}
            if "myaccount.google.com" not in final_url and "gds.google.com" not in final_url:
                logger.info(
                    f"No clear post-password outcome for {account.email}. URL: {final_url[:120]}"
                )
                return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

        return VerificationStatus.SUCCESS_VERIFIED, "Google authentication successful", {}

    except Exception as e:
        return VerificationStatus.ERROR, f"Google login error: {str(e)}", {}
