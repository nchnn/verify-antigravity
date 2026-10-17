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

        time.sleep(1.5)
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

        current_src = driver.page_source.lower()
        if "couldn't find your google account" in current_src or "enter a valid email" in current_src:
            return VerificationStatus.INVALID_CREDENTIALS, "Google account not found", {}

        if is_security_rejection_present(driver):
            return (
                VerificationStatus.ERROR,
                "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                {},
            )

        if is_bot_challenge_present(driver):
            resolved = wait_for_bot_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "Robot verification challenge ('Confirm you're not a robot') timed out",
                    {},
                )

        password_entered = enter_password(driver, account.password)
        if not password_entered:
            ensure_active_window(driver)
            if is_security_rejection_present(driver):
                return (
                    VerificationStatus.ERROR,
                    "Google blocked sign-in ('This browser or app may not be secure' - headless/automation detected)",
                    {},
                )
            if is_bot_challenge_present(driver):
                resolved = wait_for_bot_challenge_resolution(driver, account.email, settings)
                if resolved:
                    password_entered = enter_password(driver, account.password)
                    if not password_entered:
                        if "myaccount.google.com" in driver.current_url or "gds.google.com" in driver.current_url:
                            return VerificationStatus.SUCCESS_VERIFIED, "Session already active", {}
                        return VerificationStatus.ERROR, "Password input field not encountered after robot challenge", {}
                else:
                    return (
                        VerificationStatus.CHALLENGE_REQUIRED,
                        "Robot verification challenge ('Confirm you're not a robot') timed out",
                        {},
                    )
            elif "myaccount.google.com" in driver.current_url or "gds.google.com" in driver.current_url:
                return VerificationStatus.SUCCESS_VERIFIED, "Session already active", {}
            else:
                return VerificationStatus.ERROR, "Password input field not encountered", {}

        ensure_active_window(driver)
        current_src = driver.page_source.lower()
        if "wrong password" in current_src or "incorrect password" in current_src:
            return VerificationStatus.INVALID_CREDENTIALS, "Incorrect password provided", {}

        if is_bot_challenge_present(driver):
            resolved = wait_for_bot_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "Robot verification challenge ('Confirm you're not a robot') timed out",
                )

        if is_qr_challenge_present(driver):
            resolved = wait_for_qr_challenge_resolution(driver, account.email, settings)
            if not resolved:
                return (
                    VerificationStatus.CHALLENGE_REQUIRED,
                    "QR code verification challenge timed out — please scan the QR code on your phone and retry",
                    {},
                )

        current_src = driver.page_source.lower()
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
                f"2FA/Phone prompt detected for {account.email}. Waiting up to {settings.challenge_timeout_seconds}s for resolution..."
            )
            resolved = wait_for_challenge_resolution(driver, timeout=settings.challenge_timeout_seconds)
            if not resolved:
                return VerificationStatus.CHALLENGE_REQUIRED, "Security verification or 2FA required", {}

        return VerificationStatus.SUCCESS_VERIFIED, "Google authentication successful", {}

    except Exception as e:
        return VerificationStatus.ERROR, f"Google login error: {str(e)}", {}
