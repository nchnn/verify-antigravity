# File: auth/profile_extractor.py
# Created: 2026-09-13 04:03 AM
# Last Modified: 2026-09-13 04:10 AM
#
# Directly extracts and decrypts Google auth cookies from a Chrome profile
# SQLite database using Windows DPAPI + AES-256-GCM. No Chrome process needed.
#
# Chrome cookie encryption (v80+):
#   1. AES-256 key is stored encrypted in <profile>/Local State (DPAPI-wrapped)
#   2. Each cookie value is: b"v10" + 12-byte nonce + AES-GCM ciphertext + 16-byte tag
#   3. Decrypt key with DPAPI → decrypt each value with AES-256-GCM

import os
import json
import sqlite3
import shutil
import tempfile
import base64
import ctypes
import ctypes.wintypes
import logging
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("AntigravityAuditor")

# Google auth cookies we care about — must have at least one of the first two
PRIMARY_AUTH_COOKIES = [
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
]


# ─── Windows DPAPI decryption ──────────────────────────────────────────────────

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_decrypt(encrypted: bytes) -> bytes:
    """
    Decrypts data encrypted with Windows DPAPI (CryptUnprotectData).
    Works without pywin32 — uses ctypes directly.
    Only works for the CURRENT Windows user (same user that encrypted it).
    """
    p = ctypes.create_string_buffer(encrypted, len(encrypted))
    blobin = _DATA_BLOB(ctypes.sizeof(p), p)
    blobout = _DATA_BLOB()

    success = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blobin),
        None,   # ppszDataDescr
        None,   # pOptionalEntropy
        None,   # pvReserved
        None,   # pPromptStruct
        0,      # dwFlags
        ctypes.byref(blobout),
    )
    if not success:
        raise RuntimeError(
            f"CryptUnprotectData failed with error code: {ctypes.GetLastError()}"
        )

    result = ctypes.string_at(blobout.pbData, blobout.cbData)
    ctypes.windll.kernel32.LocalFree(blobout.pbData)
    return result


# ─── AES-256-GCM decryption ────────────────────────────────────────────────────

