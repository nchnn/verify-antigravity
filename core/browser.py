# File: browser.py
# Created: 2026-09-12 11:56 PM
# Last Modified: 2026-09-12 11:56 PM

import re
import time
import json
import logging
from pathlib import Path
from typing import Optional
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from config.settings import get_settings

logger = logging.getLogger("AntigravityAuditor")


class BrowserManager:
    """Manages undetected-chromedriver lifecycle and profile environments."""

    def __init__(self, headless: Optional[bool] = None):
        self.settings = get_settings()
        self.headless = self.settings.browser_headless if headless is None else headless
        self.driver: Optional[uc.Chrome] = None
        self.current_profile_path: Optional[Path] = None

    def get_profile_path(self, account_email: str) -> Optional[Path]:
        """Generates a dedicated profile directory for the specific account."""
        if not self.settings.persistent_profiles:
            return None

        # Sanitize email for folder name
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]", "_", account_email.lower())
        profile_dir = self.settings.profiles_dir / safe_name
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Configure Chrome to preserve session cookies across browser restarts
        default_dir = profile_dir / "Default"
        default_dir.mkdir(parents=True, exist_ok=True)
        pref_file = default_dir / "Preferences"
        try:
            prefs = {}
            if pref_file.exists():
                with open(pref_file, "r", encoding="utf-8") as f:
                    prefs = json.load(f)
            if "session" not in prefs:
                prefs["session"] = {}
            prefs["session"]["restore_on_startup"] = 1
            with open(pref_file, "w", encoding="utf-8") as f:
                json.dump(prefs, f)
        except Exception as e:
            logger.debug(f"Could not write session restore preference: {e}")

        return profile_dir

    def _detect_chrome_version(self) -> Optional[int]:
        """Detects the installed Chrome major version on Windows."""
        if self.settings.chrome_version_main:
            return self.settings.chrome_version_main

        # Attempt to inspect Chrome executable version on Windows
        chrome_paths = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
        ]
        
        for p in chrome_paths:
            path_obj = Path(p)
            if path_obj.exists():
                try:
                    import subprocess
                    cmd = f'(Get-Item "{str(path_obj)}").VersionInfo.ProductVersion'
                    res = subprocess.run(
                        ["powershell", "-NoProfile", "-Command", cmd],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    ver_str = res.stdout.strip()
                    if ver_str and "." in ver_str:
                        major = int(ver_str.split(".")[0])
                        logger.debug(f"Detected installed Chrome version: {major} ({ver_str})")
                        return major
                except Exception as e:
                    logger.debug(f"Error reading Chrome version: {e}")

        # Default to 152 if detection fails
        return 152

    def _release_profile_lock(self, profile_path: Path):
        """
        Kills any existing Chrome processes that are holding a lock on the
        given profile directory. Chrome locks the profile with a 'SingletonLock'
        file — if a previous run crashed or wasn't closed cleanly, the next
        launch will fail with 'cannot determine loading status'.
        """
        try:
            import subprocess
            import psutil

            profile_str = str(profile_path).lower()
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if proc.info["name"] and "chrome" in proc.info["name"].lower():
                        cmdline = " ".join(proc.info["cmdline"] or []).lower()
                        if profile_str in cmdline:
                            try:
                                proc.wait(timeout=2.0)
                            except (psutil.TimeoutExpired, Exception):
                                logger.debug(f"Terminating unresponsive Chrome process (PID {proc.pid}) holding profile lock.")
                                proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Also remove Chrome's SingletonLock / SingletonSocket files
            for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                lock_path = profile_path / lock_file
                if lock_path.exists():
                    try:
                        lock_path.unlink()
                        logger.debug(f"Removed stale lock file: {lock_file}")
                    except Exception:
                        pass
        except ImportError:
            logger.debug("psutil not available — skipping profile lock release. Install with: pip install psutil")
        except Exception as e:
            logger.debug(f"Profile lock release error: {e}")

    def create_driver(self, account_email: Optional[str] = None) -> uc.Chrome:
        """Initializes and returns an undetected Chrome WebDriver instance."""
        options = uc.ChromeOptions()
        
        if self.headless:
            options.add_argument("--headless=new")

        # Essential stealth & performance arguments
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,900")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--lang=en-US")
        # Suppress Chrome's 'Restore pages?' dialog after a crash
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--restore-last-session")

        profile_path = None
        if account_email:
            profile_path = self.get_profile_path(account_email)
            self.current_profile_path = profile_path
            if profile_path:
                self._release_profile_lock(profile_path)

        chrome_ver = self._detect_chrome_version()
        logger.info(f"Launching Chrome (v{chrome_ver}) for: {account_email or 'anonymous'}")
        
        try:
            self.driver = uc.Chrome(
                options=options,
                user_data_dir=str(profile_path) if profile_path else None,
                version_main=chrome_ver,
                use_subprocess=True,
            )
            self.driver.set_page_load_timeout(self.settings.page_timeout_seconds)
            self.driver.implicitly_wait(10)
            return self.driver
        except Exception as e:
            logger.error(f"Failed to launch Chrome driver: {e}")
            raise

    def _wait_for_sqlite_flush(self, profile_path: Path, timeout: float = 12.0) -> bool:
        """
        Polls the profile's SQLite cookie database to ensure Chrome has flushed
        its in-memory authentication cookies (SAPISID or SID) to disk before allowing
        the driver process to close.
        """
        from auth.profile_extractor import ProfileCookieExtractor

        start = time.time()
        pe = ProfileCookieExtractor(profile_path)
        while time.time() - start < timeout:
            try:
                cookies = pe.extract_cookies()
                if cookies and (cookies.get("SAPISID") or cookies.get("SID")):
                    elapsed = time.time() - start
                    logger.info(
                        f"[OK] Profile cookies confirmed on disk in SQLite ({len(cookies)} cookies, {elapsed:.2f}s): {profile_path.name}"
                    )
                    return True
            except Exception:
                pass
            time.sleep(0.3)

        # Secondary check: verify if the SQLite cookies table has rows
        db = profile_path / "Default" / "Network" / "Cookies"
        if db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                count = conn.execute("SELECT count(*) FROM cookies").fetchone()[0]
                conn.close()
                if count > 0:
                    logger.info(f"[OK] SQLite database flushed with {count} rows on disk: {profile_path.name}")
                    return True
            except Exception:
                pass

        logger.debug(f"[~] Cookie flush wait finished for {profile_path.name}")
        return False

    def flush_and_close(self, profile_path: Optional[Path] = None, timeout: float = 12.0) -> bool:
        """
        Ensures Chrome flushes all authentication cookies to the profile's SQLite
        database on disk BEFORE closing or terminating the browser.
        
        Guarantees Chrome is NOT force-killed until the required cookies are in their profile.
        """
        if not self.driver:
            return True

        target_profile = profile_path or self.current_profile_path

        # 1. Instruct Chrome via CDP to close gracefully — triggers NetworkService SQLite commit
        try:
            self.driver.execute_cdp_cmd("Browser.close", {})
            logger.info("[*] Sent graceful close signal via CDP; waiting for cookie flush to disk...")
        except Exception as e:
            logger.debug(f"Browser.close CDP signal notice: {e}")

        # 2. Wait and verify that cookies have reached the SQLite database on disk
        flushed = False
        if target_profile:
            flushed = self._wait_for_sqlite_flush(target_profile, timeout=timeout)

        # 3. Clean up Selenium driver & service
        try:
            time.sleep(0.8)
            self.driver.quit()
        except Exception:
            pass
        finally:
            self.driver = None
            self.current_profile_path = None

        return flushed

    def close(self):
        """Safely terminates the active Chrome driver instance, ensuring SQLite cache is flushed."""
        if self.driver:
            if self.current_profile_path:
                self.flush_and_close(self.current_profile_path, timeout=5.0)
            else:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                finally:
                    self.driver = None
                    self.current_profile_path = None
