# Solverr Improvement Plan

## Overview

Solverr has a solid foundation: a hybrid Fast TLS → cached/session state → Camoufox browser architecture, browser pooling, metrics, events, a dashboard, Docker support, and an existing test suite.

The next development cycle should prioritize **correctness, stability, observability, security, and maintainability** before adding more browser tricks or challenge-specific features.

---

## Current Assessment

| Area | Current State | Priority |
|---|---|---|
| Architecture | Good | Low |
| Docker / NAS deployment | Good | Low |
| Fast TLS → browser escalation | Good concept | Low |
| Browser lifecycle | Good, needs hardening | Medium |
| Cookie/session correctness | Needs work | Critical |
| Concurrent request handling | Needs work | Critical |
| API security | Needs work | Critical |
| Observability | Good start | Medium |
| Error handling | Too broad in places | Medium |
| Tests | Good breadth | Medium |
| CI/CD | Functional, needs tightening | Medium |
| Code maintainability | `browser.py` too large | High |
| Dependency management | Needs cleanup | Medium |

---

# Priority 0 — Immediate Correctness Fixes

These should be addressed before new feature work.

## 1. Fix Request Coalescing Isolation

Current in-flight request deduplication is too coarse.

Example current key:

```python
inflight_key = f"{method}:{url}:{req.forceBrowser}"
```

This can incorrectly treat requests with different cookies, headers, bodies, sessions, proxies, or options as identical.

### Improve by fingerprinting:

- HTTP method
- URL
- POST/body data
- headers
- cookies
- proxy
- session
- user agent
- `forceBrowser`
- `fastTlsOnly`
- timeout-related options
- selectors / browser options

Use canonical JSON plus SHA-256.

### Recommendation

Only coalesce clearly safe requests by default:

- GET only
- no POST body
- no Authorization header
- no custom session
- no custom cookies
- same proxy and solve options

Do not coalesce POST requests by default.

---

## 2. Fix Cookie Identity and Scoping

Cookies should not be keyed only by domain + name.

Correct identity should include:

```text
domain + path + name
```

Introduce a cookie key model such as:

```python
@dataclass(frozen=True)
class CookieKey:
    domain: str
    path: str
    name: str
```

Preserve:

- domain
- path
- host-only semantics
- expiration
- Secure
- SameSite where relevant
- replacement rules

---

## 3. Fix Session Cookie Replacement

Session cookie updates should also use:

```text
domain + path + name
```

instead of only cookie name.

This prevents legitimate same-name cookies on different paths from overwriting one another.

---

## 4. Fix Dashboard SSE Import Bug

`app/api/dashboard.py` references:

```python
asyncio.CancelledError
```

without importing `asyncio`.

Add:

```python
import asyncio
```

---

## 5. Fix Version String

Current logging produces:

```text
vv1.5.0-ultra
```

because the stored version already contains `v` and logging adds another.

Move toward:

```text
1.5.0
```

and keep branding/edition metadata separate.

---

## 6. Fix Transparent Proxy URL Parsing

Avoid manually parsing:

```python
raw_query = str(request.url.query)
```

Use FastAPI / Starlette query parsing:

```python
target_url = request.query_params.get("url")
```

---

## 7. Use `time.monotonic()` for Durations

Use:

```python
time.monotonic()
```

for latency and timeout measurements.

Reserve:

```python
time.time()
```

for wall-clock timestamps.

---

## 8. Stop Returning Raw Internal Exceptions

Do not return:

```python
"detail": str(exc)
```

to API clients.

Instead return a safe error with request ID:

```json
{
  "status": "error",
  "error": "Internal solver error",
  "request_id": "ab12cd34"
}
```

Keep detailed traceback information in logs.

---

# Priority 1 — Architecture and Maintainability

## 9. Split `browser.py`

`app/solver/browser.py` is too large and owns too many responsibilities.

Suggested structure:

```text
app/solver/browser/
    __init__.py
    pool.py
    browser.py
    navigation.py
    challenges.py
    captcha.py
    cookies.py
    interactions.py
    models.py
```

Suggested responsibilities:

### `pool.py`
- Camoufox process lifecycle
- pool health
- checkout / return
- recycling

### `browser.py`
- high-level browser solve workflow

### `navigation.py`
- navigation
- wait policies
- page loading

### `challenges.py`
- challenge detection
- challenge classification

### `captcha.py`
- captcha handling
- token injection
- challenge interaction

### `cookies.py`
- Playwright cookie conversion
- cookie normalization

### `interactions.py`
- age gates
- clicks
- mouse behavior
- human-like interactions