def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext_with_tag: bytes) -> bytes:
    """Decrypts AES-256-GCM ciphertext (includes 16-byte tag at the end)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext_with_tag, None)


# ─── Main Extractor Class ──────────────────────────────────────────────────────

class ProfileCookieExtractor:
    """
    Extracts and decrypts Google auth cookies directly from a Chrome user profile.
    
    Flow:
      1. Read <profile_dir>/Local State → get DPAPI-encrypted AES key → decrypt with DPAPI
      2. Open <profile_dir>/Default/Network/Cookies (SQLite) via a temp copy
      3. For each row: decrypt encrypted_value with AES-256-GCM using the key
      4. Return {name: value} dict of all Google auth cookies
    """

    def __init__(self, profile_dir: Path):
        """
        Args:
            profile_dir: The root of the Chrome user data dir, e.g.
                         chrome_profiles/randumonly_gmail.com/
        """
        self.profile_dir = Path(profile_dir)
        self._aes_key: Optional[bytes] = None

    # ── Key retrieval ──────────────────────────────────────────────────────────

    def _get_aes_key(self) -> Optional[bytes]:
        """
        Reads and decrypts Chrome's master AES-256 key from 'Local State'.
        Cached after first call.
        """
        if self._aes_key is not None:
            return self._aes_key

        local_state_path = self.profile_dir / "Local State"
        if not local_state_path.exists():
            logger.debug(f"[ProfileExtractor] Local State not found: {local_state_path}")
            return None

        try:
            with open(local_state_path, "r", encoding="utf-8") as f:
                local_state = json.load(f)

            encrypted_key_b64: str = (
                local_state.get("os_crypt", {}).get("encrypted_key", "")
            )
            if not encrypted_key_b64:
                logger.debug("[ProfileExtractor] No os_crypt.encrypted_key in Local State")
                return None

            # Base64-decode and strip the leading "DPAPI" prefix (5 bytes)
            encrypted_key = base64.b64decode(encrypted_key_b64)
            if encrypted_key[:5] == b"DPAPI":
                encrypted_key = encrypted_key[5:]

            aes_key = _dpapi_decrypt(encrypted_key)
            self._aes_key = aes_key
            logger.debug(f"[ProfileExtractor] AES key decrypted ({len(aes_key)} bytes)")
            return aes_key

        except Exception as e:
            logger.debug(f"[ProfileExtractor] Failed to get AES key: {e}")
            return None

    # ── Cookie decryption ──────────────────────────────────────────────────────

    @staticmethod
    def _decode_cookie_bytes(decrypted: bytes) -> str:
        """
        Decodes decrypted cookie payload into a UTF-8 string.
        Chromium 120+ prepends a 32-byte binary signature/hash to the plaintext before AES-GCM encryption.
        """
        if not decrypted:
            return ""

        # Chromium 120+: If payload > 32 bytes and header contains binary bytes, strip 32-byte header
        if len(decrypted) > 32 and not all(32 <= b <= 126 for b in decrypted[:32]):
            try:
                return decrypted[32:].decode("utf-8")
            except UnicodeDecodeError:
                pass

        # Standard decode
        try:
            return decrypted.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # Fallback with error ignore if 32-byte header
        if len(decrypted) > 32:
            try:
                return decrypted[32:].decode("utf-8", errors="ignore")
            except Exception:
                pass

        return ""

    def _decrypt_value(self, encrypted_value: bytes, aes_key: bytes) -> str:
        """
        Decrypts a single Chrome cookie value.

        Chrome 80+ stores cookies as:
            b"v10" | b"v11" + 12-byte nonce + ciphertext + 16-byte GCM tag

        Older Chrome used raw DPAPI encryption (no prefix).
        """
        if not encrypted_value:
            return ""

        try:
            prefix = encrypted_value[:3]

            if prefix in (b"v10", b"v11"):
                # AES-256-GCM format (Chrome 80+)
                nonce = encrypted_value[3:15]           # 12 bytes
                ciphertext_tag = encrypted_value[15:]   # rest (ciphertext + 16-byte tag)
                decrypted = _aes_gcm_decrypt(aes_key, nonce, ciphertext_tag)
                return self._decode_cookie_bytes(decrypted)
            else:
                # Legacy DPAPI-only encryption (Chrome < 80)
                decrypted = _dpapi_decrypt(encrypted_value)
                return self._decode_cookie_bytes(decrypted)

        except Exception as e:
            logger.debug(f"[ProfileExtractor] Cookie decryption failed: {e}")
            return ""

    # ── SQLite extraction ──────────────────────────────────────────────────────

    def extract_cookies(self) -> Optional[Dict[str, str]]:
        """
        Reads Chrome's Cookies SQLite database, decrypts all Google auth
        cookie values, and returns them as {name: value}.

        Returns None if:
          - Cookies database not found
          - DPAPI key unavailable
          - No SAPISID or SID found (session not valid)
        """
        cookies_db = self.profile_dir / "Default" / "Network" / "Cookies"
        if not cookies_db.exists():
            logger.debug(f"[ProfileExtractor] Cookies DB not found: {cookies_db}")
            return None

        aes_key = self._get_aes_key()
        if not aes_key:
            return None

        # Chrome holds an exclusive SQLite lock while running.
        # Copy to a temp file so we can read it even if Chrome is open.
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix="_cookies.db", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(cookies_db), tmp_path)

            cookie_dict: Dict[str, str] = {}

            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                # Select Google auth cookies, prioritizing .google.com domain and freshest expiry
                rows = conn.execute(
                    """
                    SELECT name, encrypted_value, host_key, expires_utc
                    FROM cookies
                    WHERE (host_key LIKE '%google.com%'
                       OR  host_key LIKE '%antigravity%')
                    ORDER BY 
                        CASE 
                            WHEN host_key = '.google.com' THEN 0 
                            WHEN host_key LIKE '%.google.com' THEN 1 
                            ELSE 2 
                        END ASC,
                        expires_utc DESC
                    """
                ).fetchall()
            finally:
                conn.close()

            for row in rows:
                name: str = row["name"]
                encrypted_value: bytes = bytes(row["encrypted_value"])

                if not name or not encrypted_value:
                    continue
                if name in cookie_dict:
                    continue  # Already have a fresher value

                value = self._decrypt_value(encrypted_value, aes_key)
                if value:
                    cookie_dict[name] = value

            if not cookie_dict:
                logger.debug("[ProfileExtractor] No decryptable Google cookies in profile")
                return None

            # Require at least SAPISID or SID to consider the session valid
            if not (cookie_dict.get("SAPISID") or cookie_dict.get("SID")):
                logger.debug(
                    "[ProfileExtractor] Cookies found but no SAPISID/SID — session not valid"
                )
                return None

            logger.info(
                f"[ProfileExtractor] Decrypted {len(cookie_dict)} cookies "
                f"({', '.join(k for k in PRIMARY_AUTH_COOKIES if k in cookie_dict)})"
            )
            return cookie_dict

        except Exception as e:
            logger.debug(f"[ProfileExtractor] SQLite extraction error: {e}")
            return None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def build_cookie_header(self, cookie_dict: Dict[str, str]) -> str:
        """Formats the cookie dict as a single HTTP Cookie header string."""
        return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
