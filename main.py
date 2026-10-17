# File: main.py
# Created: 2026-09-12 11:57 PM
# Last Modified: 2026-09-14 05:18 PM

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
    Returns True if this email already has valid tokens saved in verified_tokens.json.

    Accepts either:
      1. Full OAuth extraction: ya29 access token + cookies  (source: browser_cdp)
      2. DPAPI cookie-only extraction: valid SAPISID/SID     (source: dpapi_profile)

    Both are considered "extracted" — the difference is that dpapi_profile entries
    won't have OAuth tokens, so 9router won't be synced. But the cookies are valid.
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

        # Check if we have OAuth tokens (full extraction)
        at = entry.get("accessToken", "")
        if at and at.startswith("ya29."):
            cookie_dict = entry.get("cookie_dict", {})
            has_cookies = bool(cookie_dict.get("SAPISID") or cookie_dict.get("SID") or entry.get("cookies"))
            return has_cookies

        # Check if we have valid cookies from DPAPI extraction (cookies-only is also valid)
        cookie_dict = entry.get("cookie_dict", {})
        if cookie_dict.get("SAPISID") or cookie_dict.get("SID"):
            return True

        return False
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
        time.sleep(1.0)  # Give OS time to release file locks
    except ImportError:
        pass


def _launch_driver_with_timeout(browser_manager, account, timeout: int):
    """
    Launches Chrome in a background thread with a strict timeout.
    Raises TimeoutError if Chrome doesn't open within `timeout` seconds.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser_manager.create_driver, account)
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

    Builds a complete session_info with every field 9router / OmniRoute needs:
      - cookie_dict    → ALL decrypted Google auth cookies
      - cookies        → HTTP Cookie header string
      - tokens         → COOKIE_* entries for every auth cookie found
      - accessToken    → empty string (not available from profile — requires OAuth)
      - refreshToken   → empty string (not available from profile — requires OAuth)
      - source         → "dpapi_profile" (so downstream can tell this was offline extraction)

    NOTE: OAuth tokens (accessToken/refreshToken) are NOT in Chrome profiles.
    This extraction provides cookies only. 9router needs OAuth tokens for full
    API connectivity — a browser OAuth flow is still required for those.

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

    # Build a session_info with ALL fields 9router / OmniRoute expects
    cookie_header = pe.build_cookie_header(cookie_dict)

    # Include COOKIE_* token entries for every Google auth cookie found
    all_auth_cookie_names = [
        "SAPISID", "SID", "__Secure-1PSID", "__Secure-3PSID", "SSID", "HSID",
        "APISID", "__Secure-1PAPISID", "__Secure-3PAPISID", "SIDCC",
        "__Secure-1PSIDCC", "__Secure-3PSIDCC", "__Secure-1PSIDTS",
        "__Secure-3PSIDTS", "LSID", "OSID", "__Secure-OSID",
        "__Host-1PLSID", "__Host-3PLSID", "__Host-GAPS", "__Host-GAPSTS",
    ]
    tokens = {
        f"COOKIE_{k}": v
        for k, v in cookie_dict.items()
        if k in all_auth_cookie_names
    }

    session_info = {
        "email": account_email,
        "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "portal_url": "(extracted directly from profile — Chrome bypassed)",
        "cookies": cookie_header,
        "cookie_header": cookie_header,
        "cookie_dict": cookie_dict,
        "tokens": tokens,
        "storage": {},
        # OAuth tokens — not available from Chrome profile, require browser OAuth flow
        "accessToken": "",
        "refreshToken": "",
        "source": "dpapi_profile",
    }

    extractor.save_session(session_info)
    print(
        f"[OK] Direct profile extraction successful: {len(cookie_dict)} cookies saved "
        f"for {account_email} (Chrome launch bypassed!)"
    )
    print(
        f"[~] NOTE: OAuth tokens (accessToken/refreshToken) are NOT in the Chrome profile. "
        f"9router / OmniRoute needs a browser OAuth flow to obtain them."
    )
    return True



def process_account(
    account,
    browser_manager,
    auditor,
    loader,
    extractor,
    extract_tokens: bool = True,
    skipped_credentials: list = None,
):
    settings = browser_manager.settings
    max_retries = settings.max_retries_per_account
    launch_timeout = settings.chrome_launch_timeout_seconds

    # -- STEP 1: Validate existing tokens & refresh if expired --------
    if _email_already_extracted(account.email, settings):
        is_valid, detail = extractor.validate_and_refresh_stored_token(account.email)
        if is_valid:
            print(f"[OK] SKIPPING Chrome — {detail}: {account.email}")
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details=f"Token validated/refreshed — Chrome not launched. {detail}",
                duration_seconds=0.0,
            )
            loader.remove_account(account.email)
            return
        else:
            # Token expired and refresh failed — fall through to re-extract via OAuth
            print(f"[~] Stored token stale for {account.email}: {detail}")
            print(f"[~] Proceeding to Chrome OAuth for fresh extraction...")

    # -- STEP 2: Try DPAPI extraction from existing Chrome profile (no Chrome launch) --
    #    Extracts cookies directly from Default/Network/Cookies using DPAPI + AES-256-GCM.
    #    Saves cookies to verified_tokens.json but OAuth tokens will be empty — Chrome
    #    browser OAuth flow is still needed for accessToken/refreshToken (9router requirement).
    if extract_tokens and _profile_exists(account.email, settings):
        dpapi_ok = _try_dpapi_extract(account.email, settings, extractor)
        if dpapi_ok:
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details="Cookies extracted from Chrome profile via DPAPI (no browser launched). OAuth tokens still needed for 9router.",
                duration_seconds=0.0,
            )
            # Do NOT remove from accounts.txt — still needs OAuth flow for full 9router sync
            return

    # -- STEP 3: Chrome verification & OAuth token extraction -----------------
    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        start_time = time.time()
        driver = None

        try:
            if attempt > 1:
                print(f"[RETRY] Retry {attempt}/{max_retries} for: {account.email}")
                _force_kill_chrome()
                time.sleep(1.5)

            # Timeout-guarded Chrome launch
            driver = _launch_driver_with_timeout(browser_manager, account, launch_timeout)

            from auth.google_auth import GoogleAuthVerifier
            verifier = GoogleAuthVerifier(driver)
            status, details, token_data = verifier.login_and_verify(account)
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
                        session_info = extractor.extract_session_data(driver, account.email, token_data=token_data)
                        extractor.save_session(session_info)
                        print(f"[*] Tokens & cookies extracted: {account.email} -> verified_tokens.json, verified_tokens.txt")
                        if settings.auto_register_9router and token_data and token_data.get("refresh_token"):
                            from auth.db_sync import NineRouterDbSync
                            synced = NineRouterDbSync().sync_account(
                                email=account.email,
                                access_token=token_data.get("access_token", ""),
                                refresh_token=token_data.get("refresh_token", ""),
                                project_id="aicode-consumers",
                            )
                            if synced:
                                print(f"[OK] Account registered and activated in 9router SQLite: {account.email}")
                    except Exception as ex:
                        print(f"[!] Warning: Token extraction failed for {account.email}: {ex}")

                # Ensure Chrome flushes cookies to its profile on disk BEFORE closing (No force-kill)
                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path and driver:
                    flushed = browser_manager.flush_and_close(profile_path)
                    if flushed:
                        print(f"[OK] Authentication cookies verified on disk in profile: {account.email}")
                    else:
                        print(f"[~] Cookie flush wait finished for {account.email}")

                removed = loader.remove_account(account.email)
                if removed:
                    print(f"[*] Verified: {account.email} automatically removed from {loader.file_path.name}")

            elif status == VerificationStatus.INVALID_CREDENTIALS:
                # Wrong password or account not found — skip this email entirely.
                # Keep the account in accounts.txt so the user can correct the password.
                if skipped_credentials is not None:
                    skipped_credentials.append(account.email)
                print(f"[SKIP] Skipping {account.email} — {details}")
                print(f"[SKIP] Account kept in {loader.file_path.name} — update the password and re-run.")
                return  # <- skip, do not retry, do not remove from accounts.txt

            elif status in (VerificationStatus.ACCESS_FORBIDDEN, VerificationStatus.VALIDATION_REQUIRED) and driver:
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
    skipped_credentials: list[str] = []  # Accounts skipped due to wrong password

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
                skipped_credentials=skipped_credentials,
            )
            time.sleep(0.5)  # Brief pause between accounts
    except KeyboardInterrupt:
        print("\nVerification process interrupted by user.")
    finally:
        # Export reports and display tabular audit summary
        auditor.export_reports()
        auditor.print_summary()

        # Print skipped accounts summary (wrong password / account not found)
        if skipped_credentials:
            print(f"\n{'=' * 60}")
            print(f"⚠️  SKIPPED ACCOUNTS — INVALID CREDENTIALS ({len(skipped_credentials)})")
            print(f"{'=' * 60}")
            print("The following accounts were skipped due to incorrect password")
            print("or unrecognized email. They remain in accounts.txt so you can")
            print("fix the password and re-run.\n")
            for i, email in enumerate(skipped_credentials, start=1):
                print(f"  [{i:02d}] {email}")
            print(f"\n{'=' * 60}\n")


if __name__ == "__main__":
    main()
