# Anti-Detection & Bot-Spoofing Enhancement Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the Selenium login flow against Google's bot detection and repeat-login heuristics across all possible fingerprint vectors.

**Architecture:** Layered spoofing — browser fingerprint layer (Chrome options + CDP patches), behavioral layer (timing/input realism), session layer (profile + cookie continuity), network layer (proxy + IP rotation), and HTTP layer (realistic headers in OAuth requests).

**Tech Stack:** `undetected-chromedriver`, `selenium`, `fake-useragent` (or static UA pool), standard `http.client`, `json`, `random`, Python stdlib. No new framework deps unless noted per task.

**Spec:** This plan implements the anti-detection audit discussed in session on 2026-10-25. No separate spec doc.

## Global Constraints

- No Zod, no new heavy framework deps — stdlib + already-installed packages first.
- `undetected-chromedriver` stays as the Chrome driver; do not swap to plain selenium.
- All new Chrome flags go inside `BrowserManager.create_driver` in `core/browser.py`.
- All new CDP patches go in a dedicated `core/browser_patches.py` helper called once after driver creation.
- Phrase lists stay in `auth/challenge_handlers.py`; no new phrase constants elsewhere.
- Match existing colorama color conventions for any new log lines.
- Python 3.14 target — no `distutils`; `setuptools` shim already assumed present.

## Review Focus

- UA pool exhaustion: if `fake-useragent` fetch fails at runtime, fallback must be a hardcoded realistic string, not an empty string.
- Proxy auth failure: `--proxy-server` with auth credentials may not work in all Chrome versions — test with a real proxy before relying on it.
- CDP patch timing: `Page.addScriptToEvaluateOnNewDocument` must be called after `driver` is created but before any `driver.get()` — order matters.
- Canvas/audio noise randomness: must produce different values per account session, not per run (profile-bound seed, not process-level `random.seed()`).
- Cookie injection: `driver.add_cookie()` requires the driver to be on the same domain first — always `driver.get("https://accounts.google.com")` before injecting.

---

## Task 1: Browser Fingerprint Hardening (CDP Patches)

**Files:**
- Create: `core/browser_patches.py`
- Modify: `core/browser.py` (call patches after driver creation)

**Interfaces:**
- Produces: `apply_stealth_patches(driver: uc.Chrome) -> None`
- Called in `BrowserManager.create_driver` immediately after `self.driver = uc.Chrome(...)`.

**What this covers:**
- Remove `navigator.webdriver` trace (backup to what `uc` does)
- Spoof `navigator.plugins` (non-empty, realistic)
- Spoof `navigator.languages` → `["en-US", "en"]`
- Spoof `navigator.hardwareConcurrency` → random 4/8/12/16
- Spoof `navigator.deviceMemory` → random 4/8
- Spoof `screen.colorDepth` → 24
- Spoof `window.chrome` object (present, non-null)
- Add realistic `Permissions.query` mock so `navigator.permissions.query({name:'notifications'})` returns `"denied"` not `"prompt"` (automation default)

- [ ] **Step 1: Create `core/browser_patches.py`**

  Implement `apply_stealth_patches(driver)`. Use `driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": JS_STRING})` with a single JS block covering all spoofs above. Seed randomness from `hash(driver.session_id)` inside the JS — pass random values as Python-generated literals injected into the JS string so they're fixed per session but differ across accounts.

  Key JS values to inject (generate in Python, embed as literals):
  ```python
  hw_concurrency = random.choice([4, 8, 12, 16])
  device_memory = random.choice([4, 8])
  ```

- [ ] **Step 2: Verify patch loads**

  After creating a driver in a test script, navigate to `https://bot.sannysoft.com` or `https://arh.antoinevastel.com/bots/areyouheadless` and capture `page_source`. Confirm `webdriver` field shows `false` or missing.

- [ ] **Step 3: Wire into `BrowserManager.create_driver` in `core/browser.py`**

  After `self.driver = uc.Chrome(...)` and before `return self.driver`, add:
  ```python
  from core.browser_patches import apply_stealth_patches
  apply_stealth_patches(self.driver)
  ```

