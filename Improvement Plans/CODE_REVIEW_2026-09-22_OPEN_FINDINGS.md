# Code Review — Open Findings (2026-09-22)

Full-repository deep review of `app/` (3 parallel specialists: correctness/reliability,
security, performance/maintainability). This file tracks what's **still open** after
the first fix pass. See git history around this file's introducing commit for the
findings that were already fixed (SSRF fail-open, leaked exception detail to API
callers, the challenge-loop false-positive-clear bug, cookie-identity dedup, the
in-flight-dedup thundering herd, the fast-TLS session-jar cross-session leak, pool
instance health-checking, and the unbounded `_domain_scores` dict).

## Important (not yet fixed)

### CR-009 — `_execute_solve_flow` is a ~270-line monolith
**File:** `app/solver/browser/browser.py:337-606`

Combines cookie preload, media-blocking wiring, a response listener, navigation, the
full challenge-detection/interactive-click polling loop, Tier-3.5 captcha escalation,
selector/delay waits, screenshot capture, and final state extraction/status
resolution in one function, threading shared mutable state (`last_main_status`,
`cleared`, `last_detected_challenge`) through all of it.

**Why not fixed yet:** this is a pure maintainability refactor with no existing test
coverage that exercises the actual challenge-solve loop against a real (or realistically
mocked) browser/page. Splitting it blind risks introducing exactly the kind of subtle
bug this review exists to catch — the CR-001 loop bug (already fixed) went undetected
for this exact reason.

**Suggested approach when picked up:** write characterization tests first (a fake
`Page`/`BrowserContext` double driving the loop through the interesting cases —
clean load, WAF challenge → clears, WAF challenge → never clears/times out, browser
error page) so the refactor has something to prove it preserved behavior against.
Then split into `_preload_context()`, `_run_challenge_loop()` (returning a small
result object: cleared / last_challenge / last_status), `_maybe_captcha_escalate()`,
and `_finalize_solution()`, mirroring the decomposition already done for
`challenges.py`/`navigation.py`/`interactions.py`.

### CR-011 — No IP pinning between SSRF validation and the actual connection (DNS rebinding)
**File:** `app/security.py:65-88` (validation) vs. `app/solver/fast_tls.py` (curl_cffi)
and `app/solver/browser/navigation.py`/Camoufox launch (independent DNS resolution)

`check_target_url`/`check_target_url_async` resolve a hostname once and validate that
IP, but curl_cffi and Camoufox/Firefox each perform their **own independent DNS
resolution** when actually connecting — there's no pinning between the validated
address and the connection actually made. A domain with a very short TTL (or an
authoritative NS the attacker controls) can return a public IP to Solverr's
validation lookup and a private/internal IP microseconds later to the actual
client's resolver, reaching loopback/RFC1918/cloud-metadata targets despite the
check passing. This is a step beyond the already-documented "doesn't re-validate
redirects" limitation — it affects even the very first request.

**Why not fixed yet:** a real fix means pinning the resolved IP across three
independent network stacks with genuinely different APIs:
- curl_cffi's `AsyncSession.request()` doesn't expose a resolve-pin option in the
  installed version (0.16.3) — would need to go through libcurl's `CURLOPT_RESOLVE`
  via a lower-level API, unverified to exist in this binding.
