# File: account_loader.py
# Created: 2026-09-12 11:56 PM
# Last Modified: 2026-09-13 00:17 AM

import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
from config.settings import get_settings


@dataclass
class Account:
    email: str
    password: str
    is_fallback_password: bool = False
    source_line: int = 0

    def mask_password(self) -> str:
        if not self.password:
            return "<NO PASSWORD>"
        if len(self.password) <= 4:
            return "****"
        return self.password[:2] + "*" * (len(self.password) - 4) + self.password[-2:]


class AccountLoader:
    def __init__(self, file_path: Optional[Path] = None):
        self.settings = get_settings()
        self.file_path = file_path or self.settings.accounts_file

    def load_accounts(self, target_email: Optional[str] = None) -> List[Account]:
        """Loads and parses accounts from accounts.txt with fallback password support."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Accounts file not found at: {self.file_path}")

        accounts: List[Account] = []
        target_norm = target_email.strip().lower() if target_email else None

        with open(self.file_path, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                # Skip blank lines and comments
                if not line or line.startswith("#"):
                    continue

                email, password, is_fallback = self._parse_line(line)
                if not email:
                    continue

                if target_norm and email.lower() != target_norm:
                    continue

                accounts.append(
                    Account(
                        email=email,
                        password=password,
                        is_fallback_password=is_fallback,
                        source_line=line_no,
                    )
                )

        return accounts

    def remove_account(self, email: str) -> bool:
        """Removes a successfully verified account from the accounts file."""
        if not self.file_path.exists():
            return False

        email_norm = email.strip().lower()
        new_lines: List[str] = []
        removed = False

        with open(self.file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()
            # Preserve comments and empty lines
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            parsed_email, _, _ = self._parse_line(stripped)
            if parsed_email and parsed_email.lower() == email_norm:
                removed = True
                continue
            new_lines.append(line)

        if removed:
            with open(self.file_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        return removed

    def _parse_line(self, line: str) -> tuple[Optional[str], str, bool]:
        """
        Parses a single line.
        Supports:
          - email:password
          - email:
          - email
        """
        if ":" in line:
            parts = line.split(":", 1)
            email = parts[0].strip()
            password = parts[1].strip()
        else:
            email = line.strip()
            password = ""

        # Basic email syntax check
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return None, "", False

        is_fallback = False
        if not password:
            password = self.settings.default_account_password
            is_fallback = True

        return email, password, is_fallback
