# File: main.py
# Created: 2026-09-12 11:57 PM
# Last Modified: 2026-09-13 04:12 AM

import re
import json
import time
import argparse
import sys
import concurrent.futures
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output encoding on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config.settings import get_settings
from loaders.account_loader import AccountLoader, Account
from audit.auditor import AccountAuditor, VerificationStatus
from core.browser import BrowserManager
from auth.google_auth import GoogleAuthVerifier
from auth.token_extractor import TokenExtractor
from auth.profile_extractor import ProfileCookieExtractor


def parse_args():
    parser = argparse.ArgumentParser(
        description="Antigravity Account Verification & Google OAuth Automation Suite"
    )
    parser.add_argument(
        "-a",
        "--account",
        type=str,
        default=None,
        help="Target a specific email from accounts.txt (or all accounts if omitted)",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List loaded accounts and password resolution without running automation",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default is headed for interactive handling)",
    )
    parser.add_argument(
        "--accounts-file",
        type=str,
        default=None,
        help="Path to custom accounts text file (default: accounts.txt)",
    )
    parser.add_argument(
        "--no-extract-tokens",
        action="store_true",
        help="Disable automatic extraction of tokens and cookies upon verification",
    )
    return parser.parse_args()


def display_account_list(accounts: list[Account]):
    print(f"\nFound {len(accounts)} accounts:")
    print("-" * 60)
    for i, acc in enumerate(accounts, start=1):
        pw_type = "DEFAULT FALLBACK" if acc.is_fallback_password else "CUSTOM"
        print(f"[{i:02d}] {acc.email:<35} | {pw_type:<16} | Pass: {acc.mask_password()}")
    print("-" * 60 + "\n")


def _email_already_extracted(email: str, settings) -> bool:
    """
    Returns True if this email already has tokens saved in verified_tokens.json
    with at least one primary Google cookie present.
    """
    json_path = settings.base_dir / getattr(settings, "tokens_output_json", "verified_tokens.json")
    if not json_path.exists():
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(email)
        if not entry:
            return False
        # Must have at least SAPISID or SID in cookie_dict to be considered valid
        cookie_dict = entry.get("cookie_dict", {})
        return bool(cookie_dict.get("SAPISID") or cookie_dict.get("SID"))
    except Exception:
        return False


def _profile_exists(email: str, settings) -> bool:
    """
    Returns True if a chrome_profiles folder already exists for this email.
    The folder name matches BrowserManager.get_profile_path() sanitization.
    """
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", email.lower())
    profile_dir = settings.profiles_dir / safe_name
    return profile_dir.exists()


def _force_kill_chrome():
    """Kills ALL running Chrome processes — used when a launch hangs or after a crash."""
    try:
        import psutil
        killed = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and "chrome" in proc.info["name"].lower():
                    proc.kill()
                    killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed:
            print(f"[~] Killed {killed} lingering Chrome process(es).")
        time.sleep(1.5)  # Give OS time to release file locks
    except ImportError:
        pass


