import re
import json
import time
import concurrent.futures
from datetime import datetime
from pathlib import Path
from typing import Optional

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
            print(f"[~] Killed {killed} lingering Chrome process(es).")
        time.sleep(1.0)
    except ImportError:
        pass


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

    print(f"[*] Inspecting profile and decrypting cookies for: {account_email}")
    pe = ProfileCookieExtractor(profile_dir)
    cookie_dict = pe.extract_cookies()

    if not cookie_dict:
        print(f"[~] Profile directory exists for {account_email}, but no active Google session found in SQLite. Sign-in required.")
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

    if email_already_extracted(account.email, settings):
        is_valid, detail = extractor.validate_and_refresh_stored_token(account.email)
        if is_valid:
            print(f"[OK] SKIPPING Chrome \u2014 {detail}: {account.email}")
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details=f"Token validated/refreshed \u2014 Chrome not launched. {detail}",
                duration_seconds=0.0,
            )
            loader.remove_account(account.email)
            return
        else:
            print(f"[~] Stored token stale for {account.email}: {detail}")
            print(f"[~] Proceeding to Chrome OAuth for fresh extraction...")

    if extract_tokens and profile_exists(account.email, settings):
        dpapi_ok = try_dpapi_extract(account.email, settings, extractor)
        if dpapi_ok:
            auditor.log_status(
                email=account.email,
                status=VerificationStatus.ALREADY_VERIFIED,
                details="Cookies extracted from Chrome profile via DPAPI (no browser launched). OAuth tokens still needed for 9router.",
                duration_seconds=0.0,
            )
            return

    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        start_time = time.time()
        driver = None

        try:
            if attempt > 1:
                print(f"[RETRY] Retry {attempt}/{max_retries} for: {account.email}")
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
                if skipped_credentials is not None:
                    skipped_credentials.append(account.email)
                print(f"[SKIP] Skipping {account.email} \u2014 {details}")
                print(f"[SKIP] Account kept in {loader.file_path.name} \u2014 update the password and re-run.")
                return

            elif status in (VerificationStatus.ACCESS_FORBIDDEN, VerificationStatus.VALIDATION_REQUIRED) and driver:
                profile_path = browser_manager.get_profile_path(account.email)
                if profile_path:
                    browser_manager.flush_and_close(profile_path)

            return

        except TimeoutError as te:
            last_error = str(te)
            print(f"[!] Chrome launch TIMED OUT on attempt {attempt}/{max_retries} for {account.email}.")
            force_kill_chrome()

        except Exception as e:
            last_error = str(e)
            print(f"[!] Error on attempt {attempt}/{max_retries} for {account.email}: {e}")

        finally:
            browser_manager.close()

    duration = time.time() - start_time if 'start_time' in locals() else 0.0
    print(f"[FAIL] All {max_retries} attempts failed for {account.email}. Marking as ERROR.")
    auditor.log_status(
        email=account.email,
        status=VerificationStatus.ERROR,
        details=f"Failed after {max_retries} retries. Last error: {last_error}",
        duration_seconds=duration,
    )
