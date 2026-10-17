import sys
import time
import argparse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config.settings import get_settings
from loaders.account_loader import AccountLoader, Account
from audit.auditor import AccountAuditor, VerificationStatus
from core.browser import BrowserManager
from auth.token_extractor import TokenExtractor
from core.account_processor import process_account


def parse_args():
    parser = argparse.ArgumentParser(
        description="Antigravity Account Verification & Google OAuth Automation Suite"
    )
    parser.add_argument(
        "-a",
        "--account",
        type=str,
        default=None,
        help="Target a specific email from accounts.txt (or all accounts if omitted)",
    )
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List loaded accounts and password resolution without running automation",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default is headed for interactive handling)",
    )
    parser.add_argument(
        "--accounts-file",
        type=str,
        default=None,
        help="Path to custom accounts text file (default: accounts.txt)",
    )
    parser.add_argument(
        "--no-extract-tokens",
        action="store_true",
        help="Disable automatic extraction of tokens and cookies upon verification",
    )
    return parser.parse_args()


def display_account_list(accounts: list[Account]):
    print(f"\nFound {len(accounts)} accounts:")
    print("-" * 60)
    for i, acc in enumerate(accounts, start=1):
        pw_type = "DEFAULT FALLBACK" if acc.is_fallback_password else "CUSTOM"
        print(f"[{i:02d}] {acc.email:<35} | {pw_type:<16} | Pass: {acc.mask_password()}")
    print("-" * 60 + "\n")


def main():
    args = parse_args()
    settings = get_settings()
    loader = AccountLoader(args.accounts_file)
    auditor = AccountAuditor()
    extractor = TokenExtractor()

    extract_enabled = settings.extract_tokens and not args.no_extract_tokens

    try:
        accounts = loader.load_accounts(target_email=args.account)
    except Exception as e:
        print(f"Error loading accounts: {e}")
        sys.exit(1)

    if not accounts:
        print("No matching accounts found to process.")
        sys.exit(0)

    if args.list:
        display_account_list(accounts)
        sys.exit(0)

    print("\n" + "=" * 60)
    print("STARTING ANTIGRAVITY ACCOUNT VERIFICATION")
    print(f"Target count: {len(accounts)}")
    print(f"Headless mode: {args.headless or settings.browser_headless}")
    print(f"Extract tokens/cookies: {extract_enabled}")
    print("=" * 60 + "\n")

    browser_manager = BrowserManager(headless=args.headless or settings.browser_headless)
    skipped_credentials: list[str] = []

    try:
        for index, acc in enumerate(accounts, start=1):
            print(f"\n>>> Processing [{index}/{len(accounts)}]: {acc.email}")
            process_account(
                account=acc,
                browser_manager=browser_manager,
                auditor=auditor,
                loader=loader,
                extractor=extractor,
                extract_tokens=extract_enabled,
                skipped_credentials=skipped_credentials,
            )
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nVerification process interrupted by user.")
    finally:
        auditor.export_reports()
        auditor.print_summary()

        if skipped_credentials:
            sep = "=" * 60
            print(f"\n{sep}")
            print(f"\u26a0\ufe0f  SKIPPED ACCOUNTS \u2014 INVALID CREDENTIALS ({len(skipped_credentials)})")
            print(f"{sep}")
            print("The following accounts were skipped due to incorrect password")
            print("or unrecognized email. They remain in accounts.txt so you can")
            print("fix the password and re-run.\n")
            for i, email in enumerate(skipped_credentials, start=1):
                print(f"  [{i:02d}] {email}")
            print(f"\n{sep}\n")


if __name__ == "__main__":
    main()
