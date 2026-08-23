# Solverr — Unified Improvement Plan

Merges `SOLVERR_REVIEW.md` (code-level review with line citations) and
`Solverr_Improvement_Plan.md` (broader roadmap). Both were independently
verified against `main` on 2026-08-23 before implementation — every cited
bug reproduced in the current code at the time.

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
  to CI; bandit added as non-blocking; load testing and full pytest
  coverage tracking not done.
- **Phase 1** (architecture/maintainability): partially done — `tier`
  field, exception-detail redaction. The `browser.py` package split, typed
  exception hierarchy, and request-wide timeout budgeting were **not**
  attempted — see "Deliberately not done" below.

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

Not done (deliberately deferred — see below):
- Splitting `app/solver/browser.py` (979 lines) into a package.
- A full typed-exception hierarchy (`NavigationError`, `BrowserPoolError`,
  etc.) replacing broad `except Exception` throughout the solve flow.
- Request-wide timeout budgeting (`deadline = time.monotonic() + maxTimeout`
  shared across stages) — today each stage still has its own timeout.
- `time.time()` vs `time.monotonic()` audit for latency/timeout math.

**Why deferred:** `browser.py` is the most state-heavy module in the
codebase (pool lifecycle, challenge-detection loop, captcha escalation) —
splitting it and reworking exception handling both carry real regression
risk that's hard to fully retire without extensive hardware-in-the-loop
testing (which this pass did do for Docker/cgroup changes, but a full
control-flow refactor is a different order of risk). Recommend a dedicated
follow-up session scoped to just this, with the existing 100-test suite
plus new tests written *before* the refactor as a safety net.

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

Not done:
- More browser-pool tests beyond the cancellation-safety one added in
  Phase 2 (context-creation failure, shutdown-with-waiters, etc.).
- Load testing (k6/Locust/asyncio script) at various concurrency levels.
- `mypy`/`pyright` (not installed, and the codebase isn't currently
  type-strict enough to adopt without a dedicated pass) and formal
  coverage-percentage tracking.

## Phase 9 — Versioning & Dependencies (partially done)

Done:
- `VERSION` is now plain semver (`"1.5.0"`), `EDITION` split out
  (`"ultra"`), `DISPLAY_VERSION` (`"1.5.0-ultra"`) used for
  human-facing display (startup log, dashboard) — fixes the `vv1.5.0-ultra`
  bug at the source instead of patching the one call site.

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

1. **Root-cause the PUID/PGID Camoufox hang.** This is the single
   highest-value remaining item — it's a production correctness risk for
   exactly the NAS/self-hosted audience this project targets, and it
   wasn't understood, only worked around (timeout) for the one endpoint
   that lacked protection.
2. **`browser.py` package split** — write characterization tests for the
   current behavior first, then split, verifying against both the test
   suite and a real Docker smoke test (as validated in this pass) at each
   step.
3. Load testing script + more browser-pool failure-mode tests.
4. `CookieStore` interface extraction, if the dual-mode logic ever needs
   a third backend.
