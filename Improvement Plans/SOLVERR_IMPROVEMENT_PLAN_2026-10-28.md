# Solverr Improvement and Bug Fix Plan

> **For agentic workers:** Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` to implement this plan task-by-task. Keep the checkboxes current.

**Goal:** Close Solverr's remaining high-risk outbound-network security gap, reduce browser solve-flow regression risk, and strengthen release confidence.

**Architecture:** Preserve the existing FastAPI API and four-tier solve pipeline. Put DNS resolution, policy checks, and upstream connection establishment behind one validated egress path shared by Fast TLS and Camoufox; then improve browser-flow testability through characterization tests before refactoring. Keep load testing and dependency updates as separately reviewable work.

**Tech Stack:** Python 3.15, FastAPI, curl_cffi, Playwright/Camoufox, pytest/unittest, Docker Compose, GitHub Actions.

**Spec:** `Improvement Plans/DNS_REBINDING_MITIGATION_PLAN.md`; `Improvement Plans/CODE_REVIEW_2026-09-22_OPEN_FINDINGS.md`; `Improvement Plans/UNIFIED_IMPROVEMENT_PLAN.md`.

## Global Constraints

- Preserve FlareSolverr-compatible `/v1` and `/v2` request and response behavior.
- Keep `DENIED_HOSTS`, `ALLOW_PRIVATE_NETWORKS`, and `ALLOWED_HOSTS` precedence consistent with current documentation and tests.
- Do not claim DNS rebinding is fixed unless the actual socket connection uses the address that passed policy validation.
- Keep caller-supplied external proxies an explicit trusted-proxy path; Solverr cannot verify the remote proxy's final DNS resolution.
- Keep real protected-site checks out of ordinary CI; use controlled local DNS and loopback listeners for deterministic security tests.
- Preserve root and documented PUID/PGID Docker operation, graceful shutdown, and current NAS resource limits.

## Review Focus

- A hostname returns a public address during validation and a private address on a later lookup; the request must never reach the private listener. Test in Task 1.
- DNS returns mixed public and blocked IPv4/IPv6 answers; enforce the configured policy consistently. Test in Task 1.
- Redirects, browser subresources, HTTP CONNECT, cancellation, and concurrent requests must stay within the same egress policy. Test in Task 1.
- A clean page, a challenge that clears, a challenge that times out, and a browser error page must preserve today's solve result semantics. Test in Task 2.
- Worker saturation, browser launch failure, and shutdown must not leak tasks or permanently reduce pool capacity. Test in Task 3.

---

### Task 1: Close the DNS rebinding gap (CR-011)

**Files:**
- Modify: `app/security.py` and egress configuration/startup wiring in `app/main.py` and `app/config.py`
- Modify: `app/solver/fast_tls.py` and `app/solver/browser/navigation.py`
- Test: `tests/test_security.py`
- Create: `tests/test_egress_proxy.py`
- Reference: `Improvement Plans/DNS_REBINDING_MITIGATION_PLAN.md`

- [x] Write deterministic tests using a controlled resolver that changes a hostname from public to loopback between lookups; assert the loopback listener receives no connection.
- [x] Cover public, loopback, RFC1918, link-local, reserved, IPv4, IPv6, mixed DNS answers, resolver errors, and the configured allow/deny precedence.
- [x] Implement one loopback-only policy-enforcing egress proxy. Resolve each destination once, validate every returned address, and connect directly to a validated address. Support HTTP absolute-form requests and HTTPS CONNECT tunnels while preserving end-to-end TLS.
- [x] Route default Fast TLS and Camoufox traffic, redirects, and browser subresources through that egress path. Keep explicit external proxies documented as trusted.
- [x] Test concurrent requests to the same hostname, DNS rotation, cancellation, redirects, HTTP, and HTTPS CONNECT.
- [ ] Build and exercise the container as root and with `PUID=1000` / `PGID=10`; verify FlareSolverr compatibility, user-supplied proxies, health checks, and shutdown.
- [ ] Complete root and `PUID=1000` / `PGID=10` container integration checks; then finalize the CR-011 status and SSRF deployment docs.

**Done when:** Default HTTP and browser egress cannot perform a second unvalidated DNS resolution between policy validation and connection.

### Task 2: Make the browser solve flow safely refactorable (CR-009)

**Files:**
- Modify: `app/solver/browser/browser.py`
- Create: `tests/test_browser_solve_flow.py`
- Modify: `Improvement Plans/CODE_REVIEW_2026-09-22_OPEN_FINDINGS.md`

- [x] Add fake Page/BrowserContext characterization tests for clean load, WAF challenge cleared, WAF challenge still present at timeout, and browser error page.
- [x] Run the new tests against the current implementation and record the expected outcomes before changing control flow; all four passed before the refactor.
- [x] Split `_execute_solve_flow` into focused helpers for context preload, challenge loop, captcha escalation, and solution finalization. Return the challenge loop state in a small result object instead of threading mutable locals through the full method.
- [ ] Run the characterization tests and complete existing unit suite; the four new cases and 32 browser-focused tests pass. The full suite ran 481 tests; 478 passed and three unrelated API stats/auth tests failed because this runner has no process with the PID expected by the existing psutil fixture.
- [x] Update CR-009 status with test coverage and link [PR #133](https://github.com/rpeters1430/solverr/pull/133).

**Done when:** The extracted helpers are independently understandable and the four key browser states remain covered by automated tests.

### Task 3: Add repeatable concurrency and browser-pool reliability checks

**Files:**
- Modify: `tests/test_camoufox_pool.py` and `tests/test_engine.py`
- Create: `scripts/load_test.py` (or a benchmark-local equivalent if it fits the existing benchmark conventions better)
- Modify: `Improvement Plans/UNIFIED_IMPROVEMENT_PLAN.md`

- [x] Add failure-mode tests for worker saturation, failed browser launch/relaunch, request cancellation, and graceful shutdown.
- [x] Add an opt-in local load scenario that reports throughput, latency percentiles, timeout rate, and peak worker/process count for a fixed concurrency and request mix.
- [x] Document a small NAS-sized run profile and a larger CI/developer profile; keep load tests out of normal CI until their runtime and stability are established.
- [x] Record baseline results before changing worker sizing or pool behavior.

**Done when:** Pool failure behavior has regression coverage and load results can be compared across revisions without relying on live protected sites.

### Task 4: Resolve the current dependency update

**Files:**
- Review: GitHub PR #127, `renovate/safe-non-major-dependencies`

- [x] Confirm PR #127's CI is green for pytest 9.2.0 and the GitHub Actions Python patch update.
- [x] Check benchmark/test compatibility and merge the PR if the checks pass; otherwise record the failing check and adjust the update in that PR.
- [x] Keep runtime browser dependencies pinned or version-reviewed separately; do not combine unrelated Camoufox, Playwright, or curl_cffi upgrades into this dependency-only change.

**Done when:** PR #127 is merged or has a documented blocker and next action.

---

## Recommended order

1. Resolve PR #127 as quick maintenance.
2. Implement Task 1 as its own security-focused release item. Do not bundle it with the browser refactor.
3. Complete Task 2 after the controlled characterization tests are in place.
4. Use Task 3 to establish operational baselines before making future concurrency or pool changes.

## Repository review snapshot

Prepared against the public `main` branch on 2026-10-28. The repo is a Python/FastAPI service with an existing broad unit suite, Docker publish and CodSpeed workflows, and one open Renovate dependency PR (#127). The review backlog dated 2026-10-18 identifies CR-009 and CR-011 as open; verify that status again before implementation because the repository has changed since that review.
