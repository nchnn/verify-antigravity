import re
import json
import time
import shutil
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Optional
from colorama import Fore, Style

from config.settings import get_settings
from audit.auditor import VerificationStatus
from auth.token_extractor import TokenExtractor
from auth.profile_extractor import ProfileCookieExtractor


def email_already_extracted(email: str, settings) -> bool:
    json_path = settings.base_dir / getattr(settings, "tokens_output_json", "verified_tokens.json")
    if not json_path.exists():
        return False
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entry = data.get(email)
        if not entry:
            return False

        at = entry.get("accessToken", "")
        if at and at.startswith("ya29."):
            cookie_dict = entry.get("cookie_dict", {})
            has_cookies = bool(cookie_dict.get("SAPISID") or cookie_dict.get("SID") or entry.get("cookies"))
            return has_cookies

        cookie_dict = entry.get("cookie_dict", {})
        if cookie_dict.get("SAPISID") or cookie_dict.get("SID"):
            return True

        return False
    except Exception:
        return False


def profile_exists(email: str, settings) -> bool:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", email.lower())
    profile_dir = settings.profiles_dir / safe_name
    return profile_dir.exists()


def force_kill_chrome():
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
            print(f"{Fore.YELLOW}[~] Killed {killed} lingering Chrome process(es).{Style.RESET_ALL}")
        time.sleep(1.0)
    except ImportError:
        pass


def _cleanup_profile(email: str, settings):
    """Remove chrome profile folder for unverified accounts."""
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", email.lower())
    profile_dir = settings.profiles_dir / safe_name
    if profile_dir.exists():
        try:
            shutil.rmtree(profile_dir)
            print(f"{Fore.YELLOW}[~] Removed profile folder for unverified account: {email}{Style.RESET_ALL}")
        except Exception as e:
            print(f"{Fore.RED}[!] Failed to remove profile folder for {email}: {e}{Style.RESET_ALL}")


def launch_driver_with_timeout(browser_manager, account, timeout: int):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browser_manager.create_driver, account)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(
                f"Chrome launch timed out after {timeout}s \u2014 process may be stuck or locked."
            )


