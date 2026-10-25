import sys
import time
import argparse
from colorama import init, Fore, Style

init(autoreset=True)

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
    print(f"\n{Fore.CYAN}Found {len(accounts)} accounts:{Style.RESET_ALL}")
    print(Fore.CYAN + "-" * 60 + Style.RESET_ALL)
    for i, acc in enumerate(accounts, start=1):
        pw_type = "DEFAULT FALLBACK" if acc.is_fallback_password else "CUSTOM"
        pw_color = Fore.YELLOW if acc.is_fallback_password else Fore.GREEN
        print(
            f"{Fore.WHITE}[{i:02d}] {Fore.CYAN}{acc.email:<35}{Style.RESET_ALL}"
            f" | {pw_color}{pw_type:<16}{Style.RESET_ALL}"
            f" | Pass: {acc.mask_password()}"
        )
    print(Fore.CYAN + "-" * 60 + Style.RESET_ALL + "\n")


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
        print(f"{Fore.RED}{Style.BRIGHT}Error loading accounts: {e}{Style.RESET_ALL}")
        sys.exit(1)

    if not accounts:
        print(f"{Fore.YELLOW}No matching accounts found to process.{Style.RESET_ALL}")
        sys.exit(0)

    if args.list:
        display_account_list(accounts)
        sys.exit(0)

    print("\n" + Fore.CYAN + Style.BRIGHT + "=" * 60 + Style.RESET_ALL)
    print(Fore.CYAN + Style.BRIGHT + "STARTING ANTIGRAVITY ACCOUNT VERIFICATION" + Style.RESET_ALL)
    print(f"{Fore.WHITE}Target count: {Fore.YELLOW}{len(accounts)}{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Headless mode: {Fore.YELLOW}{args.headless or settings.browser_headless}{Style.RESET_ALL}")
    print(f"{Fore.WHITE}Extract tokens/cookies: {Fore.YELLOW}{extract_enabled}{Style.RESET_ALL}")
    print(Fore.CYAN + Style.BRIGHT + "=" * 60 + Style.RESET_ALL + "\n")

    browser_manager = BrowserManager(headless=args.headless or settings.browser_headless)
    skipped_credentials: list[str] = []

    try:
        for index, acc in enumerate(accounts, start=1):
            print(f"\n{Fore.CYAN}{Style.BRIGHT}>>> Processing [{index}/{len(accounts)}]: {acc.email}{Style.RESET_ALL}")
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
        print(f"\n{Fore.YELLOW}{Style.BRIGHT}Verification process interrupted by user.{Style.RESET_ALL}")
    finally:
        auditor.export_reports()
        auditor.print_summary()

        if skipped_credentials:
            sep = Fore.RED + Style.BRIGHT + "=" * 60 + Style.RESET_ALL
            print(f"\n{sep}")
            print(f"{Fore.RED}{Style.BRIGHT}⚠️  SKIPPED ACCOUNTS — INVALID CREDENTIALS ({len(skipped_credentials)}){Style.RESET_ALL}")
            print(f"{sep}")
            print(f"{Fore.YELLOW}The following accounts were skipped due to incorrect password")
            print("or unrecognized email. They remain in accounts.txt so you can")
            print(f"fix the password and re-run.{Style.RESET_ALL}\n")
            for i, email in enumerate(skipped_credentials, start=1):
                print(f"  {Fore.YELLOW}[{i:02d}] {email}{Style.RESET_ALL}")
            print(f"\n{sep}\n")


if __name__ == "__main__":
    main()