def _launch_driver_with_timeout(browser_manager, account_email: str, timeout: int):
    """
    Launches Chrome in a background thread with a strict timeout.
    Raises TimeoutError if Chrome doesn't open within `timeout` seconds.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser_manager.create_driver, account_email)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"Chrome launch timed out after {timeout}s — process may be stuck or locked."
            )

def _try_dpapi_extract(
    account_email: str,
    settings,
    extractor: TokenExtractor,
) -> bool:
    """
    Attempts to extract auth cookies directly from the Chrome profile using
    Windows DPAPI + AES-256-GCM decryption — no browser launch needed.

    Returns True if cookies were successfully extracted and saved.
    """
    import re as _re
    from datetime import datetime

    safe_name = _re.sub(r"[^a-zA-Z0-9_.-]", "_", account_email.lower())
    profile_dir = settings.profiles_dir / safe_name

    if not profile_dir.exists():
        return False

    print(f"[*] Inspecting profile and decrypting cookies for: {account_email}")
    pe = ProfileCookieExtractor(profile_dir)
    cookie_dict = pe.extract_cookies()

    if not cookie_dict:
        print(f"[~] Profile directory exists for {account_email}, but no active Google session found in SQLite. Sign-in required.")
        return False

    # Build a session_info compatible with TokenExtractor.save_session()
    cookie_header = pe.build_cookie_header(cookie_dict)
    tokens = {
        f"COOKIE_{k}": v
        for k, v in cookie_dict.items()
        if k in ["SAPISID", "SID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "HSID", "APISID"]
    }

    session_info = {
        "email": account_email,
        "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "portal_url": "(extracted directly from profile - Chrome bypassed)",
        "cookies": cookie_header,
        "cookie_header": cookie_header,
        "cookie_dict": cookie_dict,
        "tokens": tokens,
        "storage": {},
        "source": "dpapi_profile",
    }

    extractor.save_session(session_info)
    print(
        f"[OK] Direct profile extraction successful: {len(cookie_dict)} cookies saved "
        f"for {account_email} (Chrome launch bypassed!)"
    )
    return True



def process_account(
    account,
    browser_manager,
    auditor,
    loader,
    extractor,
    extract_tokens: bool = True,
):
    settings = browser_manager.settings
    max_retries = settings.max_retries_per_account
    launch_timeout = settings.chrome_launch_timeout_seconds

    # -- STEP 1: Skip Chrome - tokens already in verified_tokens.json ---------
    if _email_already_extracted(account.email, settings):
        print(f"[OK] SKIPPING Chrome - tokens already extracted for: {account.email}")
        auditor.log_status(
            email=account.email,
            status=VerificationStatus.ALREADY_VERIFIED,
            details="Tokens already present in verified_tokens.json - Chrome not launched.",
            duration_seconds=0.0,
        )
        loader.remove_account(account.email)
        return

    # -- STEP 2: DPAPI - extract directly from profile, no Chrome needed -------
    if _profile_exists(account.email, settings) and extract_tokens:
        dpapi_ok = _try_dpapi_extract(account.email, settings, extractor)
        if dpapi_ok:
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details="Session cookies extracted via DPAPI from profile - Chrome not launched.",
                duration_seconds=0.0,
            )
            loader.remove_account(account.email)
            return
        else:
            print(f"[~] Falling back to Chrome launch for sign-in: {account.email}")

    # -- STEP 3: Retry loop - Chrome launch as last resort ---------------------
    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        start_time = time.time()
        driver = None

        try:
            if attempt > 1:
                print(f"[RETRY] Retry {attempt}/{max_retries} for: {account.email}")
                _force_kill_chrome()
                time.sleep(3)

            # Timeout-guarded Chrome launch
            driver = _launch_driver_with_timeout(browser_manager, account.email, launch_timeout)

            from auth.google_auth import GoogleAuthVerifier
            verifier = GoogleAuthVerifier(driver)
            status, details = verifier.login_and_verify(account)
            duration = time.time() - start_time

            auditor.log_status(
                email=account.email,
                status=status,
                details=details,
                duration_seconds=duration,
            )

            if status in (VerificationStatus.SUCCESS_VERIFIED, VerificationStatus.ALREADY_VERIFIED):
                if extract_tokens and driver:
                    try:
                        session_info = extractor.extract_session_data(driver, account.email)
                        extractor.save_session(session_info)
                        print(f"[*] Tokens & cookies extracted: {account.email} -> verified_tokens.json, verified_tokens.txt")
                    except Exception as ex:
                        print(f"[!] Warning: Token extraction failed for {account.email}: {ex}")

                # Ensure Chrome flushes cookies to its profile on disk BEFORE closing (No force-kill)
                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path and driver:
                    print(f"[*] Ensuring cookies are saved to profile on disk: {profile_path.name}")
                    flushed = browser_manager.flush_and_close(profile_path)
                    if flushed:
                        print(f"[OK] Authentication cookies verified on disk in profile: {account.email}")
                    else:
                        print(f"[~] Cookie flush wait finished for {account.email}")

                removed = loader.remove_account(account.email)
                if removed:
                    print(f"[*] Verified: {account.email} automatically removed from {loader.file_path.name}")

            elif status == VerificationStatus.ACCESS_FORBIDDEN and driver:
                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path:
                    browser_manager.flush_and_close(profile_path)

            return  # <- success or definitive non-retryable status, exit loop

        except TimeoutError as te:
            last_error = str(te)
            print(f"[!] Chrome launch TIMED OUT on attempt {attempt}/{max_retries} for {account.email}.")
            _force_kill_chrome()

        except Exception as e:
            last_error = str(e)
            print(f"[!] Error on attempt {attempt}/{max_retries} for {account.email}: {e}")

        finally:
            browser_manager.close()

    # All retries exhausted
    duration = time.time() - start_time if 'start_time' in locals() else 0.0
    print(f"[FAIL] All {max_retries} attempts failed for {account.email}. Marking as ERROR.")
    auditor.log_status(
        email=account.email,
        status=VerificationStatus.ERROR,
        details=f"Failed after {max_retries} retries. Last error: {last_error}",
        duration_seconds=duration,
    )


def main():
    args = parse_args()
    settings = get_settings()
    loader = AccountLoader(args.accounts_file)
    auditor = AccountAuditor()
    extractor = TokenExtractor()

    extract_enabled = settings.extract_tokens and not args.no_extract_tokens

    try:
        accounts = loader.load_accounts(target_email=args.account)
    except Exception as e:
        print(f"Error loading accounts: {e}")
        sys.exit(1)

    if not accounts:
        print("No matching accounts found to process.")
        sys.exit(0)

    if args.list:
        display_account_list(accounts)
        sys.exit(0)

    print("\n" + "=" * 60)
    print("STARTING ANTIGRAVITY ACCOUNT VERIFICATION")
    print(f"Target count: {len(accounts)}")
    print(f"Headless mode: {args.headless or settings.browser_headless}")
    print(f"Extract tokens/cookies: {extract_enabled}")
    print("=" * 60 + "\n")

    browser_manager = BrowserManager(headless=args.headless or settings.browser_headless)

    try:
        for index, acc in enumerate(accounts, start=1):
            print(f"\n>>> Processing [{index}/{len(accounts)}]: {acc.email}")
            process_account(
                account=acc,
                browser_manager=browser_manager,
                auditor=auditor,
                loader=loader,
                extractor=extractor,
                extract_tokens=extract_enabled,
            )
            time.sleep(settings.action_delay_seconds)
    except KeyboardInterrupt:
        print("\nVerification process interrupted by user.")
    finally:
        # Export reports and display tabular audit summary
        auditor.export_reports()
        auditor.print_summary()


if __name__ == "__main__":
    main()