---

## 10. Add an Internal Engine Result Model

Do not infer which tier handled a request based on duration.

Introduce internal metadata such as:

```python
class SolveMetadata:
    tier: str
    duration_ms: float
    challenge_type: str | None
    cache_used: bool
    browser_used: bool
    proxy_used: bool
    attempts: int
```

And:

```python
class EngineResult:
    solution: SolutionModel
    metadata: SolveMetadata
```

Benefits:

- reliable dashboard reporting
- better metrics
- easier debugging
- cleaner API behavior

---

## 11. Add Request-Wide Timeout Budgeting

Treat `maxTimeout` as the total request budget.

At request start:

```python
deadline = time.monotonic() + max_timeout
```

Each stage calculates:

```python
remaining = deadline - time.monotonic()
```

Example:

```text
Total timeout:       60 sec
Fast TLS used:        9 sec
Browser gets:        51 sec
Browser used:        39 sec
Fallback gets:       12 sec
```

Use `asyncio.timeout()` where practical.

---

## 12. Introduce Typed Solver Errors

Replace excessive broad exception handling with explicit internal exceptions:

```python
class SolverrError(Exception):
    pass

class NavigationError(SolverrError):
    pass

class BrowserLaunchError(SolverrError):
    pass

class BrowserPoolError(SolverrError):
    pass

class ChallengeTimeoutError(SolverrError):
    pass

class FastTLSError(SolverrError):
    pass

class ProxyError(SolverrError):
    pass
```

Avoid:

```python
except Exception:
    pass
```

except in tightly controlled cleanup situations.

---

# Priority 2 — Browser Pool Hardening

## 13. Expand Browser Health Tracking

Track per pooled instance:

```text
created_at
last_used
use_count
active_pages
health_status
last_failure
recycle_reason
```

Useful states:

```text
healthy
busy
draining
dead
```

Recycle after:

- browser crash
- Playwright disconnect
- context creation failure
- navigation process failure
- excessive age
- excessive use count
- abnormal resource growth

---

## 14. Improve Cancellation Handling

When an upstream caller gives up, Solverr should cancel work cleanly.

Verify cancellation always:

- closes page
- closes context
- releases semaphore
- returns or recycles browser
- removes in-flight request
- releases queue slot
- does not continue working unnecessarily

Add automated cancellation tests.

---

## 15. Add Browser Queue Visibility

Track:

```text
browser pool size
busy browsers
idle browsers
queue depth
waiting requests
recycles
browser failures
in-flight requests
queue wait time
```

Important metric:

```text
solverr_browser_queue_wait_seconds
```

This will help tune worker counts on the UGREEN NAS.

---

# Priority 3 — Observability

## 16. Replace Simple Averages With Histograms

Instead of only:

```text
avg_fast_ms
avg_browser_ms
```

use Prometheus histograms.

Track:

```text
p50
p90
p95
p99
```

Example:

```text
solverr_request_duration_seconds_bucket{tier="fast_tls"}
solverr_request_duration_seconds_bucket{tier="browser"}
```

---

## 17. Add More Metrics

Recommended metrics:

```text
solverr_browser_pool_size
solverr_browser_pool_busy
solverr_browser_pool_idle
solverr_browser_pool_waiters
solverr_browser_pool_recycles_total
solverr_browser_pool_failures_total
solverr_requests_inflight
solverr_browser_queue_wait_seconds
solverr_cache_hits_total
solverr_cache_misses_total
solverr_timeouts_total
solverr_browser_crashes_total
```

---

## 18. Improve Dashboard

Recommended dashboard cards:

- Fast TLS success rate
- Browser success rate
- Fast TLS fallback rate
- current pool utilization
- current queue depth
- RAM usage
- CPU usage
- p95 latency
- recent browser failures
- recent challenge types
- cache hit ratio

---

# Priority 4 — Security and API Hardening

## 19. Protect `/metrics`

Keep `/health` unauthenticated.

Make `/metrics` configurable:

```text
METRICS_REQUIRE_AUTH=true
```

Default to authenticated for safer deployments.

---

## 20. Remove API Keys From Query Parameters

Avoid:

```text
?api_key=
?key=
```

Keep headers such as:

```http
X-Api-Key: ...
```

or:

```http
Authorization: Bearer ...
```

Use:

```python
hmac.compare_digest()
```

for secret comparison.

---

## 21. Add SSRF Protection

Solverr accepts arbitrary target URLs, which can expose internal services.

Protect against:

- `127.0.0.1`
- localhost
- RFC1918 private IP ranges
- link-local ranges
- cloud metadata addresses
- IPv6 localhost/private ranges
- redirects into blocked ranges

