import time
import logging
import random
from typing import Optional

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

logger = logging.getLogger("AntigravityAuditor")


_ACCOUNT_NOT_FOUND_PHRASES = [
    "couldn't find your google account",
    "enter a valid email",
    "find your google account",
    "no account found",
    "account doesn't exist",
]

_WRONG_PASSWORD_PHRASES = [
    "your password was changed",
    "wrong password",
    "incorrect password",
    "wrong password. try again",
]

_TWO_FA_PHRASES = [
    "2-step verification",
    "verify it's you",
    "check your phone",
    "enter the code",
    "confirm your recovery phone",
    "verify your info to continue",
    "scan the qr code",
]


def wait_for_transition(driver, timeout: float = 8.0, poll: float = 0.3) -> str:
    """
    Wait up to `timeout` seconds (polling every `poll` seconds) for Google's
    page to transition after a form submit. Returns a string token as soon as
    any outcome is detected, or "timeout" if nothing fires within `timeout`.

    Tokens:
        "not_found"    — account does not exist
        "rejected"     — security block (headless/automation detected)
        "bot"          — reCAPTCHA / robot challenge
        "qr"           — QR code verification challenge
        "2fa"          — 2-step / phone / recovery challenge
        "wrong_pass"   — wrong password error
        "authed"       — already authenticated (myaccount / gds session)
        "password"     — password input field appeared (email step succeeded)
        "timeout"      — none of the above within `timeout` seconds
    """
    try:
        driver.implicitly_wait(0)
    except Exception:
        pass

    def _condition(d):
        try:
            src = (d.page_source or "").lower()
            url = (d.current_url or "").lower()

            if any(p in src for p in _ACCOUNT_NOT_FOUND_PHRASES):
                return "not_found"

            if any(p in src for p in _WRONG_PASSWORD_PHRASES):
                return "wrong_pass"

            # Security rejection
            if (
                "signin/rejected" in url
                or "/rejected" in url
                or any(p in src for p in [
                    "this browser or app may not be secure",
                    "couldn't sign you in",
                    "try using a different browser",
                ])
            ):
                # Could still be "account not found" disguised as rejection
                if any(p in src for p in _ACCOUNT_NOT_FOUND_PHRASES):
                    return "not_found"
                return "rejected"

            if "myaccount.google.com" in url or "gds.google.com" in url:
                return "authed"

            # Bot challenge
            if "challenge/recaptcha" in url or "/recaptcha" in url:
                return "bot"
            if any(p in src for p in ["confirm you're not a robot", "not a robot"]):
                return "bot"
            try:
                if d.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha'], #recaptcha, .g-recaptcha"):
                    return "bot"
            except Exception:
                pass

            # QR challenge
            if "challenge/iap/qrcode" in url:
                return "qr"
            if any(p in src for p in ["scan the qr code with your phone", "scan the qr code"]):
                return "qr"

            # 2FA / phone challenge (exclude "scan the qr code" — already caught above)
            if any(p in src for p in _TWO_FA_PHRASES):
                return "2fa"

            # Password field appeared (email step done)
            try:
                d.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']")
                return "password"
            except Exception:
                pass

        except Exception:
            pass
        return False  # keep waiting

    try:
        result = WebDriverWait(driver, timeout, poll_frequency=poll).until(_condition)
    except TimeoutException:
        result = "timeout"
    finally:
        try:
            driver.implicitly_wait(10)
        except Exception:
            pass

    return result


def ensure_active_window(driver) -> bool:
    try:
        handles = driver.window_handles
        if not handles:
            return False
        try:
            _ = driver.current_window_handle
        except Exception:
            driver.switch_to.window(handles[-1])
        return True
    except Exception:
        return False


def current_url(driver) -> str:
    try:
        return (driver.current_url or "").lower()
    except Exception:
        return ""


def page_source(driver) -> str:
    try:
        return (driver.page_source or "").lower()
    except Exception:
        return ""


def is_security_rejection_present(driver) -> bool:
    try:
        url = current_url(driver)
        src = page_source(driver)

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


def is_bot_challenge_present(driver) -> bool:
    try:
        url = current_url(driver)
        src = page_source(driver)

        if "challenge/recaptcha" in url or "/recaptcha" in url:
            return True

        bot_phrases = [
            "confirm you're not a robot",
            "please verify that you are not a robot",
            "not a robot",
        ]
        if any(phrase in src for phrase in bot_phrases):
            return True

        if driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha'], #recaptcha, .g-recaptcha"):
            return True

        return False
    except Exception:
        return False


