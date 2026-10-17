# Code Structure Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split two bloated files (`google_auth.py`, `main.py`) into focused modules and strip all comments/docstrings from every `.py` file — zero behavior change.

**Architecture:** Extract functions from the two large files into new modules (`auth/google_login.py`, `auth/challenge_handlers.py`, `auth/oauth_flow.py`, `core/account_processor.py`). The original files become thin orchestrators that import and delegate. All `__init__.py` re-exports preserved for backward compatibility.

**Tech Stack:** Python 3.14, selenium, undetected-chromedriver, existing deps only.

**Spec:** `docs/superpowers/specs/2026-10-17-refactor-design.md`

## Global Constraints

- Zero behavior change — every import path that worked before must work after.
- No new dependencies.
- All `#` comments, file headers, and `"""` docstrings removed from every file.
- Windows-only project; DPAPI/PowerShell code is intentional.
- `from auth.google_auth import GoogleAuthVerifier` and `from auth import GoogleAuthVerifier` must both work.

## Review Focus

1. **Circular imports** — `auth/oauth_flow.py` imports from `auth/phone_verifier.py` and `auth/db_sync.py`; verify no cycle exists.
2. **Missing function arguments** — methods that used `self.driver`/`self.settings` become standalone functions; verify every call passes `driver`/`settings` correctly.
3. **Return type consistency** — `login_and_verify` returns 3-tuples everywhere (VerificationStatus, str, dict); verify all new call sites propagate all three values.
4. **`__init__.py` re-exports** — verify `from auth import GoogleAuthVerifier` still resolves after `google_auth.py` is slimmed down.
5. **Import ordering** — verify no file imports a name that hasn't been defined yet due to split ordering.

---

### Task 1: Strip comments and docstrings from all well-scoped files

These files don't need structural changes, only comment/docstring removal:
`config/settings.py`, `config/__init__.py`, `loaders/account_loader.py`, `loaders/__init__.py`, `audit/auditor.py`, `audit/__init__.py`, `core/browser.py`, `core/__init__.py`, `auth/token_extractor.py`, `auth/phone_verifier.py`, `auth/profile_extractor.py`, `auth/db_sync.py`, `auth/__init__.py`.

**Files:**
- Modify: `config/settings.py`, `config/__init__.py`, `loaders/account_loader.py`, `loaders/__init__.py`, `audit/auditor.py`, `audit/__init__.py`, `core/browser.py`, `core/__init__.py`, `auth/token_extractor.py`, `auth/phone_verifier.py`, `auth/profile_extractor.py`, `auth/db_sync.py`, `auth/__init__.py`

**Interfaces:**
- Consumes: nothing
- Produces: cleaned files with identical public APIs

- [ ] **Step 1: Strip all `#` comment lines, inline `# ...` comments, file headers, and `"""` docstrings from each file**

Remove every `#` comment (whole-line and inline), every `"""..."""` / `'''...'''` docstring, and every `# File:` / `# Created:` / `# Last Modified:` / `# Purpose:` header. Keep blank lines between logical sections. Do not change any code.

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile <each file>`
Expected: no errors for any file.

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "refactor: strip all comments and docstrings from stable modules"
```

---

### Task 2: Create `auth/challenge_handlers.py`

Extract challenge detection and wait-loop functions from `google_auth.py`.

**Files:**
- Create: `auth/challenge_handlers.py`

**Interfaces:**
- Consumes: `selenium.webdriver` driver instance, `config.settings.Settings`
- Produces:
  - `is_security_rejection_present(driver) -> bool`
  - `is_bot_challenge_present(driver) -> bool`
  - `is_qr_challenge_present(driver) -> bool`
  - `wait_for_bot_challenge_resolution(driver, account_email: str, settings) -> bool`
  - `wait_for_qr_challenge_resolution(driver, account_email: str, settings) -> bool`
  - `wait_for_challenge_resolution(driver, timeout: int) -> bool`
  - `handle_oauth_consent(driver) -> None`
  - `ensure_active_window(driver) -> bool`
  - `current_url(driver) -> str`
  - `page_source(driver) -> str`

- [ ] **Step 1: Create `auth/challenge_handlers.py`**

Move the following methods from `GoogleAuthVerifier`, converting from `self.xxx` to function parameters:
- `_ensure_active_window` → `ensure_active_window(driver)`
- `_current_url` → `current_url(driver)`
- `_page_source` → `page_source(driver)`
- `_is_security_rejection_present` → `is_security_rejection_present(driver)`
- `_is_bot_challenge_present` → `is_bot_challenge_present(driver)`
- `_is_qr_challenge_present` → `is_qr_challenge_present(driver)`
- `_wait_for_bot_challenge_resolution` → `wait_for_bot_challenge_resolution(driver, account_email, settings)`
- `_wait_for_qr_challenge_resolution` → `wait_for_qr_challenge_resolution(driver, account_email, settings)`
- `_wait_for_challenge_resolution` → `wait_for_challenge_resolution(driver, timeout)`
- `_handle_oauth_consent` → `handle_oauth_consent(driver)`