- Camoufox/Firefox pinning means setting `network.dns.forceResolve`-style prefs on
  the launched profile — plausible in principle but nothing here could validate it
  end-to-end (no live network path to a real protected site in this dev environment,
  and Camoufox itself isn't invoked by the test suite).
- The Tier 4 fallback-proxy path adds a third variant to get right.

**Suggested approach when picked up:** prototype and validate the curl_cffi side
first (it's Tier 1 — the highest-traffic path) against a real test target before
touching the browser tier; treat the Camoufox-side fix as a separate, riskier
follow-up that needs to be validated against a real deployment, not just unit tests.

## Medium

- **CR-012** `app/solver/engine.py` `_cap_response_body()` — truncates by `str`
  character count against a **byte** budget (`MAX_RESPONSE_BODY_MB`); multi-byte
  UTF-8 content (CJK, emoji, accented text) can still exceed the configured cap by
  up to ~4x after truncation. Fix: truncate on `solution.response.encode("utf-8")[:max_bytes]`
  then `.decode("utf-8", errors="ignore")` instead of slicing the string.

- **CR-013** `app/api/dashboard.py` `/api/test` — `useCache=false` only clears the
  request's own (empty) cookie field; it doesn't stop `engine.py` from unconditionally
  merging in whatever's already cached for that domain via `cookie_cache.get_cookies_async`,
  so a "cold" test run isn't actually cache-free. Fix: thread a real cache-bypass flag
  through to `_do_process_request` that skips the cache lookup entirely.

- **CR-014** `app/solver/cache.py:337-357` `count_domains()` — the Redis path does a
  full `SCAN` over the entire `solverr:cookie:*` keyspace on every 5s cache-miss;
  this backs both `/api/stats` and every `/metrics` scrape, so a Prometheus scrape
  every 15-30s pays an O(N-cookies) cost on a large shared Redis cache. Fix: maintain
  a Redis `SET`/`SCARD` of known domains incrementally on `set_cookies`/eviction
  instead of re-deriving it from a keyspace scan, or lengthen the local cache TTL
  for this specific stat (domain count changes slowly).

- **CR-015** `app/solver/cache.py:214-218` `get_cookies()` (local-disk path) —
  collapses results to unique-by-**name**, silently dropping distinct-path cookies
  that the module's own `_cookie_key()` (domain+path+name) was built to preserve.
  Same root issue class as the now-fixed CR-004, different layer. Fix: key the
  dedup dict by `(c.domain, c.path, c.name)` to match the rest of the module, unless
  the name-only collapse is intentional (e.g. for Playwright's `add_cookies`) — in
  which case document that explicitly at the call site.

- **CR-016** `app/solver/browser/interactions.py:44-102` `dispatch_challenge_click` —
  sequentially awaits up to ~14 separate Playwright IPC round trips (one per
  Turnstile/reCAPTCHA/hCaptcha CSS selector) before falling back to the single
  `page.evaluate()` shadow-DOM walker in step 4.5, which already proves one call
  suffices. Lower priority since Tier 3 solves are already seconds-scale, but worth
  folding steps 2-4 into one `page.evaluate()` call for consistency with the existing
  walker.

- **CR-017** `app/solver/fast_tls.py:316`, `app/solver/browser/browser.py:198,222` —
  exception text is logged raw (not run through `sanitize_proxy_url`), while every
  other proxy-logging site in the codebase sanitizes first. libcurl/Playwright
  connection-failure messages frequently embed the full target/proxy URL verbatim.
  Fix: sanitize proxy substrings out of exception text before logging these lines.

- **CR-018** `app/main.py:166-178` `dashboard_index()` — synchronous `open().read()`
  on every `GET /` inside an `async def` handler, no `asyncio.to_thread`. Low
  severity (infrequent relative to `/v1`/`/scrape` traffic) but trivially fixable:
  read and cache the template contents once at import/startup instead.

## Minor

- **CR-019** `app/solver/sessions.py:132,153` — session proxy URLs (potentially with
  embedded credentials) persist to Redis in plaintext with no field-level protection.
  Reasonable given Solverr's role as a proxy pass-through, but undocumented as a
  deployment consideration — unlike the cookie cache file, which explicitly gets
  `chmod 0o660` treatment (`cache.py:489`). Suggest documenting it in the
  horizontal-scaling / Redis deployment notes rather than changing the behavior.

- **CR-020** `app/solver/browser/pool.py:71-101` `release()`/`_relaunch_with_retry()` —
  after 3 failed relaunch attempts, pool capacity is permanently reduced by one until
  a later `acquire()` happens to notice `_created < size`. Worth confirming this
  reliably self-heals under sustained failure (e.g. Redis-adjacent issues affecting
  Camoufox launch indirectly) rather than assuming it does.

## Already fixed (for reference — see commit history)

| ID | Summary |
|----|---------|
| CR-001 | Challenge-clearance loop could false-positive "cleared" on a content-skipped iteration, before an unsolved hCaptcha/DataDome/etc. widget was ever clicked |
| CR-002 | `check_target_url_async` failed open (allowed the request) on a DNS lookup failure instead of blocking it |
| CR-003 | Four API routes echoed raw exception text (potentially including proxy credentials) back to callers instead of a generic message |
| CR-004 | Cookie merge in `engine.py` deduped by name only, contradicting the domain+path+name identity model |
| CR-005 | Fast TLS tagged captured cookies with the original request URL's domain instead of the actual post-redirect responding domain |
| CR-006 | In-flight request dedup could thundering-herd retries after a shared failure, and could pop the wrong future |
| CR-007 | Pooled curl_cffi session was keyed without session id, letting concurrent callers under different sessions share one cookie jar |
| CR-008 | A pooled Camoufox instance that failed during setup was silently re-queued instead of force-recycled |
| CR-010 | `FastTLSEngine._domain_scores` grew unbounded for the life of the process |