Suggested configuration:

```text
ALLOW_PRIVATE_NETWORKS=false
ALLOWED_HOSTS=
DENIED_HOSTS=
```

Allow private access explicitly when required for trusted LAN setups.

---

## 22. Filter Hop-by-Hop Proxy Headers

Do not blindly forward all incoming headers.

Strip:

```text
Host
Connection
Content-Length
Transfer-Encoding
Keep-Alive
Proxy-Authenticate
Proxy-Authorization
TE
Trailer
Upgrade
```

Reconstruct appropriate outbound headers.

---

## 23. Add Request and Response Size Limits

Suggested configuration:

```text
MAX_REQUEST_BODY_MB
MAX_RESPONSE_BODY_MB
MAX_SCREENSHOT_MB
```

This reduces risk of excessive NAS memory usage.

---

# Priority 5 — Cache and Persistence Cleanup

## 24. Introduce Cache Backend Interfaces

Separate cookie/session business logic from persistence.

Suggested interface:

```text
CookieStore
    MemoryCookieStore
    JsonCookieStore
    RedisCookieStore
```

Common methods:

```python
await store.get(...)
await store.set(...)
await store.delete(...)
await store.list(...)
```

---

## 25. Replace Redis `KEYS`

Avoid:

```python
redis.keys("solverr:cookie:*")
```

Use:

```text
SCAN
```

or maintain Redis index sets.

---

## 26. Add Cache Limits

Recommended settings:

```text
MAX_CACHE_DOMAINS=1000
MAX_COOKIES_PER_DOMAIN=100
MAX_SESSIONS=500
```

Use LRU or age-based eviction.

---

# Priority 6 — Health and Diagnostics

## 27. Split Liveness and Readiness

Add:

```text
/health/live
/health/ready
```

### Liveness

Confirms the process is alive.

### Readiness

Confirms Solverr can accept work.

Potential checks:

- Camoufox available
- browser pool usable
- persistence/cache available
- required Redis connection available
- no permanent initialization failure

---

## 28. Add Browser Self-Test Endpoint

Authenticated endpoint example:

```text
/api/diagnostics/browser
```

Test:

- acquire browser
- create context
- create page
- execute JavaScript
- close page/context
- return browser to pool

---

# Priority 7 — Docker and NAS Improvements

## 29. Improve Auto Worker Detection

Detect Docker/cgroup limits instead of relying only on host-level `psutil`.

Inspect:

```text
/sys/fs/cgroup/memory.max
```

and CPU quota files.

Base auto sizing on effective container resources.

---

## 30. Reduce Minimum Worker Aggressiveness

Avoid automatically forcing a minimum of four CPU-based workers.

Suggested logic:

```text
max(1, floor(effective_cpu_count * 0.75))
```

then clamp by memory.

For the UGREEN NAS, prioritize predictable resource use over maximum concurrency.

---

## 31. Add Container Hardening

Consider:

```yaml
security_opt:
  - no-new-privileges:true

cap_drop:
  - ALL
```

if Camoufox runs correctly under those restrictions.

---

## 32. Investigate Read-Only Filesystem Support

Long-term goal:

```yaml
read_only: true
```

with explicit writable paths for:

```text
/app/data
/tmp
browser runtime/cache paths
```

Requires browser testing.

---

# Priority 8 — CI/CD and Testing

## 33. Add Docker Smoke Test

After building the image in CI:

1. start container
2. wait for `/health`
3. verify 200
4. verify correct Solverr version
5. verify Camoufox availability
6. optionally run browser self-test

This helps catch missing browser dependencies before publishing GHCR images.

---

## 34. Add More Browser Pool Tests

Test:

- browser dies while checked out
- browser fails when returned
- context creation failure
- two or more waiting clients
- shutdown with waiting requests
- recycle while saturated
- cancellation while waiting
- timeout while waiting

---

## 35. Add Load Testing

Create a small load test using:

- k6
- Locust
- or an asyncio script

Test:

```text
1 concurrent
5 concurrent
10 concurrent
25 concurrent
50 concurrent
```

Measure:

- Fast TLS throughput
- browser throughput
- queue time
- RAM usage
- CPU usage
- failed solves
- browser recycle rate
- total solve latency

---

## 36. Add Static Analysis

Add to CI:

```text
ruff
mypy or pyright
bandit
```

Minimum:

```bash
ruff check .
ruff format --check .
```

---

## 37. Add Test Coverage

Use `coverage.py`.

Initial goal:

```text
70%
```

Longer-term:

