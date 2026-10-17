
import re
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional
from config.settings import get_settings


@dataclass
class Account:
    email: str
    password: str
    proxy: Optional[str] = None
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
        if not self.file_path.exists():
            raise FileNotFoundError(f"Accounts file not found at: {self.file_path}")

        accounts: List[Account] = []
        target_norm = target_email.strip().lower() if target_email else None

        with open(self.file_path, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                email, password, is_fallback, proxy = self._parse_line(line, include_proxy=True)
                if not email:
                    continue

                if target_norm and email.lower() != target_norm:
                    continue

                accounts.append(
                    Account(
                        email=email,
                        password=password,
                        proxy=proxy,
                        is_fallback_password=is_fallback,
                        source_line=line_no,
                    )
                )

        return accounts

    def remove_account(self, email: str) -> bool:
        if not self.file_path.exists():
            return False

        email_norm = email.strip().lower()
        new_lines: List[str] = []
        removed = False

        with open(self.file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()
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

    def _parse_line(self, line: str, include_proxy: bool = False):
        proxy = None
        if ":" in line:
            parts = line.split(":", 2)
            email = parts[0].strip()
            password = parts[1].strip()
            if len(parts) > 2:
                p_cand = parts[2].strip()
                proxy = p_cand if p_cand else None
        else:
            email = line.strip()
            password = ""
            proxy = None

        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            if include_proxy:
                return None, "", False, None
            return None, "", False

        is_fallback = False
        if not password:
            password = self.settings.default_account_password
            is_fallback = True

        if include_proxy:
            return email, password, is_fallback, proxy
        return email, password, is_fallback
