
import csv
import json
import logging
from enum import Enum
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional
from colorama import init, Fore, Style
from tabulate import tabulate
from config.settings import get_settings

init(autoreset=True)


class VerificationStatus(str, Enum):
    SUCCESS_VERIFIED = "SUCCESS_VERIFIED"
    ACCESS_FORBIDDEN = "ACCESS_FORBIDDEN"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    VALIDATION_REQUIRED = "VALIDATION_REQUIRED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ALREADY_VERIFIED = "ALREADY_VERIFIED"
    SKIPPED_NO_PASSWORD = "SKIPPED_NO_PASSWORD"
    ERROR = "ERROR"


@dataclass
class AuditRecord:
    email: str
    status: VerificationStatus
    timestamp: str
    details: str = ""
    duration_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


class AccountAuditor:

    def __init__(self, log_level: int = logging.INFO):
        self.settings = get_settings()
        self.records: List[AuditRecord] = []
        self._setup_logger(log_level)

    def _setup_logger(self, log_level: int):
        self.logger = logging.getLogger("AntigravityAuditor")
        self.logger.setLevel(log_level)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def log_status(
        self,
        email: str,
        status: VerificationStatus,
        details: str = "",
        duration_seconds: float = 0.0,
    ) -> AuditRecord:
        clean_detail = details.strip().split("\n")[0]
        if "Stacktrace:" in clean_detail:
            clean_detail = clean_detail.split("Stacktrace:")[0].strip()

        record = AuditRecord(
            email=email,
            status=status,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            details=clean_detail,
            duration_seconds=round(duration_seconds, 2),
        )
        self.records.append(record)

        color = Fore.WHITE
        if status in (VerificationStatus.SUCCESS_VERIFIED, VerificationStatus.ALREADY_VERIFIED):
            color = Fore.GREEN
        elif status == VerificationStatus.ACCESS_FORBIDDEN:
            color = Fore.RED
        elif status in (
            VerificationStatus.CHALLENGE_REQUIRED,
            VerificationStatus.VALIDATION_REQUIRED,
            VerificationStatus.SKIPPED_NO_PASSWORD,
        ):
            color = Fore.YELLOW
        elif status in (VerificationStatus.INVALID_CREDENTIALS, VerificationStatus.ERROR):
            color = Fore.RED + Style.BRIGHT

        self.logger.info(
            f"{color}[{status.value}] {email} - {clean_detail} ({record.duration_seconds}s){Style.RESET_ALL}"
        )
        return record

    def export_reports(self):
        self._export_json()
        self._export_csv()
        self._export_markdown()
        self.logger.info("Reports updated: AUDIT_SUMMARY.md, audit_report.json, audit_report.csv")

    def _export_json(self):
        json_path = self.settings.audit_report_json
        data = [r.to_dict() for r in self.records]
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total_accounts": len(self.records),
                    "records": data,
                },
                f,
                indent=2,
            )

    def _export_csv(self):
        csv_path = self.settings.audit_report_csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Email", "Status", "Timestamp", "Duration (s)", "Details"])
            for r in self.records:
                writer.writerow(
                    [r.email, r.status.value, r.timestamp, r.duration_seconds, r.details]
                )

    def _export_markdown(self):
        md_path = self.settings.audit_report_md
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        status_counts = {}
        for r in self.records:
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1

        md_lines = [
            "<!--",
            "File: AUDIT_SUMMARY.md",
            f"Last Modified: {now_str}",
            "-->",
            "",
            "# Antigravity Account Verification - Audit Summary",
            "",
            f"- **Generated At**: `{now_str}`",
            f"- **Total Accounts Audited**: `{len(self.records)}`",
            "",
            "### Summary Metrics",
            "",
        ]

        badge_map = {
            VerificationStatus.SUCCESS_VERIFIED.value: "✅ `SUCCESS_VERIFIED`",
            VerificationStatus.ALREADY_VERIFIED.value: "✅ `ALREADY_VERIFIED`",
            VerificationStatus.ACCESS_FORBIDDEN.value: "🚫 `ACCESS_FORBIDDEN`",
            VerificationStatus.CHALLENGE_REQUIRED.value: "⚠️ `CHALLENGE_REQUIRED`",
            VerificationStatus.INVALID_CREDENTIALS.value: "🔑 `INVALID_CREDENTIALS`",
            VerificationStatus.SKIPPED_NO_PASSWORD.value: "⏭️ `SKIPPED_NO_PASSWORD`",
            VerificationStatus.ERROR.value: "❌ `ERROR`",
        }

        for st, count in status_counts.items():
            badge = badge_map.get(st, f"`{st}`")
            md_lines.append(f"- {badge}: **{count}**")

        md_lines.extend([
            "",
            "---",
            "",
            "### Verification Records",
            "",
            "| # | Account | Status | Duration | Diagnostic Details |",
            "| -: | :--- | :--- | :---: | :--- |",
        ])

        for idx, r in enumerate(self.records, start=1):
            badge = badge_map.get(r.status.value, f"`{r.status.value}`")
            clean_detail = r.details.replace("|", "\\|")
            md_lines.append(
                f"| {idx} | `{r.email}` | {badge} | {r.duration_seconds}s | {clean_detail} |"
            )

        md_lines.append("")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

    def print_summary(self):
        if not self.records:
            print("\nNo accounts were audited.")
            return

        status_counts = {}
        for r in self.records:
            status_counts[r.status.value] = status_counts.get(r.status.value, 0) + 1

        summary_parts = [f"{k}: {v}" for k, v in status_counts.items()]

        print("\n" + "=" * 65)
        print(f"{Fore.CYAN}{Style.BRIGHT}ANTIGRAVITY ACCOUNT VERIFICATION COMPLETE{Style.RESET_ALL}")
        print("=" * 65)
        print(f"Total Accounts Processed: {len(self.records)}")
        print(f"{Fore.YELLOW}Results: {', '.join(summary_parts)}{Style.RESET_ALL}")
        print("-" * 65)
        print(f"{Fore.GREEN}[*] Full Audit Summary saved to: {Fore.WHITE}AUDIT_SUMMARY.md{Style.RESET_ALL}")
        print(f"{Fore.GREEN}[*] Data exports saved to:      {Fore.WHITE}audit_report.csv, audit_report.json{Style.RESET_ALL}")
        print("=" * 65 + "\n")
