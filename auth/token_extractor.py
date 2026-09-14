# File: token_extractor.py
# Created: 2026-09-13 01:00 AM
# Last Modified: 2026-09-13 02:25 AM

import re
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional

from config.settings import get_settings

logger = logging.getLogger("AntigravityAuditor")


class TokenExtractor:
    """Extracts session cookies, localStorage tokens, and auth data from the verified browser."""

    # Key Google authentication cookies required by routers / API proxies
    PRIMARY_GOOGLE_COOKIES = [
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "SSID",
        "SID",
        "HSID",
        "APISID",
    ]

    def __init__(self):
        self.settings = get_settings()

    def extract_session_data(self, driver, email: str, token_data: dict = None) -> Dict[str, Any]:
        """
        Extracts cookies and local storage tokens from the active browser session.
        Uses Chrome DevTools Protocol (CDP) to extract all in-memory cookies across
        all domains (.google.com, accounts.google.com, antigravity.google), avoiding
        SQLite disk locks or Windows DPAPI decryption issues.

        token_data: optional dict with 'access_token' and 'refresh_token' obtained
                    during the OAuth code-exchange step (ya29.xxx Bearer tokens).
        """
        session_info: Dict[str, Any] = {
            "email": email,
            "extracted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "portal_url": "",
            "cookies": "",
            "cookie_header": "",
            "cookie_dict": {},
            "tokens": {},
            "storage": {},
        }

        try:
            session_info["portal_url"] = driver.current_url
        except Exception:
            session_info["portal_url"] = ""

        # 1. Extract All Cookies in Memory via CDP (Cross-Domain)
        cookie_dict: Dict[str, str] = {}
        google_cookie_pairs: List[str] = []
        all_cookie_pairs: List[str] = []

        try:
            # Query all cookies in the active browser memory
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

            # Prioritize Google authentication cookies
            if name in self.PRIMARY_GOOGLE_COOKIES or "google" in domain.lower():
                google_cookie_pairs.append(pair)

        # Assemble cookie headers
        # Use google_cookie_pairs if found, else all cookie pairs
        active_pairs = google_cookie_pairs if google_cookie_pairs else all_cookie_pairs
        cookie_header = "; ".join(active_pairs)

        session_info["cookie_dict"] = cookie_dict
        session_info["cookie_header"] = cookie_header
        # 9router token_loader expects 'cookies' to be a string or cookie header
        session_info["cookies"] = cookie_header

        # Inject OAuth Bearer tokens from code-exchange if provided
        if token_data:
            at = token_data.get("access_token", "")
            rt = token_data.get("refresh_token", "")
            if at:
                session_info["accessToken"] = at
            if rt:
                session_info["refreshToken"] = rt

        # 2. Extract LocalStorage & SessionStorage from current page
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

        # 3. Detect OAuth/Bearer/JWT tokens in cookies and storage
        self._find_tokens(session_info)

        return session_info

    def _find_tokens(self, session_info: Dict[str, Any]):
        """Scans cookies and storage for OAuth Bearer tokens, Google ya29 tokens, or JWTs."""
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

        # Key Google auth cookies
        for cookie_name in self.PRIMARY_GOOGLE_COOKIES:
            if cookie_name in session_info["cookie_dict"]:
                tokens[f"COOKIE_{cookie_name}"] = session_info["cookie_dict"][cookie_name]

        session_info["tokens"] = tokens

    def save_session(self, session_info: Dict[str, Any]):
        """
        Saves the extracted session to verified_tokens.json and verified_tokens.txt
        in formats directly compatible with 9router's TokenLoader.
        """
        email = session_info["email"]

        # 1. Update verified_tokens.json
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

        # Find primary bearer token if available
        primary_token = session_info.get("accessToken", "")
        if not primary_token:
            for t_k, t_v in session_info.get("tokens", {}).items():
                if not t_k.startswith("COOKIE_"):
                    primary_token = t_v
                    break

        # Also store accessToken/refreshToken in the tokens dict for 9router
        if session_info.get("accessToken"):
            session_info["tokens"]["accessToken"] = session_info["accessToken"]
        if session_info.get("refreshToken"):
            session_info["tokens"]["refreshToken"] = session_info["refreshToken"]

        data[email] = session_info

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        # 2. Update verified_tokens.txt (format: email:accessToken:cookies)
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
        # Format: email:accessToken:cookies (9router TokenLoader standard format)
        new_entry = f"{email}:{primary_token}:{cookies_str}\n"
        existing_lines.append(new_entry)

        with open(txt_path, "w", encoding="utf-8") as f:
            f.writelines(existing_lines)

        logger.info(f"Session data saved for {email} -> {json_path.name}, {txt_path.name}")