All `self.driver` → `driver` parameter. All `self.settings` → `settings` parameter. All `self._current_url()` → `current_url(driver)`. All `self._page_source()` → `page_source(driver)`. All `self._ensure_active_window()` → `ensure_active_window(driver)`.

No comments or docstrings.

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile auth/challenge_handlers.py`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add auth/challenge_handlers.py
git commit -m "refactor: extract challenge handlers from google_auth"
```

---

### Task 3: Create `auth/google_login.py`

Extract the login mechanics from `google_auth.py`.

**Files:**
- Create: `auth/google_login.py`

**Interfaces:**
- Consumes: `auth.challenge_handlers` functions, `selenium` driver, `Account`, `VerificationStatus`
- Produces:
  - `click_next(driver, container_id: str) -> None`
  - `enter_password(driver, password: str) -> bool`
  - `perform_google_login(driver, account: Account, settings) -> Tuple[VerificationStatus, str, dict]`

- [ ] **Step 1: Create `auth/google_login.py`**

Move from `GoogleAuthVerifier`:
- `_click_next` → `click_next(driver, container_id)`
- `_enter_password` → `enter_password(driver, password)`
- `_perform_google_login` → `perform_google_login(driver, account, settings)`

`perform_google_login` calls `ensure_active_window`, `current_url`, `page_source`, `is_security_rejection_present`, `is_bot_challenge_present`, `wait_for_bot_challenge_resolution`, `is_qr_challenge_present`, `wait_for_qr_challenge_resolution`, `wait_for_challenge_resolution` — all imported from `auth.challenge_handlers`.

Replace `self._click_next(...)` → `click_next(driver, ...)`. Replace `self._enter_password(...)` → `enter_password(driver, ...)`.

No comments or docstrings.

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile auth/google_login.py`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add auth/google_login.py
git commit -m "refactor: extract google login flow from google_auth"
```

---

### Task 4: Create `auth/oauth_flow.py`

Extract OAuth consent, token exchange, API calls, and phone validation from `google_auth.py`.

**Files:**
- Create: `auth/oauth_flow.py`

**Interfaces:**
- Consumes: `auth.challenge_handlers` functions, `Account`, `VerificationStatus`, `config.settings`
- Produces:
  - `verify_via_cloudcode_api(driver, account: Account, settings) -> Tuple[VerificationStatus, str, dict]`
  - `call_onboard_user(access_token: str, account_email: str) -> Optional[dict]`
  - `call_load_code_assist(access_token: str, account_email: str) -> Tuple[Optional[dict], Optional[int], str]`
  - `extract_validation_url(data: Optional[dict], raw_err: str) -> Optional[str]`
  - `handle_phone_validation_challenge(driver, account, validation_url, access_token, refresh_token, settings) -> Tuple[VerificationStatus, str, dict]`

- [ ] **Step 1: Create `auth/oauth_flow.py`**

Move from `GoogleAuthVerifier`:
- All OAuth constants (`_AG_CLIENT_ID`, `_AG_CLIENT_SECRET`, `_AG_SCOPES`, `_AG_REDIRECT_PORT`, `_AG_REDIRECT_URI`, `_AG_USER_AGENT`, `_ONBOARD_USER_ENDPOINTS`, `_LOAD_CODE_ASSIST_ENDPOINTS`, `_STANDARD_IDE_METADATA`, `_AUTH_SUCCESS_LANDING_PAGE`)
- `_CallbackHandler` inner class → module-level `_CallbackHandler`
- `_verify_via_cloudcode_api` → `verify_via_cloudcode_api(driver, account, settings)`
- `_call_onboard_user` → `call_onboard_user(access_token, account_email)`
- `_call_load_code_assist` → `call_load_code_assist(access_token, account_email)`
- `_extract_validation_url` → `extract_validation_url(data, raw_err)`
- `_handle_phone_validation_challenge` → `handle_phone_validation_challenge(driver, account, validation_url, access_token, refresh_token, settings)`

`_CallbackHandler` uses a module-level list `_captured_code` (passed via closure or module state per call).

`verify_via_cloudcode_api` calls `ensure_active_window`, `current_url`, `is_qr_challenge_present`, `wait_for_qr_challenge_resolution`, `handle_oauth_consent` from `auth.challenge_handlers`.

`handle_phone_validation_challenge` calls `ensure_active_window`, `current_url` from `auth.challenge_handlers` and `PhoneValidationHandler` from `auth.phone_verifier`.

No comments or docstrings.

- [ ] **Step 2: Verify syntax**

