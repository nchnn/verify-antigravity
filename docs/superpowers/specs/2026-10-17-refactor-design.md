# Refactor Design: Code Structure Cleanup & Comment Removal

## Intent & Why

The codebase is a working Google account verification + OAuth token extraction suite
("Antigravity"). It works, but two files are bloated and hard to navigate, and every
file is cluttered with comments/docstrings that restate what the code already says
through naming and types. The owner wants it readable, scannable, and easy to maintain.

## Goals

1. **Remove all comments and docstrings** from every `.py` file — `#` comments, file
   headers (`# File:`, `# Created:`), and `"""..."""` docstrings. No exceptions.
2. **Split `google_auth.py` (1,096 lines)** into focused modules, each ≤ ~300 lines,
   with a single responsibility.
3. **Split `main.py` (454 lines)** — extract processing logic out, leave a thin entry
   point.
4. **Preserve all existing behavior** — zero functional changes. Every import path that
   worked before must still work after (via `__init__.py` re-exports).

## Non-Goals

- No new features, no refactoring logic, no renaming existing public APIs.
- No changes to files that are already well-scoped (just strip their comments).
- No test suite creation (none exists today).

## Anti-Goals (what would make this a failure)

- Breaking any import — `from auth.google_auth import GoogleAuthVerifier` must still
  work after the split.
- Changing any runtime behavior, argument parsing, or output format.
- Losing any function or class.

## Current State

```
verifyAntigravity/
├── main.py                          (454 lines) ← bloated
├── config/
│   ├── __init__.py
│   └── settings.py                  (68 lines)  ← fine
├── loaders/
│   ├── __init__.py
│   └── account_loader.py           (133 lines) ← fine
├── audit/
│   ├── __init__.py
│   └── auditor.py                  (215 lines) ← fine
├── core/
│   ├── __init__.py
│   └── browser.py                  (323 lines) ← fine
├── auth/
│   ├── __init__.py
│   ├── google_auth.py              (1,096 lines) ← bloated
│   ├── token_extractor.py          (491 lines) ← fine
│   ├── phone_verifier.py           (273 lines) ← fine
│   ├── profile_extractor.py        (315 lines) ← fine
│   └── db_sync.py                  (163 lines) ← fine
```

## Target State

```
verifyAntigravity/
├── main.py                          (~40 lines) — parse_args + main, delegates to processor
├── config/
│   ├── __init__.py
│   └── settings.py                  (~55 lines)
├── loaders/
│   ├── __init__.py
│   └── account_loader.py           (~95 lines)
├── audit/
│   ├── __init__.py
│   └── auditor.py                  (~170 lines)
├── core/
│   ├── __init__.py
│   ├── browser.py                  (~260 lines)
│   └── account_processor.py        (NEW, ~200 lines) — process_account, DPAPI extract,
│                                     chrome helpers, email-already-extracted check
├── auth/
│   ├── __init__.py                  — re-exports GoogleAuthVerifier (backward compat)
│   ├── google_auth.py              (~80 lines) — GoogleAuthVerifier.login_and_verify
│   │                                 orchestrator only, delegates to sub-modules
│   ├── google_login.py             (NEW, ~180 lines) — _perform_google_login,
│   │                                 _enter_password, _click_next, email/password entry
│   ├── challenge_handlers.py       (NEW, ~250 lines) — bot challenge, QR challenge,
│   │                                 2FA/phone wait, security rejection detection
│   ├── oauth_flow.py               (NEW, ~280 lines) — OAuth consent navigation,
│   │                                 local callback server, code→token exchange,
│   │                                 onboardUser, loadCodeAssist, phone validation handler
│   ├── token_extractor.py          (~380 lines)
│   ├── phone_verifier.py           (~200 lines)
│   ├── profile_extractor.py        (~230 lines)
│   └── db_sync.py                  (~120 lines)
```

## Constraints

- **Python 3.14** — no compatibility concerns with older versions.
- **No new dependencies** — only stdlib + what's in requirements.txt.
- **Windows only** — DPAPI, PowerShell Chrome version detection, etc. are intentionally
  Windows-specific.
- **Backward-compatible imports** — `auth/__init__.py` must re-export
  `GoogleAuthVerifier` so `from auth.google_auth import GoogleAuthVerifier` and
  `from auth import GoogleAuthVerifier` both still work.

## The Split — Detailed

### 1. `google_auth.py` → 4 files

**`auth/google_auth.py`** (orchestrator):
- `GoogleAuthVerifier.__init__`
- `GoogleAuthVerifier.login_and_verify` — checks profile cookies, calls login, calls
  API verification. Delegates everything else.
- `_ensure_active_window`, `_current_url`, `_page_source` — tiny driver helpers used
  everywhere, stay here.
- Imports and calls into `google_login`, `challenge_handlers`, `oauth_flow`.

**`auth/google_login.py`** (login mechanics):
- `perform_google_login(driver, account, settings)` — the full email→password flow
- `enter_password(driver, password)` — locate and type password
- `click_next(driver, container_id)` — click Next button

**`auth/challenge_handlers.py`** (challenge detection & wait loops):
- `is_security_rejection_present(driver)` → bool
- `is_bot_challenge_present(driver)` → bool
- `is_qr_challenge_present(driver)` → bool
- `wait_for_bot_challenge_resolution(driver, email, settings)` → bool
- `wait_for_qr_challenge_resolution(driver, email, settings)` → bool
- `wait_for_challenge_resolution(driver, timeout)` → bool
- `handle_oauth_consent(driver)` — click Allow/Continue buttons

**`auth/oauth_flow.py`** (OAuth + API):
- `verify_via_cloudcode_api(driver, account, settings)` — full OAuth flow
- `call_onboard_user(access_token, email)` → dict
- `call_load_code_assist(access_token, email)` → tuple
- `extract_validation_url(data, raw_err)` → str
- `handle_phone_validation_challenge(driver, account, url, access_token, refresh_token, settings)` → tuple
- OAuth constants (`_AG_CLIENT_ID`, `_AG_SCOPES`, etc.)
- `_CallbackHandler` HTTP handler class

### 2. `main.py` → 2 files

**`main.py`** (entry point only):
- `parse_args()` — CLI argument parser
- `main()` — load settings, load accounts, print banner, loop calling
  `process_account`, print summary
- `display_account_list()`

**`core/account_processor.py`** (per-account logic):
- `process_account(account, browser_manager, auditor, loader, extractor, ...)` — the
  full per-account pipeline
- `email_already_extracted(email, settings)` → bool
- `profile_exists(email, settings)` → bool
- `try_dpapi_extract(email, settings, extractor)` → bool
- `force_kill_chrome()` — kill lingering Chrome processes
- `launch_driver_with_timeout(browser_manager, account, timeout)` — threaded launch

### 3. Comment stripping — all files

Every `.py` file: remove all `#` comment lines, all inline `# ...` comments, all
`"""..."""` / `'''...'''` docstrings. Keep blank lines between logical sections for
readability.

## What's Left to the Builder

- Exact line-by-line placement of blank lines for readability after comment removal.
- Whether challenge handler functions become standalone functions or stay as methods on
  a helper class (standalone functions are preferred for simplicity, but either works).
- Import style within the new modules (relative vs absolute — match what the codebase
  already uses, which is absolute).