def is_qr_challenge_present(driver) -> bool:
    try:
        url = current_url(driver)
        src = page_source(driver)

        if "challenge/iap/qrcode" in url:
            return True

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


def wait_for_bot_challenge_resolution(driver, account_email: str, settings) -> bool:
    if settings.browser_headless:
        logger.error(
            f"[HEADLESS CAPTCHA] 'Confirm you're not a robot' triggered for {account_email} in headless mode. "
            f"Headless mode cannot solve on-screen CAPTCHAs interactively. "
            f"Please run with headed mode (set BROWSER_HEADLESS=false in .env or run without --headless) to solve."
        )
        return False

    wait_seconds = settings.challenge_timeout_seconds

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
        ensure_active_window(driver)
        cur_url = current_url(driver)
        cur_src = page_source(driver)

        if "challenge/recaptcha" not in cur_url and "confirm you're not a robot" not in cur_src:
            logger.info(f"[OK] Robot challenge resolved: page moved away from reCAPTCHA for {account_email}.")
            return True

        try:
            pw_elements = driver.find_elements(
                By.CSS_SELECTOR, "input[type='password'], input[name='Passwd']"
            )
            if any(el.is_displayed() for el in pw_elements):
                logger.info(f"[OK] Robot challenge resolved: Password field encountered for {account_email}.")
                return True
        except Exception:
            pass

        if any(marker in cur_url for marker in ["myaccount.google.com", "antigravity.google", "checkcookie", "gds.google.com"]):
            logger.info(f"[OK] Robot challenge resolved: Authenticated session detected for {account_email}.")
            return True

        elapsed = int(time.time() - start_time)
        if elapsed > 0 and elapsed % 15 == 0 and elapsed != last_logged:
            print(f"[*] Still waiting for robot verification resolution... ({elapsed}/{wait_seconds}s)")
            last_logged = elapsed

        time.sleep(2)

    logger.warning(f"[FAIL] Robot challenge timed out after {wait_seconds}s for {account_email}.")
    return False


def wait_for_qr_challenge_resolution(driver, account_email: str, settings) -> bool:
    wait_seconds = settings.challenge_timeout_seconds

    try:
        qr_url = driver.current_url
    except Exception:
        qr_url = "https://accounts.google.com/"

    logger.warning(
        f"[QR CHALLENGE] Google QR-code verification triggered for {account_email}. "
        f"Waiting up to {wait_seconds}s for resolution..."
    )

    print("\n" + "=" * 65)
    print(f"[!] QR CODE VERIFICATION REQUIRED FOR: {account_email}")
    print("=" * 65)
    print("[!] Google needs to verify your device via a QR code.")
    print("")
    if settings.browser_headless:
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
        ensure_active_window(driver)
        cur_url = current_url(driver)
        cur_src = page_source(driver)

        if "challenge/iap/qrcode" not in cur_url and "scan the qr code" not in cur_src:
            logger.info(f"[OK] QR challenge resolved: page moved away from QR screen for {account_email}.")
            return True

        auth_markers = [
            "myaccount.google.com",
            "accounts.google.com/checkcookie",
            "gds.google.com",
            "uplevelingstep",
            "auth_success",
        ]
        if any(marker in cur_url for marker in auth_markers):
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


def wait_for_challenge_resolution(driver, timeout: int = 60) -> bool:
    start_time = time.time()
    while time.time() - start_time < timeout:
        ensure_active_window(driver)
        cur_url = driver.current_url.lower()
        if (
            "myaccount.google.com" in cur_url
            or "accounts.google.com/checkcookie" in cur_url
            or "gds.google.com" in cur_url
        ):
            return True
        time.sleep(2)
    return False


def handle_oauth_consent(driver) -> None:
    try:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        for btn in buttons:
            text = btn.text.strip().lower()
            if text in ["continue", "allow", "i agree", "accept", "get started"]:
                logger.info(f"Clicking consent/activation button: '{btn.text}'")
                ActionChains(driver).move_to_element(btn).pause(random.uniform(0.1, 0.3)).click().perform()
                time.sleep(1.0)
                break
    except Exception:
        pass