Run: `python -m py_compile auth/oauth_flow.py`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add auth/oauth_flow.py
git commit -m "refactor: extract OAuth flow and API calls from google_auth"
```

---

### Task 5: Slim down `auth/google_auth.py` and update `auth/__init__.py`

Replace the bloated class with a thin orchestrator that imports from the three new modules.

**Files:**
- Modify: `auth/google_auth.py`
- Modify: `auth/__init__.py`

**Interfaces:**
- Consumes: `auth.google_login.perform_google_login`, `auth.oauth_flow.verify_via_cloudcode_api`, `auth.challenge_handlers.ensure_active_window/current_url/page_source`
- Produces: `GoogleAuthVerifier` class with same public API: `__init__(driver)`, `login_and_verify(account) -> Tuple[VerificationStatus, str, dict]`

- [ ] **Step 1: Rewrite `auth/google_auth.py`**

`GoogleAuthVerifier` keeps:
- `__init__(self, driver)` — stores `self.driver`, `self.settings`, `self.wait`
- `login_and_verify(self, account)` — same logic flow but calls:
  - `ensure_active_window(self.driver)` from `auth.challenge_handlers`
  - `current_url(self.driver)` / `page_source(self.driver)` from `auth.challenge_handlers`
  - `perform_google_login(self.driver, account, self.settings)` from `auth.google_login`
  - `verify_via_cloudcode_api(self.driver, account, self.settings)` from `auth.oauth_flow`

Remove all other methods — they now live in the three new modules.
No comments or docstrings.

- [ ] **Step 2: Update `auth/__init__.py`**

Keep re-exporting `GoogleAuthVerifier` and `TokenExtractor`. Add `PhoneValidationHandler`, `ProfileCookieExtractor`, `NineRouterDbSync` to `__init__.py` only if they were already there (they are not — leave `__init__.py` with just `GoogleAuthVerifier` and `TokenExtractor`).

No comments or docstrings.

- [ ] **Step 3: Verify syntax**

Run: `python -m py_compile auth/google_auth.py auth/__init__.py`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add auth/google_auth.py auth/__init__.py
git commit -m "refactor: slim google_auth.py to orchestrator, update __init__"
```

---

### Task 6: Create `core/account_processor.py` and slim `main.py`

Extract per-account processing logic from `main.py`.

**Files:**
- Create: `core/account_processor.py`
- Modify: `main.py`
- Modify: `core/__init__.py`

**Interfaces:**
- Consumes: `BrowserManager`, `AccountAuditor`, `AccountLoader`, `TokenExtractor`, `GoogleAuthVerifier`, `ProfileCookieExtractor`, `NineRouterDbSync`, `VerificationStatus`
- Produces:
  - `process_account(account, browser_manager, auditor, loader, extractor, extract_tokens, skipped_credentials) -> None`
  - `email_already_extracted(email: str, settings) -> bool`
  - `profile_exists(email: str, settings) -> bool`
  - `try_dpapi_extract(account_email: str, settings, extractor) -> bool`
  - `force_kill_chrome() -> None`
  - `launch_driver_with_timeout(browser_manager, account, timeout: int) -> driver`

- [ ] **Step 1: Create `core/account_processor.py`**

Move from `main.py`:
- `_email_already_extracted` → `email_already_extracted`
- `_profile_exists` → `profile_exists`
- `_force_kill_chrome` → `force_kill_chrome`
- `_launch_driver_with_timeout` → `launch_driver_with_timeout`
- `_try_dpapi_extract` → `try_dpapi_extract`
- `process_account` → `process_account`

All functions become module-level. No `_` prefix needed since they're no longer "private" helpers of `main`.

No comments or docstrings.

- [ ] **Step 2: Slim `main.py`**

Keep only:
- `parse_args()`
- `display_account_list(accounts)`
- `main()`

`main()` imports `process_account` from `core.account_processor`.

Strip all comments and docstrings.

- [ ] **Step 3: Update `core/__init__.py`**

Add `account_processor` exports: `from .account_processor import process_account`.
Strip comments.

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile core/account_processor.py main.py core/__init__.py`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add core/account_processor.py main.py core/__init__.py
git commit -m "refactor: extract account processing from main.py"
```

---

### Task 7: Full integration verification

**Files:**
- All modified/created files

- [ ] **Step 1: Verify all files compile**

Run: `python -m py_compile main.py config/settings.py config/__init__.py loaders/account_loader.py loaders/__init__.py audit/auditor.py audit/__init__.py core/browser.py core/__init__.py core/account_processor.py auth/google_auth.py auth/google_login.py auth/challenge_handlers.py auth/oauth_flow.py auth/token_extractor.py auth/phone_verifier.py auth/profile_extractor.py auth/db_sync.py auth/__init__.py`
Expected: no errors.

- [ ] **Step 2: Verify backward-compatible imports**

Run a Python script that tests:
```python
from auth.google_auth import GoogleAuthVerifier
from auth import GoogleAuthVerifier
from auth.token_extractor import TokenExtractor
from core.browser import BrowserManager
from audit.auditor import AccountAuditor, VerificationStatus
from loaders.account_loader import AccountLoader, Account
from config.settings import get_settings
from core.account_processor import process_account
```
Expected: no ImportError.

- [ ] **Step 3: Verify `main.py --help` works**

Run: `python main.py --help`
Expected: prints argument help text, exits 0.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: verify full integration after structure refactor"
```