- [ ] **Step 4: Commit**

  ```bash
  git add core/browser_patches.py core/browser.py
  git commit -m "feat: add CDP stealth patches (navigator, plugins, chrome object spoofs)"
  ```

---

## Task 2: Realistic User-Agent Rotation Per Account

**Files:**
- Modify: `core/browser.py` — add UA selection in `create_driver`
- Modify: `core/browser_patches.py` — override UA via CDP after driver starts

**Interfaces:**
- Produces: `get_ua_for_account(email: str) -> str` (deterministic per email, stable across retries)
- Consumes: account email string

**What this covers:**

Currently no UA is set on the browser (only on HTTP requests in `oauth_flow.py`). Chrome's default `HeadlessChrome/...` UA is a bot signal. Fix: set a realistic desktop Chrome UA **matching the installed Chrome version** via `options.add_argument("--user-agent=...")` AND patch `navigator.userAgent` via CDP so JS reads the same value.

- [ ] **Step 1: Build `get_ua_for_account(email: str) -> str` in `core/browser_patches.py`**

  Use a static list of 6–8 realistic Chrome Windows UAs (no `HeadlessChrome`, no `Automation`). Pick deterministically by `hash(email) % len(pool)` so the same account always gets the same UA across retries (stable fingerprint). If `fake-useragent` is already installed (`import fake_useragent` succeeds), pull from it as primary with the static pool as fallback.

  Example pool entry:
  ```
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
  ```

- [ ] **Step 2: Apply UA in `create_driver`**

  Before `uc.Chrome(...)`:
  ```python
  ua = get_ua_for_account(account_email or "default")
  options.add_argument(f"--user-agent={ua}")
  ```
  Then in `apply_stealth_patches`, also patch via CDP:
  ```js
  Object.defineProperty(navigator, 'userAgent', { get: () => '<ua>' });
  Object.defineProperty(navigator, 'appVersion', { get: () => '<ua_stripped>' });
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add core/browser_patches.py core/browser.py
  git commit -m "feat: per-account deterministic UA rotation + CDP navigator.userAgent patch"
  ```

---

## Task 3: Canvas & Audio Fingerprint Noise

**Files:**
- Modify: `core/browser_patches.py` — add canvas/audio noise to the CDP script

**Interfaces:**
- No new public interface. Extends `apply_stealth_patches` JS block.

**What this covers:**

Canvas fingerprinting: Google (and fingerprint.js) calls `canvas.toDataURL()` and reads pixel bytes. If the same browser instance always produces the exact same canvas output, it's a tracking signal. Fix: patch `HTMLCanvasElement.prototype.toDataURL` and `CanvasRenderingContext2D.prototype.getImageData` to add per-session sub-pixel noise (±1 on random pixels).

Audio fingerprinting: `OfflineAudioContext` produces a deterministic float sum. Patch `AudioBuffer.prototype.getChannelData` to nudge values by `±1e-7`.

Noise must be:
- Consistent within a session (same seed per driver session)
- Different per account (seed from session ID or account email hash)
- Tiny enough not to break visual rendering

- [ ] **Step 1: Add canvas noise patch to JS block in `apply_stealth_patches`**

  JS approach:
  ```js
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type) {
    const ctx = this.getContext('2d');
    if (ctx) {
      const imageData = ctx.getImageData(0, 0, this.width, this.height);
      for (let i = 0; i < imageData.data.length; i += 100) {
        imageData.data[i] ^= NOISE_SEED & 0xff;
      }
      ctx.putImageData(imageData, 0, 0);
    }
    return _toDataURL.apply(this, arguments);
  };
  ```
  `NOISE_SEED` is a Python-generated random int per session, embedded as a literal.

- [ ] **Step 2: Add audio noise patch**

  ```js
  const _getChannelData = AudioBuffer.prototype.getChannelData;
  AudioBuffer.prototype.getChannelData = function(channel) {
    const data = _getChannelData.call(this, channel);
    for (let i = 0; i < data.length; i += 100) {
      data[i] += AUDIO_NOISE * 1e-7;
    }
    return data;
  };
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add core/browser_patches.py
  git commit -m "feat: canvas and audio fingerprint noise injection per session"
  ```

