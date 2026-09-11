# Solverr — Unified Improvement Plan

Merges `SOLVERR_REVIEW.md` (code-level review with line citations) and
`Solverr_Improvement_Plan.md` (broader roadmap). Both were independently
verified against `main` on 2026-08-23 before implementation — every cited
bug reproduced in the current code at the time.

**Re-verified against `main` on 2026-09-10** (now at `VERSION = "1.7.0"`,
`app/config.py:185`): status summary and phase notes below updated to match
what has landed since the 2026-08-23/2026-09-03 passes — the CodSpeed
benchmark suite, the unused-Jinja dependency removal, and confirmation of
which "not done" items are still genuinely open. See the "Competitive scan
— TRAWL" section at the bottom for what TRAWL's 2026-09-04 `v1.5.0` release
adds relative to Solverr.

**Same-day follow-up (2026-09-10):** AWS WAF detection, Redis reconnection
resilience, and MCP server support (the TRAWL-scan items 1-3 below) were
implemented right after the scan above — see each item's "done" note for
what shipped and how it was verified.

## Status summary

- **Phase 0** (correctness/security bugs): done.
- **Phase 3** (observability), **Phase 4** (security/API hardening),
  **Phase 5** (cache/persistence), **Phase 6** (health/diagnostics),
  **Phase 9** (version string): done.
- **Phase 2** (browser pool hardening): effectively done — health/queue
  metrics and cancellation-safety were verified, no separate work needed.
- **Phase 7** (Docker/NAS): cgroup-aware worker sizing done; container
  hardening (`cap_drop: ALL`) investigated and deliberately **not**
  defaulted on — see the finding below.
- **Phase 8** (CI/CD): ruff + a real Docker build/health smoke test added
  to CI; bandit added as non-blocking; a CodSpeed micro-benchmark suite
  (`benchmarks/`, `.github/workflows/codspeed.yml`) covering the CPU-bound
  hot paths landed 2026-09-08 for continuous regression detection — this
  is complementary to, not a substitute for, the still-not-done
  concurrency-level load testing and full pytest coverage tracking below.
- **Phase 1** (architecture/maintainability): partially done — `tier`
  field, exception-detail redaction, and (2026-09-03) the `browser.py`
  package split are done. A typed exception hierarchy was **not**
  attempted — see "Deliberately not done" below. Request-wide timeout
  budgeting shipped separately in v1.7.0 (`RequestBudget` in
  `app/solver/engine.py`).

## ⚠️ Finding from this pass: PUID/PGID + Camoufox can hang indefinitely

Verified directly against a built image, 2026-08-23. Running the container
with `-e PUID=1000 -e PGID=1000` (no other flags) and triggering a Tier 3
browser solve can leave the Playwright↔Firefox IPC handshake hanging
forever instead of erroring — reproduced with `GET /api/diagnostics/browser`
(a new endpoint added in this pass) sitting unresponsive for 2+ minutes
before it was manually killed. Root cause wasn't identified (not a
capability issue — reproduces with zero hardening flags; not further
narrowed down given remaining session budget).

Impact today: `BrowserPool.solve()`'s two attempts are already wrapped in
`asyncio.wait_for(timeout=tier_timeout)`, so a production solve request
hitting this still times out and gets logged/escalates to Tier 4 rather
than hanging the whole server. `BrowserPool.self_test()` had no such
wrapper and has been fixed to add one. This is still worth a tracked
GitHub issue and real investigation before recommending PUID/PGID
alongside heavy Tier 3 usage in production — see CLAUDE.md's Docker
section for the full repro notes.

## Phase 0 — Correctness & Security Fixes (implemented 2026-08-23, earlier pass)

1. `UnboundLocalError` on unreachable target sites (`browser.py`).
2. Missing `import asyncio` causing a `NameError` on every SSE disconnect
   (`dashboard.py`).
3. Dashboard XSS — unescaped `innerHTML` from attacker-controlled cookie/
   URL/error values (`app.js`).
4. API key: dropped query-param acceptance, switched to
   `hmac.compare_digest`.
5. `/proxy` hop-by-hop header leakage into the outbound request.
6. In-flight dedup key now a fingerprint of every outcome-affecting field,
   not just `method:url:forceBrowser`.
7. `SolutionModel.tier` — real tier tracking instead of `/scrape` guessing
   from duration/cookie presence.
8. Cookie cache debounced-write ordering (pending flag cleared after the
   write, not before).
