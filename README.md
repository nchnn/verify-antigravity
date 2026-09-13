<!--
File: README.md
Created: 2026-09-13 12:01 AM
Last Modified: 2026-09-14 02:18 AM
-->

# Antigravity Account Verification Suite - How to Use

A step-by-step guide for configuring, managing, and automating Google account verification for Antigravity.

---

## 1. Quick Overview

The tool automates logging into Google accounts using `undetected-chromedriver`, visiting the Antigravity portal to accept access/OAuth permissions, and recording whether accounts are verified or "Access forbidden".

### Project Structure
- `accounts.txt`: The list of accounts to verify.
- `.env`: Holds your global fallback password and runtime settings.
- `main.py`: The CLI runner.
- `audit_report.csv` & `audit_report.json`: Automatically generated result logs.

---

## 2. Configuration (`.env`)

Open `.env` to configure your settings:

```dotenv
# Global default password for accounts that don't specify one
DEFAULT_ACCOUNT_PASSWORD=YourPasswordHere

# Target portal URL to verify
ANTIGRAVITY_PORTAL_URL=https://antigravity.google/docs

# Browser display mode (false = shows Chrome window, true = hidden)
BROWSER_HEADLESS=false

# Wait timings (in seconds)
PAGE_TIMEOUT_SECONDS=30
ACTION_DELAY_SECONDS=2

# Persistent sessions to keep verified accounts logged in
PERSISTENT_PROFILES=true
PROFILES_DIR=chrome_profiles
```

---

## 3. Managing Accounts (`accounts.txt`)

Add or remove accounts in `accounts.txt` at any time.

### Supported Formats:
1. **Using the Default Password** (Recommended when all accounts share one password):
   ```text
   user1@gmail.com:
   user2@gmail.com
   ```
2. **Using a Custom Password**:
   ```text
   specialuser@gmail.com:CustomSecretPassword123
   ```
3. **Comments and Empty Lines**:
   Lines beginning with `#` and empty lines are automatically ignored.
4. **Auto-Delete on Success**:
   When an account is successfully verified, it is automatically removed from `accounts.txt`. Any accounts that fail or require manual attention remain in the file for easy re-running.

---

## 4. Commands & Examples

Run these commands in PowerShell or your terminal from the project folder:

### A. Preview Accounts (Dry Run)
Check your account list and verify passwords resolve without opening the browser:
```powershell
python main.py --list
```

### B. Verify a Single Account
Test one specific account (useful for testing accounts showing "Access forbidden"):
```powershell
python main.py -a forbuygoods4@gmail.com
```

### C. Verify All Accounts in Batch
Iterates through all accounts in `accounts.txt`:
```powershell
python main.py
```

### D. Run in Headless Mode (Hidden Window)
Run without opening the visible Chrome browser:
```powershell
python main.py --headless
```

> **Tip**: Running with a visible browser (default, without `--headless`) is recommended on the first run. If Google triggers a 2-Step Verification prompt or phone tap, the script pauses for up to 60 seconds so you can tap or confirm it on screen.

---

## 5. Reviewing Verification Audit Logs

After execution completes, the tool prints a clean, compact status summary in the terminal:

```text
=================================================================
ANTIGRAVITY ACCOUNT VERIFICATION COMPLETE
=================================================================
Total Accounts Processed: 5
Results: SUCCESS_VERIFIED: 3, ACCESS_FORBIDDEN: 2, ERROR: 0
-----------------------------------------------------------------
[*] Full Audit Summary saved to: AUDIT_SUMMARY.md
[*] Data exports saved to:      audit_report.csv, audit_report.json
=================================================================
```

### Output Files Generated:
- **`AUDIT_SUMMARY.md`**: Dedicated Markdown report featuring summary badges, formatted table of all audited accounts, execution duration, and diagnostic details.
- **`audit_report.csv`**: Can be opened in Microsoft Excel or Google Sheets for spreadsheet tracking.
- **`audit_report.json`**: Structured JSON report containing raw run metrics and timestamps.

### Status Meanings:
| Status | Description |
| :--- | :--- |
| `SUCCESS_VERIFIED` | Logged in and Antigravity access is verified and active. |
| `ACCESS_FORBIDDEN` | Logged into Google, but Antigravity denies access (account requires waitlist/whitelist approval). |
| `CHALLENGE_REQUIRED` | Google 2FA or verification prompt was triggered and timed out. |
| `INVALID_CREDENTIALS` | Email was not found or password was incorrect. |
| `ERROR` | Unexpected error or network timeout. |

---

## 6. Token & Cookie Extraction (for 9router)

When an account is successfully verified, the tool automatically extracts:
- Full session cookies (`SAPISID`, `SSID`, `SID`, `HSID`, etc.)
- Ready-to-use HTTP `Cookie:` header string
- Bearer tokens and OAuth keys from `localStorage` / `sessionStorage`

### Output Files Generated:
- **`verified_tokens.json`**: Complete structured JSON containing all accounts, ideal for programmatic importing into `9router`.
- **`verified_tokens.txt`**: Compact, line-by-line format:
  ```text
  user@gmail.com | Token: ya29... | Cookie: SAPISID=...; SSID=...; SID=...
  ```

### CLI Flag:
If you ever want to run verification without extracting tokens, pass `--no-extract-tokens`:
```powershell
python main.py --no-extract-tokens
```