```text
80–85%
```

Prioritize coverage for:

- engine
- browser pool
- cache
- sessions
- request validation
- timeout/cancellation logic

---

# Priority 9 — Versioning and Dependencies

## 38. Adopt Standard Semantic Versioning

Prefer:

```text
1.5.0
1.6.0
1.7.0
2.0.0
```

instead of:

```text
v1.5.0-ultra
```

If desired, expose edition separately:

```json
{
  "version": "1.5.0",
  "edition": "ultra"
}
```

---

## 39. Use One Version Source

Move version metadata to one source such as:

```text
pyproject.toml
```

Generate or reuse it for:

- API version
- dashboard
- Docker image labels
- logs
- Git tags
- releases

---

## 40. Move Toward `pyproject.toml`

Suggested future dependency layout:

```text
pyproject.toml
uv.lock
```

Separate:

```text
runtime
development
testing
```

dependencies.

---

## 41. Pin Browser Automation Dependencies

Pin important browser/runtime dependencies to known-good versions.

Particularly:

```text
Playwright
Camoufox
curl_cffi
```

Validate upgrades before merging automated dependency updates.

---

# Recommended Release Roadmap

## Solverr 1.5.1 — Stability Release

Focus only on correctness and obvious bugs.

- fix missing `asyncio` import
- fix double `v` version string
- fix `/proxy` URL parsing
- use monotonic timing
- stop exposing raw exceptions
- remove query-string API keys
- constant-time API key comparison
- fix in-flight request fingerprinting
- fix cookie scoping
- fix session cookie replacement

---

## Solverr 1.6 — Engine Cleanup

Refactor internals without major new user-facing features.

- split `browser.py`
- introduce `EngineResult`
- introduce `SolveMetadata`
- add request deadline budgeting
- introduce typed errors
- separate cache persistence backends
- improve browser lifecycle code

Goal: make `engine.py` read like a clear solve flow.

---

## Solverr 1.7 — Observability Release

Add:

- browser pool pressure metrics
- queue depth
- queue wait time
- p50/p95/p99 latency
- per-tier success/failure
- browser crash/recycle metrics
- improved dashboard
- clearer recent error reporting

---

## Solverr 1.8 — Security and Container Hardening

Add:

- SSRF controls
- private-network policy
- metrics authentication
- request/response limits
- hop-by-hop header filtering
- liveness/readiness split
- browser diagnostic endpoint
- Docker hardening
- Docker image smoke testing

---

## Solverr 2.0 — Scalable Job Architecture

Only after 1.x is stable.

Potential architecture:

```text
              ┌─ Fast HTTP workers
Request ──────┤
              └─ Browser queue
                     │
                Browser workers
                     │
                 Browser pool
```

Components:

```text
API layer
   │
Solve coordinator
   │
 ┌─┴───────────┐
 │             │
Fast TLS     Browser queue
engine           │
             Browser pool
```

Goals:

- predictable concurrency
- controlled browser resource use
- cleaner cancellation
- easier horizontal scaling
- Redis becomes optional shared state rather than intertwined logic

---

# UGREEN NAS Optimization Goals

For the UGREEN NAS, optimize for:

```text
Fast path usage ↑
browser reuse ↑
browser launches ↓
memory predictability ↑
queue visibility ↑
failure recovery ↑
```

Prefer a smaller number of healthy browser workers with controlled queuing rather than launching as many browsers as possible.

A target such as:

```text
3–4 healthy browser workers
```

may be preferable to higher concurrency if Solverr shares the NAS with media and automation services.

---

# Top 10 Implementation Order

1. Fix request coalescing isolation.
2. Fix cookie domain/path/name handling.
3. Fix session cookie scoping.
4. Fix immediate bugs (`asyncio`, version string, proxy parsing).
5. Split `browser.py`.
6. Add request-wide timeout budgeting.
7. Return explicit engine/tier metadata.
8. Harden browser pool recovery and cancellation.
9. Add SSRF and API security controls.
10. Add queue/pool/percentile metrics and Docker smoke tests.

---

# Definition of Success

Solverr should eventually make it easy to answer:

- Was the request solved by Fast TLS or browser?
- Why did it escalate?
- Was a cached cookie used?
- How long did it wait for a browser?
- Which challenge was detected?
- Did the browser crash or time out?
- How many browser workers are healthy?
- Are requests being queued?
- Is Solverr leaking memory?
- Is a failure caused by the target site, network, browser, cache, or Solverr itself?

The goal is for Solverr to become a service that can run continuously on the NAS with predictable behavior and enough telemetry to diagnose failures quickly.