9. `vv1.5.0-ultra` double-v version string.
10. `/proxy`'s manual `raw_query` parsing — investigated, kept as-is
    (intentional: supports an unescaped nested query string in `url=`;
    switching to `request.query_params` broke a passing regression test).
11. Cookie identity now `domain+path+name` (cache + session cookies), not
    name alone.

## Phase 1 — Architecture & Maintainability (partially done)

Done:
- `SolutionModel.tier` (see Phase 0 #7) covers the practical need behind
  the plan's `EngineResult`/`SolveMetadata` proposal without adding a
  parallel wrapper type.
- Exception-detail redaction: `main.py`'s catch-all handler no longer
  returns raw `str(exc)` to callers (`request_id`-correlated log entry
  instead) — was leaking internal paths/details.
- `app/security.py`'s `SSRFBlockedError` is the first typed solver error.

Done (2026-09-03):
- Split `app/solver/browser.py` (1091 lines by then) into
  `app/solver/browser/` — `pool.py` (Camoufox process lifecycle),
  `models.py` (`_PooledCamoufox`), `challenges.py` (pure WAF/age-gate
  detection), `captcha.py` (paid-solver escalation), `cookies.py`
  (Playwright↔`CookieModel` conversion), `navigation.py` (GET/POST
  navigation + media blocking), `interactions.py` (challenge-widget click
  dispatch), and `browser.py` (`BrowserPool`, now delegating to the above
  instead of inlining ~500 lines in `_execute_solve_flow`). Done as a
  behavior-preserving move (verbatim code relocated into functions, no
  control-flow rewrite) specifically to keep the regression risk called
  out below low: `__init__.py` re-exports the exact prior public API so
  every existing import site (`app/main.py`, `app/metrics.py`,
  `app/api/dashboard.py`, `app/solver/engine.py`, `app/solver/fast_tls.py`,
  and all `tests/test_*.py` files that imported from `app.solver.browser`)
  needed zero changes. Added `tests/test_browser_cookie_and_nav_helpers.py`
  covering the newly-extracted pure `cookies.py`/`navigation.py` helpers.
  Full existing suite (111 tests) still passes unchanged (120 with the new
  file) — verified before and after.

Not done (deliberately deferred — see below):
- A full typed-exception hierarchy (`NavigationError`, `BrowserPoolError`,
  etc.) replacing broad `except Exception` throughout the solve flow.
- `time.time()` vs `time.monotonic()` audit for latency/timeout math.

Done separately in v1.7.0 (not part of this plan's pass, but closing this
item): request-wide timeout budgeting — `RequestBudget` in
`app/solver/engine.py` (`deadline = time.monotonic() + maxTimeout` shared
across stages).

**Why the exception hierarchy is still deferred:** reworking
`except Exception` handling throughout the solve flow (as opposed to
relocating code, which the browser.py split above did) is a genuine
control-flow change with real regression risk that needs the same kind of
hardware-in-the-loop verification the Docker/cgroup changes got. Recommend
scoping it as its own follow-up rather than folding it into further
`browser/` package work.

## Phase 2 — Browser Pool Hardening (done)

- `BrowserPool.pool_stats()`: pool size/created/busy/idle, recycle count,
  crash count, average queue-wait time.
- Verified (via `tests/test_camoufox_pool.py`) that a crash/cancellation
  mid-solve still closes the page/context and checks the instance back
  into the pool — the existing `finally` blocks in
  `_solve_with_pooled_camoufox`/`_solve_with_ephemeral_camoufox` already
  handled this correctly; added a regression test rather than new code.
- `solverr_browser_queue_wait_seconds` exposed in `/metrics`.

## Phase 3 — Observability (done)

- Per-tier Prometheus histograms (`solverr_request_duration_seconds_bucket`,
  `_sum`, `_count`) via a minimal `Histogram` class in `engine.py`.
- New counters: `solverr_cookie_cache_lookups_total{outcome=hit|miss}`,
  `solverr_timeouts_total`, `solverr_requests_inflight`,
  `solverr_browser_pool_*`, `solverr_browser_crashes_total`.
- Dashboard: new "Browser Pool & Cache Health" panel (pool utilization,
  cache hit ratio, queue wait, crashes/timeouts).

## Phase 4 — Security & API Hardening (done)

- `app/security.py::check_target_url()` — SSRF protection blocking
  loopback/RFC1918/link-local/cloud-metadata targets by default
  (`ALLOW_PRIVATE_NETWORKS=false`), with `ALLOWED_HOSTS`/`DENIED_HOSTS`
  overrides. Wired into `HybridSolverEngine.process_request`, so it covers
  `/v1`, `/v2`, `/scrape`, and `/proxy` uniformly. **Only the initial
  target is checked** — redirects are not re-validated (would need hooking
  `curl_cffi`, Camoufox navigation, and the Tier 4 proxy path separately).
- `METRICS_REQUIRE_AUTH` — `/metrics` stays open by default, can be gated
  behind the same `X-Api-Key`.
- `MAX_REQUEST_BODY_MB` (413 on oversized `Content-Length`),
  `MAX_RESPONSE_BODY_MB` (truncates an oversized solved response),
  `MAX_SCREENSHOT_MB` (drops an oversized screenshot).

## Phase 5 — Cache & Persistence Cleanup (done)

- Redis `KEYS` replaced with `SCAN` (`scan_iter`) in both `cache.py` and
  `sessions.py` — `KEYS` blocks the single-threaded Redis server for the
  whole keyspace walk.
- `MAX_CACHE_DOMAINS`/`MAX_COOKIES_PER_DOMAIN`/`MAX_SESSIONS` — LRU-ish
  eviction for the local (non-Redis) backends; Redis relies on its
  existing TTL expiry instead.
- Not done: a formal `CookieStore` interface
  (`MemoryCookieStore`/`JsonCookieStore`/`RedisCookieStore`) — the
  dual-mode logic already inside `CookieCache`/`SessionManager` works and
  splitting it is a pure maintainability nice-to-have, not a bug fix.

## Phase 6 — Health & Diagnostics (done)

- `GET /health/live` (liveness only) and `GET /health/ready` (same check
  as `/health`) added alongside the existing `/health`.
- `GET /api/diagnostics/browser` — launches a real ephemeral Camoufox
  end-to-end (context, page, JS execution), unlike `/health`'s
  import-only check. Timeout-bounded after the PUID/PGID hang finding
  above.

## Phase 7 — Docker/NAS (partially done)

Done:
- `app/config.py` now reads cgroup v2 (`/sys/fs/cgroup/cpu.max`,
  `memory.max`) or v1 limits for `TOTAL_CPU_CORES`/`TOTAL_RAM_GB` when
  present, falling back to host-level `os.cpu_count()`/`psutil`. Verified
  against the real image under `docker run --cpus=2 --memory=2g`: worker
  auto-tuning now correctly sizes down to 1 instead of the old forced
  minimum of 4 (which would have oversubscribed a 2GB-limited container).
- Minimum auto-tuned worker count lowered from a forced 4 to 1 (clamped by
  the RAM-based cap either way).
- `security_opt: [no-new-privileges:true]` added to `docker-compose.yml`
  by default — verified working for both the root and PUID/PGID paths.

Investigated but **not defaulted on**:
- `cap_drop: [ALL]` — verified working when the container runs as root
  (the default), but breaks Camoufox under the PUID/PGID path even with
  `CHOWN`/`SETUID`/`SETGID`/`SYS_CHROOT`/`SYS_ADMIN` added back (hangs,
  same failure mode as the PUID finding above, tested directly against
  the built image). Left as a commented-out opt-in in
  `docker-compose.yml` with this caveat, rather than silently breaking
  the documented PUID/PGID NAS deployment path.
- Read-only root filesystem: not attempted — given the `cap_drop`
  finding, likely has similar non-root interaction risk and needs the
  same kind of direct verification before shipping as a default.

## Phase 8 — CI/CD & Testing (partially done)

Done:
- `pyproject.toml` added with a minimal `[tool.ruff]` config
  (`select = ["E9", "F"]` — syntax errors + pyflakes; line-length checks
  excluded since the codebase doesn't follow a strict line-length
  convention and enforcing one would need an unrelated reformat).
  Existing lint debt (7 unused imports, 1 unused variable, 1 f-string
  without placeholders) was fixed as part of adding this.
- CI (`docker-publish.yml`): `ruff check app/` added as a blocking step;
  `bandit -r app -ll` added as non-blocking (findings not triaged in this
  pass, so it shouldn't fail builds yet).
- **Real Docker build + smoke test** added to the publish job: builds the
  image locally (`load: true`), starts it, polls `/health` for up to 60s,
  fails the job with container logs on timeout, tears down, then proceeds
  to the real multi-tag push. Validated locally against this exact
  workflow logic (built image, ran the same curl-polling loop, confirmed
  pass/fail behavior) before committing it to CI.
- **CodSpeed benchmark suite** (2026-09-08): `pytest-codspeed` benchmarks
  in `benchmarks/` (kept out of `tests/` so `python -m unittest discover -s
  tests` is unaffected) covering challenge detection, the cookie cache,
  sessions, request/response models, `/scrape` extraction, Tier 1 profile
  selection, SSRF checks, metrics rendering, and cursor geometry — run in
  CI simulation mode on every push/PR via
  `.github/workflows/codspeed.yml`. Catches CPU-hot-path regressions
  automatically; still doesn't answer the concurrency/throughput questions
  the "Not done" load-testing item below covers.

Not done:
- More browser-pool tests beyond the cancellation-safety one added in
  Phase 2 (context-creation failure, shutdown-with-waiters, etc.).
- Load testing (k6/Locust/asyncio script) at various concurrency levels.
- `mypy`/`pyright` (not installed, and the codebase isn't currently
  type-strict enough to adopt without a dedicated pass) and formal
  coverage-percentage tracking.

## Phase 9 — Versioning & Dependencies (partially done)

Done:
- `VERSION` is now plain semver (currently `"1.7.0"` as of this pass,
  bumped from `"1.5.0"` when Phase 1's request-wide timeout budgeting
  shipped), `EDITION` split out (`"ultra"`), `DISPLAY_VERSION`
  (`"1.7.0-ultra"`) used for human-facing display (startup log,
  dashboard) — fixes the `vv1.5.0-ultra` bug at the source instead of
  patching the one call site.
- Unused `Jinja2` dependency removed from `requirements.txt`
  (2026-09-08) — the app doesn't use server-side Jinja templating
  (`app/templates/index.html` is served as a static file), so this was
  dead weight in the runtime image.

Not done (deliberately deferred):
- Full `pyproject.toml`/`uv.lock` migration with
  runtime/dev/test dependency groups — `pyproject.toml` now exists but
  only for `[tool.ruff]` config, not packaging. Migrating
  `requirements.txt` wholesale touches the Docker build (`pip install -r
  requirements.txt`), which works today; doing this without also
  re-validating the full image build was judged lower value than the
  security/correctness work above for this pass.
- Pinning Playwright/Camoufox/curl_cffi to exact versions beyond what
  `requirements.txt` already does.

## Recommended next session

1. **Root-cause the PUID/PGID Camoufox hang.** Still the single
   highest-value remaining item — it's a production correctness risk for
   exactly the NAS/self-hosted audience this project targets, and it
   wasn't understood, only worked around (timeout) for the one endpoint
   that lacked protection. Decision made 2026-09-03: keep PUID/PGID
   support as-is (it's fine for Fast-TLS-only/proxy-only deployments that
   never touch Tier 3) rather than removing it, with CLAUDE.md's existing
   root-required warning staying the guidance for Tier 3 browser solving.
   Needs a real non-root container + strace/debug session to progress —
   not reproducible in a sandbox without a Docker daemon.
2. ~~`browser.py` package split~~ — done 2026-09-03, see Phase 1 above.
3. Load testing script + more browser-pool failure-mode tests.
4. `CookieStore` interface extraction, if the dual-mode logic ever needs
   a third backend.
5. Typed exception hierarchy for the solve flow (see Phase 1's "not done"
   list) — now a more natural next step since `browser/` is already split
   into focused modules, each of which is a smaller surface to retrofit
   typed errors into than the old single file.
6. ~~Redis reconnection resilience for `CookieCache`/`SessionManager`~~ —
   done 2026-09-10, see "Competitive scan — TRAWL" below, item 3.
7. ~~AWS WAF challenge detection~~ — done 2026-09-10, see item 1 below.

## Competitive scan — TRAWL `v1.5.0` (released 2026-09-04)

TRAWL (`germondai/trawl`, the other project CLAUDE.md and the README name
as a comparison point) shipped `v1.5.0` on 2026-09-04, six days before this
pass. Its changelog and README (`dev` branch) were reviewed against
Solverr's current `app/` to see what's genuinely missing here versus
already covered. Solverr already has DataDome and Akamai detection
(`app/solver/browser/challenges.py`'s `WAF_SIGNATURES`, counted in
`engine.py`), so those aren't gaps — the items below are.

1. **AWS WAF challenge detection — done 2026-09-10.** Added an `"aws_waf"`
   entry to `CHALLENGE_MARKERS` in `app/solver/browser/challenges.py`,
   keyed on the `window.gokuProps` JS variable AWS WAF's challenge page
   embeds inline ("goku" is its internal codename) plus an `awswaf`
   substring, mirroring the existing DataDome/Akamai entries. Counted in
   `PerformanceMetrics.challenges_solved["aws_waf"]` (`engine.py`).
   **Correction (2026-09-11, PR review):** an initial `"aws-waf-token"`
   cookie-name marker was removed — `detect_challenge()` only ever sees
   `page.content()`/title (see the call site in `browser.py`), never
   cookies, so a cookie-name string could never actually match anything in
   production; the review comment that caught this is the reason
   `challenges.py`'s "no page dependency" contract is called out explicitly
   in this file's Module Layout section. Covered by
   `tests/test_challenge_detection.py`, including a regression test that
   the cookie name alone does *not* match.

2. **MCP (Model Context Protocol) server — done 2026-09-10, hardened
   2026-09-11.** Added `app/mcp_server.py` using the `mcp` package's
   `MCPServer` (the `mcp` 2.x successor to 1.x's `FastMCP` — verified
   directly against the installed `mcp==2.2.0` API rather than assumed,
   since this is a fast-moving SDK), mounted at `/mcp` in `app/main.py` and
   gated by `ENABLE_MCP` (default on). Four tools: `solverr_scrape`,
   `solverr_screenshot`, `solverr_get_cookies`, `solverr_get_stats` — all
   calling directly into the same
   `solver_engine`/`cookie_cache`/`browser_pool` singletons the HTTP routes
   use. Two integration details that would otherwise silently break this in
   production, both verified end-to-end with a real `TestClient` against
   the actual pinned dependency versions before shipping: (a) mounting a
   Starlette sub-app does **not** invoke its own lifespan, so
   `mcp_server.session_manager.run()` must be entered explicitly from
   `app/main.py`'s lifespan via `contextlib.AsyncExitStack` — without this,
   every request 500s with "Task group is not initialized"; (b)
   `streamable_http_app()` auto-enables DNS-rebinding Host-header
   validation when `host="127.0.0.1"` (its default), which would reject
   nearly every real request to Solverr's network-exposed deployment
   (Docker network alias, NAS IP, reverse-proxy hostname) with a 421.
   **Correction (2026-09-11, PR review):** the initial fix disabled that
   protection unconditionally, which a reviewer correctly flagged as
   weakening the common no-`API_KEY` default deployment — DNS rebinding
   from a malicious webpage would then reach `solverr_get_cookies` et al.
   with no protection at all. Replaced with `_mcp_transport_security()`:
   disabled only when `API_KEY` is set (a shared secret is the real gate
   then); otherwise left on and restricted to localhost by default, with
   `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` (`app/config.py`) to widen it
   for a real non-localhost, no-key deployment. Also fixed: the
   `solverr_screenshot` tool was labeling its JPEG bytes (`page.screenshot
   (type="jpeg")`, `browser.py`) as PNG, which some MCP clients would
   reject on a mimetype/signature mismatch; and `solverr_scrape`'s
   docstring advertised a `"tier4_proxy"` option that doesn't actually
   force Tier 4 (`ScrapeRequest.to_v1_request()` maps it to the same
   `forceBrowser=True` as `"tier3_browser"` — Tier 4 is an automatic
   engine-side escalation, not a caller-selectable mode). Covered by
   `tests/test_mcp_server.py` (tool registration, a cache-backed tool call,
   the API-key gate applying to `/mcp` like every other route, the
   localhost-only Host rejection, and `_mcp_transport_security()`'s
   branching in isolation).

3. **Redis reconnection resilience — done 2026-09-10, hardened
   2026-09-11.** `CookieCache` (`app/solver/cache.py`) and `SessionManager`
   (`app/solver/sessions.py`) each replaced their one-shot `_init_redis()`
   with a `_redis()` helper that retries the connection on a 30s cooldown
   (`REDIS_RECONNECT_INTERVAL_SECONDS`) instead of setting `redis_client`
   to `None` permanently on the first failure — a transient Redis restart
   during container startup (`docker compose --profile distributed up`,
   where `solverr` can start before `redis` is ready) now self-heals
   instead of stranding that replica on local-only cache/sessions until a
   manual restart. **Correction (2026-09-11, PR review):** the initial
   `_redis()` only retried a connection that failed to *establish* in the
   first place — once a client had connected successfully, an outage
   afterward (Redis restarted mid-run) left `redis_client` set to the now-
   dead client forever, so every later cache/session operation kept
   retrying that stale connection (paying its `socket_connect_timeout` each
   time) instead of backing off, and never triggered a fresh reconnect
   attempt even once Redis came back. Fixed with an `_invalidate_redis()`
   helper, called from every Redis operation's exception handler in both
   files, that clears `redis_client` and resets the cooldown timer so the
   next call goes through `_redis()`'s normal retry path. Covered by a
   reconnection test (initial-failure) and a new post-connect-outage test
   in each of `tests/test_cache.py` and `tests/test_sessions.py`.
   **Second correction (2026-09-11, PR review round 2):** `CookieCache`'s
   reads go to Redis exclusively once `_redis()` returns a live client
   (`get_cookies`/`get_all_entries`), so cookies written to the local
   fallback store during an outage simply stopped being served the moment
   Redis reconnected — not lost from disk, but invisible to callers. Fixed
   with `_migrate_local_store_to_redis()`, called right after a successful
   `_redis()` connect, which flushes `_store` into Redis and clears it.
   `SessionManager` didn't have this problem (`get_session()` always checks
   its in-memory `_sessions` dict before Redis, regardless of connection
   state). Covered by a migration test in `tests/test_cache.py`.

   Also fixed in this round: `app/solver/engine.py`'s in-flight
   deduplication fingerprint (`HybridSolverEngine.process_request`) was
   missing `screenshot` and `maxTimeout` — a pre-existing gap (present
   since the fingerprint was introduced) that the MCP tools made easy to
   trigger, since they expose both as independent per-call parameters. Two
   concurrent requests for the same URL differing only in one of those
   fields could incorrectly share one answer (a plain scrape getting back
   screenshot data meant for a concurrent `solverr_screenshot` call, or
   vice versa; a short per-call timeout silently inheriting a concurrent
   longer one). Both fields added to the fingerprint dict; covered by two
   new tests in `tests/test_engine.py`. Also: `_mcp_transport_security()`'s
   `MCP_ALLOWED_HOSTS`/`MCP_ALLOWED_ORIGINS` were replacing the localhost
   defaults instead of extending them (an operator adding a real deployment
   hostname would have lost local access) — fixed to concatenate instead;
   and `solverr_scrape` silently sent unsupported HTTP methods (PUT,
   DELETE) as GET with no `post_data` parameter to actually support POST —
   fixed to validate `method` is GET/POST and accept `post_data`.

4. **Response/debug capture (console logs, network requests, redirect
   chain) — matches this plan's own Phase 3/6, not a new item.** TRAWL
   1.5.0 added optional response-body/console/network/redirect-chain
   capture for its `inspect` MCP tool and API. Solverr's `/scrape` has no
   equivalent today (confirmed: no `console`/`redirectChain`/network-log
   fields in `app/models/flaresolverr.py` or the browser navigation code).
   This overlaps with debugging/diagnostics rather than being urgent on
   its own — worth folding into a future `/scrape` enhancement (e.g. an
   optional `capture: ["console", "network", "redirects"]` request field)
   rather than treating as a standalone priority.

5. **Not applicable to Solverr's architecture:** TRAWL's MITM forward-proxy
   mode (self-signed root CA, RFC 5280 cert-compat fixes in 1.5.0) has no
   equivalent in Solverr's design — Solverr's `/proxy` is a transparent
   HTTP proxy without a MITM CA, and introducing one would be a much larger
   architectural change than anything else in this scan. Not recommended
   unless a specific user need for MITM HTTPS interception surfaces.
   Likewise TRAWL's bundled FFmpeg (for video-heavy scrape targets) and
   Bun/Tini process-management changes are runtime-specific to its Node/Bun
   stack and don't map onto Solverr's Python/`tini`-already-PID-1 setup.

**Status:** items 1-3 implemented 2026-09-10 (see above). Item 4 (response/
debug capture) is still open and worth scoping as part of a future
`/scrape` enhancement — an `inspect`-style MCP tool would be a natural
consumer of it once it exists, alongside the HTTP API. Item 5 (MITM proxy,
FFmpeg, Bun/Tini) remains not recommended, per the reasoning above.