---

## Task 4: Human-Like Mouse Movement (Pre-Click Drift)

**Files:**
- Create: `core/human_mouse.py`
- Modify: `auth/google_login.py` — replace `ActionChains.move_to_element().click()` calls

**Interfaces:**
- Produces: `human_click(driver, element) -> None`
- Consumes: `selenium` `WebElement`

**What this covers:**

Current code does `move_to_element → pause → click` in a straight line. Real humans move the mouse in a curved path (Bézier curve approximation) with micro-jitter. Fix: generate 8–12 intermediate points along a slightly curved path to the element, move through them with small random delays.

- [ ] **Step 1: Create `core/human_mouse.py`**

  Implement `human_click(driver, element)`:
  - Get element center via `element.location` + `element.size`
  - Generate 8–12 Bézier interpolation points between current mouse position (tracked as a module-level variable defaulting to viewport center) and target
  - Add ±5px jitter to each intermediate point
  - Use `ActionChains.move_by_offset` steps with `time.sleep(random.uniform(0.01, 0.04))` between each
  - Final click via `ActionChains.click(element).perform()`
  - Update tracked position after click

  No new deps — pure `ActionChains` + `random` + `math`.

- [ ] **Step 2: Replace direct clicks in `auth/google_login.py`**

  Replace the two `ActionChains(driver).move_to_element(btn).pause(...).click().perform()` calls in `click_next` with `human_click(driver, btn)`.

- [ ] **Step 3: Commit**

  ```bash
  git add core/human_mouse.py auth/google_login.py
  git commit -m "feat: Bézier-path human mouse movement for all login clicks"
  ```

---

## Task 5: Randomized Inter-Action Delays

**Files:**
- Create: `core/human_timing.py`
- Modify: `auth/google_login.py` — replace bare `time.sleep(...)` literals

**Interfaces:**
- Produces: `think_pause()`, `micro_pause()`, `action_pause()`
- All return `None`, call `time.sleep` internally

**What this covers:**

Current delays are `random.uniform(0.02, 0.07)` for typing and `time.sleep(1.0)` flat waits. Flat integer sleeps are a bot signal — real humans don't sleep exactly 1.000s. Fix: replace all fixed sleeps with human-distribution draws (normal distribution clamped to a range).

| Function | Distribution | Range |
|---|---|---|
| `micro_pause()` | uniform | 15–80ms (between keystrokes) |
| `think_pause()` | normal µ=0.8, σ=0.2 | clamped 0.4–1.8s (after field fill, before submit) |
| `action_pause()` | normal µ=0.3, σ=0.1 | clamped 0.15–0.7s (between UI actions) |

- [ ] **Step 1: Create `core/human_timing.py`**

  ```python
  import time, random
  def micro_pause(): time.sleep(random.uniform(0.015, 0.08))
  def think_pause(): time.sleep(max(0.4, min(1.8, random.normalvariate(0.8, 0.2))))
  def action_pause(): time.sleep(max(0.15, min(0.7, random.normalvariate(0.3, 0.1))))
  ```

- [ ] **Step 2: Replace sleeps in `auth/google_login.py`**

  - Per-character sleep → `micro_pause()`
  - `time.sleep(random.uniform(0.2, 0.5))` after field fill → `think_pause()`
  - `time.sleep(0.3)` after key press → `action_pause()`
  - `time.sleep(1.0)` page waits stay as-is (these are actual load waits, not human timing)

- [ ] **Step 3: Commit**

  ```bash
  git add core/human_timing.py auth/google_login.py
  git commit -m "feat: human-distribution timing replacing flat sleeps in login flow"
  ```

---

## Task 6: Scroll & Viewport Interaction Before Login

**Files:**
- Modify: `auth/google_login.py` — add pre-form interaction in `perform_google_login`

**Interfaces:**
- No new public interface. Inline behavior added before email field interaction.

