# File: settings.py
# Created: 2026-09-12 11:56 PM
# Last Modified: 2026-09-13 03:50 AM

import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env file if present
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE)
else:
    load_dotenv()


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    accounts_file: Path = BASE_DIR / "accounts.txt"
    profiles_dir: Path = BASE_DIR / os.getenv("PROFILES_DIR", "chrome_profiles")
    audit_report_json: Path = BASE_DIR / "audit_report.json"
    audit_report_csv: Path = BASE_DIR / "audit_report.csv"
    audit_report_md: Path = BASE_DIR / "AUDIT_SUMMARY.md"
    
    default_account_password: str = os.getenv("DEFAULT_ACCOUNT_PASSWORD", "")
    chrome_version_main: Optional[int] = (
        int(os.getenv("CHROME_VERSION_MAIN")) if os.getenv("CHROME_VERSION_MAIN") else None
    )
    antigravity_portal_url: str = os.getenv(
        "ANTIGRAVITY_PORTAL_URL", "https://antigravity.google/docs"
    )
    browser_headless: bool = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
    page_timeout_seconds: int = int(os.getenv("PAGE_TIMEOUT_SECONDS", "30"))
    action_delay_seconds: float = float(os.getenv("ACTION_DELAY_SECONDS", "2.0"))
    persistent_profiles: bool = os.getenv("PERSISTENT_PROFILES", "true").lower() == "true"
    
    # Token & Session Extraction Settings
    extract_tokens: bool = os.getenv("EXTRACT_TOKENS", "true").lower() == "true"
    tokens_output_json: str = os.getenv("TOKENS_OUTPUT_JSON", "verified_tokens.json")
    tokens_output_txt: str = os.getenv("TOKENS_OUTPUT_TXT", "verified_tokens.txt")

    # Retry & Timeout Settings
    # How long (seconds) to wait for Chrome to launch before declaring it stuck
    chrome_launch_timeout_seconds: int = int(os.getenv("CHROME_LAUNCH_TIMEOUT", "45"))
    # How many times to retry a stuck/failed account before marking it ERROR
    max_retries_per_account: int = int(os.getenv("MAX_RETRIES_PER_ACCOUNT", "3"))
    # How long (seconds) to wait for on-screen challenge / reCAPTCHA resolution
    challenge_timeout_seconds: int = int(os.getenv("CHALLENGE_TIMEOUT_SECONDS", "120"))

    # Phone Validation & 9router Integration Settings
    phone_validation_timeout_seconds: int = int(os.getenv("PHONE_VALIDATION_TIMEOUT_SECONDS", "180"))
    sms_activate_api_key: str = os.getenv("SMS_ACTIVATE_API_KEY", "")
    nine_router_db_path: Path = Path(os.path.expandvars(os.getenv("NINEROUTER_DB_PATH", r"%APPDATA%\9router\db\data.sqlite")))
    auto_register_9router: bool = os.getenv("AUTO_REGISTER_9ROUTER", "true").lower() == "true"


_settings_instance = None


def get_settings() -> Settings:
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance
