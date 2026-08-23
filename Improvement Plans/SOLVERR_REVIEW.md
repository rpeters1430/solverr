# Solverr — Code Review & Improvement Plan

Reviewed: full `solverr-main.zip` upload (~3,140 lines across `app/`, plus tests, Dockerfile, docs).

**Overall impression:** this is a genuinely well-built project. The tiered fast-path/browser architecture is sound, the comments explain *why* (not just what) at every non-obvious decision, there's real test coverage (12 test files, ~1,070 lines), and the Docker/CI setup is production-grade. The bugs below are the kind that hide in exactly this kind of codebase: the "happy path" is heavily tested and exercised, but a couple of failure/edge paths were never hit by tests or by a real target site behaving badly. Nothing here suggests sloppiness — it's the normal residue of fast iteration.

---

## 🔴 Critical bugs (will crash or misbehave in production)

### 1. `UnboundLocalError` when a target site is completely unreachable
**File:** `app/solver/browser.py`, `_execute_solve_flow`, ~lines 592–610 and 949–954

```python
if method.upper() == "POST" and post_data:
    try:
        ...
        try:
            response = await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            initial_status = response.status if response else 0
        except Exception:
            initial_status = 0          # <-- response never gets assigned here
    except Exception as nav_err:
        ...
        initial_status = 0
else:
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        initial_status = response.status if response else 0
    except Exception as nav_err:
        ...
        initial_status = 0              # <-- response never gets assigned here either
```
...and later, unconditionally:
```python
if last_main_status["code"] is not None:
    status_code = last_main_status["code"]
elif response:                          # <-- NameError if navigation raised before this line ran
    status_code = response.status
```
`response` is only ever bound *inside* the `try` blocks, on the same line that can raise. If `page.goto()`/`page.wait_for_load_state()` throws (DNS failure, connection refused, timeout, TLS error — all common for a tool whose entire job is visiting flaky/hostile sites) and no HTTP response event ever fired (so `last_main_status["code"]` is still `None`), the code falls through to `elif response:` where `response` was never assigned → `UnboundLocalError`.

**Impact:** a request to a target that's simply down or unreachable doesn't return a clean error solution — it throws an unrelated Python `NameError`-family exception. It still gets caught by the outer handlers and returned as a 500-ish error, so it's not catastrophic, but it destroys the actual error message (network unreachable, DNS failure, etc.) and replaces it with a confusing "UnboundLocalError: local variable 'response' referenced before assignment" in the logs — exactly the moment a self-hoster most needs a clear error.

**Fix:** initialize `response = None` before both branches.

```python
initial_status = 0
response = None
if method.upper() == "POST" and post_data:
    try:
        ...
        try:
            response = await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            initial_status = response.status if response else 0
        except Exception:
            initial_status = 0
    except Exception as nav_err:
        logger.warning(f"[BrowserPool] POST form navigation notice: {nav_err}")
        initial_status = 0
else:
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        initial_status = response.status if response else 0
    except Exception as nav_err:
        logger.warning(f"[BrowserPool] Navigation notice: {nav_err}")
        initial_status = 0
```

**Test to add:** mock `page.goto` to raise (e.g. `PlaywrightTimeoutError`) with no response listener firing, assert `_execute_solve_flow` returns a `SolutionModel` with a fallback status instead of raising.

---

### 2. `NameError` on every SSE dashboard disconnect
**File:** `app/api/dashboard.py`, `sse_event_stream` (~line 105)

```python
@router.get("/events")
async def sse_event_stream():
    from fastapi.responses import StreamingResponse
    import json
    import time
    from app.events import event_broadcaster

    async def event_generator():
        q = event_broadcaster.subscribe()
        try:
            ...
            while True:
                data = await q.get()
                yield f"data: {data}\n\n"
        except asyncio.CancelledError:   # <-- `asyncio` is never imported in this file
            pass
        finally:
            event_broadcaster.unsubscribe(q)
```
`asyncio` is not imported anywhere in `app/api/dashboard.py` — neither at module scope nor inside this function (only `json`, `time`, and `StreamingResponse` are imported locally). Every time a browser tab closes or reloads the dashboard, Starlette cancels the generator, which raises `asyncio.CancelledError` — and the `except asyncio.CancelledError:` clause itself raises `NameError: name 'asyncio' is not defined` while trying to handle it. This is silent (logged, not user-visible) but it means the `finally: event_broadcaster.unsubscribe(q)` still runs, so it's not leaking queues — just spamming the log with unrelated tracebacks on essentially every dashboard visit.

**Fix:** one-line — add `import asyncio` to the local imports (or the top of the file):
```python
async def event_generator():
    import asyncio
    q = event_broadcaster.subscribe()
    ...
```

**Test to add:** open the SSE endpoint with `TestClient`, cancel the stream, assert no exception is logged / the queue is unsubscribed cleanly.

---

## 🟠 Security issues (self-hosted, but worth fixing)

### 3. Dashboard is vulnerable to stored/reflected XSS from the sites it solves
**File:** `app/static/app.js` — lines ~147, 150, 234, 313 (and others)

The live event feed and the cookie explorer build DOM via raw template-string `innerHTML`, using values that come straight from **the target website Solverr just visited**:

```js
entry.innerHTML = `... -> <code>${d.url}</code> ...`;                    // solve event
entry.innerHTML = `... -> <code>${d.url}</code>: ${d.error}`;            // solve_error event
tr.innerHTML = `<td><code>${c.name}</code></td><td><code>${c.value}</code></td>...`; // cookie explorer
card.innerHTML = `... ${cookie stuff} ...`;                              // /api/cookies view
```
`c.name` / `c.value` are cookie names and values **set by whatever hostile Cloudflare/WAF-protected site Solverr just solved a challenge for**, and `d.url` is the final navigated URL (which can reflect redirects a malicious page controls) or a raw exception message. None of it is HTML-escaped before being dropped into `innerHTML`. A target site (or a compromised ad on a target site) that sets a cookie like:
```
Set-Cookie: tok=<img src=x onerror=fetch('/api/cookies/clear')>
```
gets that payload rendered — and executed — in the Solverr dashboard's origin the next time an operator opens the Cookie Explorer or the live feed. Since the dashboard also exposes `/api/cookies/clear`, `/api/sessions/{id}` (DELETE), and can trigger new solves via `/api/test`, this isn't just a cosmetic issue — a script running in that origin can call those endpoints.

**Fix:** never build attacker-influenced strings with `innerHTML`. Either:
- switch those specific fields to `textContent` and build the surrounding structure with `createElement`, or
- add a small `escapeHtml()` helper and run every interpolated value (`d.url`, `d.error`, `d.challenge`, `c.name`, `c.value`, `c.domain`) through it before interpolating.

```js
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
```
This is the single highest-value fix in the review — it's the only one that's a real vulnerability rather than a reliability bug.

### 4. `API_KEY` check isn't constant-time and accepts the key as a URL query parameter
**File:** `app/main.py`, `request_logging_middleware`

```python
supplied = (
    request.headers.get("x-api-key")
    or request.query_params.get("api_key")
    or request.query_params.get("key")
)
...
if supplied != settings.API_KEY:
```
Two small issues:
- `!=` is a short-circuiting string comparison, not constant-time. Low real-world risk for a home-lab tool, but `hmac.compare_digest(supplied or "", settings.API_KEY)` costs nothing and removes the timing side-channel.
- Accepting the key via `?api_key=`/`?key=` query string means it ends up in access logs, reverse-proxy logs, browser history, and `Referer` headers of any outbound request the solved page makes. If you're fronting this with a reverse proxy (common for exposing it outside the LAN), consider dropping query-param auth entirely and requiring the header.

### 5. `/proxy` forwards the raw incoming request headers into the outbound Fast-TLS request
**File:** `app/api/flaresolverr.py`, `transparent_proxy`

```python
v1_req = V1Request(
    cmd=cmd,
    url=target_url,
    postData=post_data,
    headers=dict(request.headers)   # includes Host, Content-Length, Connection, Accept-Encoding, Cookie...
)
```
This passes through hop-by-hop and identity headers (`host`, `content-length`, `connection`, `accept-encoding`) from *the client calling Solverr* straight into the outbound `curl_cffi` request to the *target site* (`fast_tls.py` merges caller headers over its own defaults). A `Host:` header pointing at Solverr itself, or a stale `Content-Length` after the body's been re-read, can produce confusing failures or let a caller spoof the outbound `Host`. Strip hop-by-hop headers (`host`, `content-length`, `connection`, `transfer-encoding`, `accept-encoding`) before forwarding, the same way a real reverse proxy would.

---

## 🟡 Correctness / design issues (not crashes, but wrong behavior)

### 6. In-flight request de-duplication can serve the wrong response
**File:** `app/solver/engine.py`, `HybridSolverEngine.process_request`

```python
inflight_key = f"{method}:{url}:{req.forceBrowser}"
if inflight_key in self._inflight:
    return await asyncio.shield(self._inflight[inflight_key])
```
Two concurrent requests to the same URL+method+forceBrowser get coalesced into one solve — but the key ignores `postData`, `cookies`, `headers`, `proxy`, and `session`. If Prowlarr/Sonarr (or two different indexers behind the same domain) fire two concurrent `POST`s to the same URL with different bodies, or two requests with different session cookies, the second caller silently gets the first caller's response. Worth adding a hash of `postData`/`session`/proxy to the key, or at minimum documenting the limitation loudly since it's a subtle correctness trap.

### 7. `/scrape`'s `tier_used` field is a guess, not the actual tier
**File:** `app/api/flaresolverr.py`, `native_scrape_api`

```python
tier_used = "tier1_fast_tls"
if duration_ms > 1500 or v1_req.forceBrowser:
    tier_used = "tier3_stealth_browser"
elif req.cookies or v1_req.cookies:
    tier_used = "tier2_cache"
```
`solver_engine.process_request` already knows exactly which tier handled the request (it logs it and emits it on the SSE bus) — but that information never makes it back into `SolutionModel`, so this endpoint has to *guess* from duration and whether cookies were supplied. A slow network hiccup on a Fast-TLS request over 1.5s gets mislabeled `tier3_stealth_browser`; a browser solve that happens to finish in under 1.5s (cached Turnstile clearance, warm pool) gets mislabeled `tier1_fast_tls`. Since this field is explicitly part of the documented native API and feeds the Prometheus tier metrics narrative, it should be accurate.

**Fix:** add a `tier: Optional[str]` field to `SolutionModel`, set it at each of the four return points in `engine.py` (it already has the string literals — `"tier1_fast_tls"`, `"tier2_cache"`, `"tier3_stealth_browser"`, `"tier4_fallback_proxy"` — right there in the `event_broadcaster.emit(...)` calls), and just read `solution.tier` in `native_scrape_api` instead of re-deriving it.

### 8. Cookie-cache debounced disk write can drop the very last update in a burst
**File:** `app/solver/cache.py`, `_schedule_save` / `_debounced_flush`

The debounce flag (`_save_pending = False`) is cleared *before* the (blocking, executor-run) disk write starts, not after it finishes. A `set_cookies()` call that lands in the window between "flag cleared" and "write finishes" won't schedule a new flush (since `_save_task.done()` is still `False`), and won't be picked up until *another* `set_cookies()` call happens later to schedule the next flush. In the common case this is harmless (there's almost always a next request), but on shutdown or during a quiet period, the last cookie update from a burst can end up only in memory and get lost if the process dies before another write is triggered. Low severity, but a one-line fix: move `self._save_pending = False` to *after* `await loop.run_in_executor(...)`, and re-schedule if a request came in during the write.

---

## 🟢 Smaller items worth doing

- **`app/config.py`** — `MAX_BROWSER_WORKERS` auto-tuning and RAM math run at class-body evaluation time (they're class attributes computed once at import). That's intentional and documented, but it means changing `RAM_PER_WORKER_GB`/`RAM_RESERVED_GB` env vars requires a restart to take effect — worth a one-line README/CLAUDE.md note if it isn't already there (a quick search says it isn't explicit).
- **`app/solver/browser.py`, `_try_captcha_solver_escalation`** — logs `sitekey[:16]` which is fine, but the paid-solver escalation path has no test coverage for the "solver returns a token but the page never settles" case (the 8-second settle-loop just times out silently and falls through to returning whatever HTML is there). Consider surfacing that as a `challengeType` suffix (e.g. `"hcaptcha_unsettled"`) so callers can distinguish "solved and cleared" from "token injected, page still stuck."
- **`app/api/dashboard.py`, `/api/test`** — `TestRequestModel` has no `maxTimeout`/`headers`/`proxy` fields, so the dashboard's manual test bench can't reproduce a session/proxy-specific issue an operator is debugging. Minor UX gap, not a bug.
- **Tests** — the two crash bugs above (#1, #2) both fall into exactly the gap in your existing suite: `tests/test_engine.py` mocks `browser_pool.solve` entirely (never exercises `_execute_solve_flow`'s navigation-failure path), and `tests/test_events.py` tests `EventBroadcaster` directly but never goes through the actual `/api/events` route. Both are easy to close and would have caught these before merge.

---

## Suggested priority order

1. **Fix #1 and #2** (`response = None` init; `import asyncio`) — five minutes total, removes two real crash paths.
2. **Fix #3** (escape dashboard-rendered values) — this is the one actual security vulnerability; do it before exposing the dashboard on anything but `localhost`.
3. **Fix #7** (real `tier_used` instead of guessing) — cheap, and your Prometheus/dashboard tier reporting is a headline feature, so it should be trustworthy.
4. **Fix #5** (strip hop-by-hop headers in `/proxy`) — cheap and prevents a real class of confusing bugs if you ever route Prowlarr through `/proxy` instead of `/v1`.
5. **#4, #6, #8** — good hardening, lower urgency for a single-user home-lab deployment.

Happy to implement any of these directly — the two critical fixes and the XSS escaping are small, self-contained diffs I can make right now if you want them applied to the source.