**What this covers:**

Real humans don't instantly focus the email field the moment the page loads. They spend 0.5–2s visually scanning, may scroll slightly, move the mouse around. Fix: after `driver.get(sign_in_url)`, do a brief realistic pre-interaction sequence before locating the email input.

- [ ] **Step 1: Add pre-interaction sequence in `perform_google_login` after `driver.get(...)`**

  ```python
  # Simulate reading the page before interacting
  time.sleep(random.uniform(0.5, 1.2))
  driver.execute_script(f"window.scrollBy(0, {random.randint(30, 80)})")
  time.sleep(random.uniform(0.3, 0.7))
  driver.execute_script("window.scrollBy(0, 0)")  # scroll back up
  ```

  Place this after `ensure_active_window(driver)` on line 81, before the `toomanysessions` check.

- [ ] **Step 2: Commit**

  ```bash
  git add auth/google_login.py
  git commit -m "feat: pre-login scroll and viewport interaction to mimic human page reading"
  ```

---

## Task 7: Timezone & Locale Fingerprint Match

**Files:**
- Modify: `core/browser.py` — add CDP timezone emulation
- Modify: `core/browser_patches.py` — patch `Intl.DateTimeFormat` timezone

**Interfaces:**
- No new public interface. Extends driver setup.

**What this covers:**

If an account's proxy is in a different timezone than the Chrome process, the `Date.getTimezoneOffset()` and `Intl.DateTimeFormat().resolvedOptions().timeZone` values won't match the IP's geo — a detection signal. Fix: when a proxy is configured for an account, set Chrome's timezone emulation via CDP to match.

For accounts without proxy: set a consistent realistic timezone (e.g. `"America/New_York"`) rather than the host machine's system timezone (which could be anything).

- [ ] **Step 1: Add timezone map to `core/browser_patches.py`**

  Small dict mapping country/region proxy prefixes to IANA timezone strings. If proxy is `None`, default to `"America/New_York"`. Expose `get_timezone_for_proxy(proxy: Optional[str]) -> str`.

- [ ] **Step 2: Apply CDP timezone emulation in `BrowserManager.create_driver`**

  After driver creation:
  ```python
  tz = get_timezone_for_proxy(proxy)
  self.driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": tz})
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add core/browser.py core/browser_patches.py
  git commit -m "feat: CDP timezone emulation per account proxy region"
  ```

---

## Task 8: HTTP Header Hardening for OAuth Requests

**Files:**
- Modify: `auth/oauth_flow.py` — update headers on all `urllib.request` calls

**Interfaces:**
- No new public interface. Modifies existing `call_onboard_user`, `call_load_code_assist`, and token exchange request headers.

**What this covers:**

Currently only `User-Agent` and `Content-Type` are set on HTTP requests. A real IDE client sends `Accept`, `Accept-Language`, `Accept-Encoding`, `Connection`, `X-Goog-Request-Reason` and potentially `Origin` headers. Missing standard headers make the request look like a script.

- [ ] **Step 1: Define `_STANDARD_HEADERS` dict in `oauth_flow.py`**

  ```python
  _STANDARD_HEADERS = {
      "User-Agent": _AG_USER_AGENT,
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-US,en;q=0.9",
      "Accept-Encoding": "gzip, deflate, br",
      "Connection": "keep-alive",
      "X-Goog-Request-Reason": "IDE integration",
  }
  ```

- [ ] **Step 2: Apply to all `urllib.request.Request` calls**

  Replace per-call header dicts with `{**_STANDARD_HEADERS, "Authorization": ..., "Content-Type": ...}`.

- [ ] **Step 3: Commit**

  ```bash
  git add auth/oauth_flow.py
  git commit -m "feat: full realistic header set on all OAuth HTTP requests"
  ```

---

## Task 9: Profile Cookie Pre-Seeding (Warm-Up Cookies)

**Files:**
- Create: `core/cookie_seeder.py`
- Modify: `auth/google_auth.py` — call seeder before login attempt

**Interfaces:**
- Produces: `seed_google_cookies(driver) -> None`
- Called in `GoogleAuthVerifier` after driver creation, before `perform_google_login`.

**What this covers:**

A browser that has never visited Google before is suspicious. Real users have Google cookies from casual browsing (search, YouTube, etc.). Fix: before navigating to the login page, visit a few benign Google URLs to accumulate NID, 1P_JAR, CONSENT cookies. This makes the profile look like it has prior Google activity.

- [ ] **Step 1: Create `core/cookie_seeder.py`**

  Implement `seed_google_cookies(driver)`:
  - Check if `chrome_profiles/<email>/Default/Network/Cookies` SQLite already has NID or CONSENT cookies — if yes, skip (profile is warm already)
  - If not: `driver.get("https://www.google.com")` → wait 1–2s → `driver.get("https://www.google.com/search?q=antigravity+ide")` → wait 1–2s
  - Log `[*] Warming up Google cookies for fresh profile`

  No new deps — sqlite3 already used in `profile_extractor.py`.

- [ ] **Step 2: Wire into `auth/google_auth.py`**

  After driver creation, before `perform_google_login`:
  ```python
  from core.cookie_seeder import seed_google_cookies
  seed_google_cookies(driver)
  ```

- [ ] **Step 3: Commit**

  ```bash
  git add core/cookie_seeder.py auth/google_auth.py
  git commit -m "feat: Google cookie pre-seeding for fresh Chrome profiles"
  ```

---

## Task 10: Account Processing Rate Limiting (Inter-Account Delay)

**Files:**
- Modify: `core/account_processor.py` — add cooldown between accounts
- Modify: `config/settings.py` — add `inter_account_delay_seconds` setting

**Interfaces:**
- New setting: `INTER_ACCOUNT_DELAY_SECONDS` (default `"5"`) read from `.env`
- Consumed in the account loop in `main.py` or `account_processor.py`

**What this covers:**

Processing 50 accounts back-to-back from the same machine, even with proxies, creates a traffic pattern Google can detect at the network level — too many login attempts in too short a window originating from the same ASN. Fix: add a randomized cooldown between account completions.

- [ ] **Step 1: Add `inter_account_delay_seconds` to `Settings` in `config/settings.py`**

  ```python
  inter_account_delay_seconds: float = float(os.getenv("INTER_ACCOUNT_DELAY_SECONDS", "5"))
  ```

- [ ] **Step 2: Apply delay in `core/account_processor.py` (or `main.py` account loop)**

  After each account completes (success or fail), before the next:
  ```python
  delay = settings.inter_account_delay_seconds
  jitter = random.uniform(-delay * 0.3, delay * 0.3)
  time.sleep(max(1.0, delay + jitter))
  ```

- [ ] **Step 3: Update `.env.example`**

  Add `INTER_ACCOUNT_DELAY_SECONDS=5` with a comment explaining it.

- [ ] **Step 4: Commit**

  ```bash
  git add config/settings.py core/account_processor.py .env.example
  git commit -m "feat: randomized inter-account cooldown delay"
  ```

---

## Summary Table

| Task | Vector Covered | Risk Without It |
|---|---|---|
| 1 | CDP fingerprint (navigator, plugins, chrome object) | Detected by fingerprint.js on Google pages |
| 2 | User-Agent (browser + JS) | `HeadlessChrome` UA = instant bot signal |
| 3 | Canvas + audio fingerprint | Stable fingerprint = linkable across sessions |
| 4 | Mouse movement path | Straight-line moves = bot pattern |
| 5 | Timing distribution | Flat integer sleeps = scripted cadence |
| 6 | Pre-form page interaction | Instant field focus = no human reading time |
| 7 | Timezone/locale consistency | Timezone ≠ proxy IP geo = fingerprint mismatch |
| 8 | HTTP request headers | Minimal headers = script, not IDE client |
| 9 | Cookie history (profile warmth) | Fresh profile with no prior Google cookies = suspicious |
| 10 | Login rate / traffic pattern | High-volume sequential logins = ASN-level block |
