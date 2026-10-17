
import os
import re
import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from config.settings import get_settings

logger = logging.getLogger("AntigravityAuditor")


class TokenExtractor:

    PRIMARY_GOOGLE_COOKIES = [
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SSID",
        "SID",
        "HSID",
        "APISID",
    ]

    ALL_GOOGLE_AUTH_COOKIES = [
        "SAPISID",
        "SID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SSID",
        "HSID",
        "APISID",
        "__Secure-1PAPISID",
        "__Secure-3PAPISID",
        "SIDCC",
        "__Secure-1PSIDCC",
        "__Secure-3PSIDCC",
        "__Secure-1PSIDTS",
        "__Secure-3PSIDTS",
        "LSID",
        "OSID",
        "__Secure-OSID",
        "__Host-1PLSID",
        "__Host-3PLSID",
        "__Host-GAPS",
        "__Host-GAPSTS",
    ]

    _TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
    _TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"

    def __init__(self):
        self.settings = get_settings()


    def validate_token(self, access_token: str) -> Tuple[bool, Optional[int]]:
        if not access_token or not access_token.startswith("ya29."):
            return False, None

        try:
            url = f"{self._TOKENINFO_URL}?access_token={urllib.parse.quote(access_token, safe='')}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                expires_in = int(data.get("expires_in", 0))
                if expires_in > 0:
                    logger.info(
                        f"Token valid — expires in {expires_in}s "
                        f"(email: {data.get('email', 'unknown')})"
                    )
                    return True, expires_in
                return False, None
        except urllib.error.HTTPError as he:
            logger.debug(f"tokeninfo returned HTTP {he.code} — token expired or invalid")
            return False, None
        except Exception as e:
            logger.debug(f"tokeninfo check failed: {e}")
            return False, None

    def refresh_access_token(self, refresh_token: str) -> Tuple[Optional[str], Optional[str]]:
        if not refresh_token:
            logger.debug("No refresh_token provided — cannot refresh")
            return None, None

        client_id = os.environ.get("AG_CLIENT_ID", "")
        client_secret = os.environ.get("AG_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            logger.warning("AG_CLIENT_ID / AG_CLIENT_SECRET not set — cannot refresh token")
            return None, None

        try:
            body = urllib.parse.urlencode({
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            }).encode("utf-8")

            req = urllib.request.Request(
                self._TOKEN_REFRESH_URL,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read().decode("utf-8"))

            new_access_token = token_data.get("access_token")
            new_refresh_token = token_data.get("refresh_token", refresh_token)

            if new_access_token:
                logger.info(f"Access token refreshed successfully (expires_in={token_data.get('expires_in')})")
                return new_access_token, new_refresh_token
            else:
                logger.warning("Token refresh response did not contain access_token")
                return None, None

        except urllib.error.HTTPError as he:
            err_body = he.read().decode("utf-8", errors="replace")[:300]
            logger.warning(f"Token refresh failed — HTTP {he.code}: {err_body}")
            return None, None
        except Exception as e:
            logger.warning(f"Token refresh failed: {e}")
            return None, None

    def validate_and_refresh_stored_token(self, email: str) -> Tuple[bool, str]:
        json_path = self.settings.base_dir / getattr(
            self.settings, "tokens_output_json", "verified_tokens.json"
        )
        if not json_path.exists():
            return False, "verified_tokens.json does not exist"

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return False, f"Failed to read verified_tokens.json: {e}"

        entry = data.get(email)
        if not entry:
            return False, f"No entry found for {email}"

        access_token = entry.get("accessToken", "")
        refresh_token = entry.get("refreshToken", "")

        if access_token and access_token.startswith("ya29."):
            is_valid, remaining = self.validate_token(access_token)
            if is_valid:
                return True, f"Access token still valid (expires in {remaining}s)"
            logger.info(f"Access token expired for {email} — attempting refresh...")
            print(f"[~] Access token expired for {email} — refreshing...")
        else:
            if not refresh_token:
                return False, "No access token and no refresh token — re-auth required"
            logger.info(f"No access token for {email}, refresh_token present — refreshing...")
            print(f"[~] No access token for {email} — refreshing with stored refresh_token...")

        if not refresh_token:
            return False, "Access token expired and no refresh_token — re-auth required"

        new_access, new_refresh = self.refresh_access_token(refresh_token)
        if not new_access:
            return False, "Access token expired and refresh failed — re-auth required"

        print(f"[OK] Token refreshed for {email}")
        entry["accessToken"] = new_access
        entry["refreshToken"] = new_refresh or refresh_token
        if "tokens" in entry:
            entry["tokens"]["accessToken"] = new_access
            entry["tokens"]["refreshToken"] = new_refresh or refresh_token
        entry["last_refreshed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data[email] = entry

        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Updated verified_tokens.json with refreshed token for {email}")
        except Exception as e:
            logger.warning(f"Failed to write refreshed token to JSON: {e}")

        self._update_txt_token(email, new_access, new_refresh or refresh_token, entry.get("cookies", ""))

        if self.settings.auto_register_9router and (new_refresh or refresh_token):
            try:
                from auth.db_sync import NineRouterDbSync
                synced = NineRouterDbSync().sync_account(
                    email=email,
                    access_token=new_access,
                    refresh_token=new_refresh or refresh_token,
                )
                if synced:
                    print(f"[OK] Refreshed token synced to 9router for {email}")
            except Exception as e:
                logger.warning(f"Failed to sync refreshed token to 9router: {e}")

        return True, f"Access token refreshed successfully for {email}"

    def _update_txt_token(self, email: str, access_token: str, refresh_token: str, cookies_str: str):
        txt_path = self.settings.base_dir / getattr(
            self.settings, "tokens_output_txt", "verified_tokens.txt"
        )

        existing_lines: List[str] = []
        if txt_path.exists():
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    existing_lines = [
                        l for l in f.readlines() if not l.strip().startswith(f"{email}:")
                    ]
            except Exception:
                existing_lines = []

        new_entry = f"{email}:{access_token}:{refresh_token}:{cookies_str}\n"
        existing_lines.append(new_entry)

        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.writelines(existing_lines)
            logger.info(f"Updated verified_tokens.txt with refreshed token for {email}")
        except Exception as e:
            logger.warning(f"Failed to update verified_tokens.txt: {e}")

    def extract_session_data(self, driver, email: str, token_data: dict = None) -> Dict[str, Any]:
        session_info: Dict[str, Any] = {
            "email": email,
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "portal_url": "",
            "cookies": "",
            "cookie_header": "",
            "cookie_dict": {},
            "tokens": {},
            "storage": {},
            "accessToken": "",
            "refreshToken": "",
            "source": "browser_cdp",
        }

        try:
            session_info["portal_url"] = driver.current_url
        except Exception:
            session_info["portal_url"] = ""

        cookie_dict: Dict[str, str] = {}
        google_cookie_pairs: List[str] = []
        all_cookie_pairs: List[str] = []

        try:
            cdp_result = driver.execute_cdp_cmd("Network.getAllCookies", {})
            raw_cookies = cdp_result.get("cookies", [])
        except Exception as e:
            logger.debug(f"CDP getAllCookies failed, falling back to driver.get_cookies: {e}")
            try:
                raw_cookies = driver.get_cookies()
            except Exception:
                raw_cookies = []

        for c in raw_cookies:
            name = c.get("name", "")
            val = c.get("value", "")
            domain = c.get("domain", "")
            if not name:
                continue

            cookie_dict[name] = val
            pair = f"{name}={val}"
            all_cookie_pairs.append(pair)

            if name in self.PRIMARY_GOOGLE_COOKIES or "google" in domain.lower():
                google_cookie_pairs.append(pair)

        active_pairs = google_cookie_pairs if google_cookie_pairs else all_cookie_pairs
        cookie_header = "; ".join(active_pairs)

        session_info["cookie_dict"] = cookie_dict
        session_info["cookie_header"] = cookie_header
        session_info["cookies"] = cookie_header

        if token_data:
            at = token_data.get("access_token", "")
            rt = token_data.get("refresh_token", "")
            if at:
                session_info["accessToken"] = at
            if rt:
                session_info["refreshToken"] = rt

        try:
            local_storage = driver.execute_script(
                "var items = {}; for (var i=0; i<localStorage.length; i++) { var k = localStorage.key(i); items[k] = localStorage.getItem(k); } return items;"
            )
            session_info["storage"]["localStorage"] = local_storage or {}
        except Exception as e:
            logger.debug(f"Error extracting localStorage for {email}: {e}")

        try:
            session_storage = driver.execute_script(
                "var items = {}; for (var i=0; i<sessionStorage.length; i++) { var k = sessionStorage.key(i); items[k] = sessionStorage.getItem(k); } return items;"
            )
            session_info["storage"]["sessionStorage"] = session_storage or {}
        except Exception as e:
            logger.debug(f"Error extracting sessionStorage for {email}: {e}")

        self._find_tokens(session_info)

        return session_info

    def _find_tokens(self, session_info: Dict[str, Any]):
        tokens: Dict[str, str] = {}

        combined_storage = {
            **session_info["storage"].get("localStorage", {}),
            **session_info["storage"].get("sessionStorage", {}),
        }

        token_regex = re.compile(
            r"(ya29\.[a-zA-Z0-9_\-]+|eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)"
        )

        for key, val in combined_storage.items():
            if not isinstance(val, str):
                continue
            if any(k in key.lower() for k in ["token", "auth", "access", "bearer", "oauth", "jwt"]):
                tokens[key] = val
            else:
                matches = token_regex.findall(val)
                if matches:
                    tokens[key] = matches[0]

        for cookie_name in self.ALL_GOOGLE_AUTH_COOKIES:
            if cookie_name in session_info["cookie_dict"]:
                tokens[f"COOKIE_{cookie_name}"] = session_info["cookie_dict"][cookie_name]

        session_info["tokens"] = tokens

    def save_session(self, session_info: Dict[str, Any]):
        email = session_info["email"]

        json_path = self.settings.base_dir / getattr(
            self.settings, "tokens_output_json", "verified_tokens.json"
        )
        data = {}
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        primary_token = session_info.get("accessToken", "")
        if not primary_token:
            for t_k, t_v in session_info.get("tokens", {}).items():
                if not t_k.startswith("COOKIE_"):
                    primary_token = t_v
                    break

        if "accessToken" not in session_info:
            session_info["accessToken"] = ""
        if "refreshToken" not in session_info:
            session_info["refreshToken"] = ""
        if "source" not in session_info:
            session_info["source"] = "unknown"

        if session_info.get("accessToken"):
            session_info["tokens"]["accessToken"] = session_info["accessToken"]
        if session_info.get("refreshToken"):
            session_info["tokens"]["refreshToken"] = session_info["refreshToken"]

        data[email] = session_info

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        txt_path = self.settings.base_dir / getattr(
            self.settings, "tokens_output_txt", "verified_tokens.txt"
        )

        existing_lines: List[str] = []
        if txt_path.exists():
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    existing_lines = [
                        l for l in f.readlines() if not l.strip().startswith(f"{email}:")
                    ]
            except Exception:
                existing_lines = []

        cookies_str = session_info.get("cookies", "")
        refresh_token = session_info.get("refreshToken", "")
        new_entry = f"{email}:{primary_token}:{refresh_token}:{cookies_str}\n"
        existing_lines.append(new_entry)

        with open(txt_path, "w", encoding="utf-8") as f:
            f.writelines(existing_lines)

        logger.info(f"Session data saved for {email} -> {json_path.name}, {txt_path.name}")