def try_dpapi_extract(account_email: str, settings, extractor: TokenExtractor) -> bool:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", account_email.lower())
    profile_dir = settings.profiles_dir / safe_name

    if not profile_dir.exists():
        return False

    print(f"{Fore.CYAN}[*] Inspecting profile and decrypting cookies for: {account_email}{Style.RESET_ALL}")
    pe = ProfileCookieExtractor(profile_dir)
    cookie_dict = pe.extract_cookies()

    if not cookie_dict:
        print(f"{Fore.YELLOW}[~] Profile directory exists for {account_email}, but no active Google session found in SQLite. Sign-in required.{Style.RESET_ALL}")
        return False

    cookie_header = pe.build_cookie_header(cookie_dict)

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
        "portal_url": "(extracted directly from profile \u2014 Chrome bypassed)",
        "cookies": cookie_header,
        "cookie_header": cookie_header,
        "cookie_dict": cookie_dict,
        "tokens": tokens,
        "storage": {},
        "accessToken": "",
        "refreshToken": "",
        "source": "dpapi_profile",
    }

    extractor.save_session(session_info)
    print(
        f"{Fore.GREEN}[OK] Direct profile extraction successful: {len(cookie_dict)} cookies saved "
        f"for {account_email} (Chrome launch bypassed!){Style.RESET_ALL}"
    )
    print(
        f"{Fore.YELLOW}[~] NOTE: OAuth tokens (accessToken/refreshToken) are NOT in the Chrome profile. "
        f"9router / OmniRoute needs a browser OAuth flow to obtain them.{Style.RESET_ALL}"
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

    if email_already_extracted(account.email, settings):
        is_valid, detail = extractor.validate_and_refresh_stored_token(account.email)
        if is_valid:
            print(f"{Fore.GREEN}[OK] SKIPPING Chrome — {detail}: {account.email}{Style.RESET_ALL}")
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details=f"Token validated/refreshed \u2014 Chrome not launched. {detail}",
                duration_seconds=0.0,
            )
            loader.remove_account(account.email)
            return
        else:
            print(f"{Fore.YELLOW}[~] Stored token stale for {account.email}: {detail}{Style.RESET_ALL}")
            print(f"{Fore.YELLOW}[~] Proceeding to Chrome OAuth for fresh extraction...{Style.RESET_ALL}")

    if extract_tokens and profile_exists(account.email, settings):
        dpapi_ok = try_dpapi_extract(account.email, settings, extractor)
        if dpapi_ok:
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details="Cookies extracted from Chrome profile via DPAPI (no browser launched). OAuth tokens still needed for 9router.",
                duration_seconds=0.0,
            )
            removed = loader.remove_account(account.email)
            if removed:
                print(f"{Fore.CYAN}[*] Verified: {account.email} automatically removed from {loader.file_path.name}{Style.RESET_ALL}")
            return

    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        start_time = time.time()
        driver = None

        try:
            if attempt > 1:
                print(f"{Fore.YELLOW}{Style.BRIGHT}[RETRY] Retry {attempt}/{max_retries} for: {account.email}{Style.RESET_ALL}")
                force_kill_chrome()
                time.sleep(1.5)

            driver = launch_driver_with_timeout(browser_manager, account, launch_timeout)

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

            # --- Immediate skip: no account found or wrong password ---
            if status == VerificationStatus.INVALID_CREDENTIALS:
                if skipped_credentials is not None:
                    skipped_credentials.append(account.email)
                print(f"{Fore.RED}{Style.BRIGHT}[SKIP] {account.email} — {details}{Style.RESET_ALL}")
                print(f"{Fore.RED}[SKIP] Account kept in {loader.file_path.name} — update the password and re-run.{Style.RESET_ALL}")
                print(f"{Fore.YELLOW}[>>] Moving immediately to next account...{Style.RESET_ALL}")
                _cleanup_profile(account.email, settings)
                return

            if status == VerificationStatus.SKIPPED_NO_PASSWORD:
                if skipped_credentials is not None:
                    skipped_credentials.append(account.email)
                print(f"{Fore.YELLOW}[SKIP] {account.email} — {details}{Style.RESET_ALL}")
                print(f"{Fore.YELLOW}[>>] No password available. Moving immediately to next account...{Style.RESET_ALL}")
                _cleanup_profile(account.email, settings)
                return

            if status in (VerificationStatus.SUCCESS_VERIFIED, VerificationStatus.ALREADY_VERIFIED):
                if extract_tokens and driver:
                    try:
                        session_info = extractor.extract_session_data(driver, account.email, token_data=token_data)
                        extractor.save_session(session_info)
                        print(f"{Fore.CYAN}[*] Tokens & cookies extracted: {account.email} -> verified_tokens.json, verified_tokens.txt{Style.RESET_ALL}")
                        if settings.auto_register_9router and token_data and token_data.get("refresh_token"):
                            from auth.db_sync import NineRouterDbSync
                            synced = NineRouterDbSync().sync_account(
                                email=account.email,
                                access_token=token_data.get("access_token", ""),
                                refresh_token=token_data.get("refresh_token", ""),
                                project_id="aicode-consumers",
                            )
                            if synced:
                                print(f"{Fore.GREEN}[OK] Account registered and activated in 9router SQLite: {account.email}{Style.RESET_ALL}")
                    except Exception as ex:
                        print(f"{Fore.RED}[!] Warning: Token extraction failed for {account.email}: {ex}{Style.RESET_ALL}")

                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path and driver:
                    flushed = browser_manager.flush_and_close(profile_path)
                    if flushed:
                        print(f"{Fore.GREEN}[OK] Authentication cookies verified on disk in profile: {account.email}{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.YELLOW}[~] Cookie flush wait finished for {account.email}{Style.RESET_ALL}")

                removed = loader.remove_account(account.email)
                if removed:
                    print(f"{Fore.CYAN}[*] Verified: {account.email} automatically removed from {loader.file_path.name}{Style.RESET_ALL}")

            elif status in (VerificationStatus.ACCESS_FORBIDDEN, VerificationStatus.VALIDATION_REQUIRED) and driver:
                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path:
                    browser_manager.flush_and_close(profile_path)
                _cleanup_profile(account.email, settings)

            else:
                # CHALLENGE_REQUIRED or any other non-success status
                _cleanup_profile(account.email, settings)

            return

        except TimeoutError as te:
            last_error = str(te)
            print(f"{Fore.RED}{Style.BRIGHT}[!] Chrome launch TIMED OUT on attempt {attempt}/{max_retries} for {account.email}.{Style.RESET_ALL}")
            force_kill_chrome()

        except Exception as e:
            last_error = str(e)
            print(f"{Fore.RED}{Style.BRIGHT}[!] Error on attempt {attempt}/{max_retries} for {account.email}: {e}{Style.RESET_ALL}")

        finally:
            browser_manager.close()

    duration = time.time() - start_time if 'start_time' in locals() else 0.0
    print(f"{Fore.RED}{Style.BRIGHT}[FAIL] All {max_retries} attempts failed for {account.email}. Marking as ERROR.{Style.RESET_ALL}")
    auditor.log_status(
        email=account.email,
        status=VerificationStatus.ERROR,
        details=f"Failed after {max_retries} retries. Last error: {last_error}",
        duration_seconds=duration,
    )
    _cleanup_profile(account.email, settings)
