# Production Observability and Deadline Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Solverr's production metrics accurately describe failures, browser recovery, full NAS resource use, and caller-visible latency while enforcing `maxTimeout` as a hard request deadline.

**Architecture:** Request-level accounting stays in `PerformanceMetrics`, browser-attempt accounting stays in `BrowserPool`, and a new resource sampler provides one reusable view of parent, child-tree, process, and host utilization. The engine applies one outer monotonic deadline and records a request exactly once; Prometheus, `/api/stats`, and the dashboard consume additive snapshots without changing legacy fields.

**Tech Stack:** Python 3.12, asyncio, FastAPI, psutil, Prometheus text exposition, vanilla JavaScript, `unittest`

**Spec:** `docs/superpowers/specs/2026-09-14-production-observability-and-deadlines-design.md`

## Global Constraints

- Preserve every existing Prometheus metric name and label set.
- Preserve every existing `/api/stats` response field; all response changes are additive.
- Metric labels must be fixed enumerations and must never contain domains, URLs, proxy addresses, or exception text.
- `maxTimeout` is the total request deadline; no browser retry receives a renewed budget.
- Do not change worker auto-tuning, cache behavior, pool limits, proxy policy, or challenge-solving behavior.
- Counters remain process-local and reset on container restart.
- Implement each production behavior only after its test has failed for the expected reason.

---

### Task 1: Request Outcome and Failure Telemetry

**Files:**
- Create: `tests/test_metrics.py`
- Modify: `app/solver/engine.py`
- Modify: `app/metrics.py`

**Interfaces:**
- Produces: `FAILURE_REASONS: tuple[str, ...]`
- Produces: `PerformanceMetrics.record_failure(duration_ms: float = 0.0, reason: str = "unknown") -> None`
- Produces: `PerformanceMetrics.failure_reasons: Dict[str, int]`
- Produces: `PerformanceMetrics.outcome_duration_histograms: Dict[str, Histogram]`
- Produces: additive `to_dict()` fields `successful_requests`, `success_rate_pct`, `failure_rate_pct`, `failure_reasons`, `avg_end_to_end_success_ms`, and `avg_end_to_end_failure_ms`
- Produces: Prometheus series `solverr_end_to_end_request_duration_seconds` and `solverr_request_failures_total`

- [ ] **Step 1: Write failing unit tests for request outcome accounting**

Create `tests/test_metrics.py` with isolated `PerformanceMetrics` instances:

```python
import unittest

from app.solver.engine import FAILURE_REASONS, PerformanceMetrics


class TestPerformanceMetrics(unittest.TestCase):
    def test_success_and_failure_outcomes_have_end_to_end_latency(self):
        subject = PerformanceMetrics()
        subject.record_fast(400.0)
        subject.record_browser(2400.0)
        subject.record_failure(61000.0, "timeout")

        data = subject.to_dict()
        self.assertEqual(data["total_requests"], 3)
        self.assertEqual(data["successful_requests"], 2)
        self.assertEqual(data["failed_requests"], 1)
        self.assertEqual(data["success_rate_pct"], 66.7)
        self.assertEqual(data["failure_rate_pct"], 33.3)
        self.assertEqual(data["avg_end_to_end_success_ms"], 1400.0)
        self.assertEqual(data["avg_end_to_end_failure_ms"], 61000.0)
        self.assertEqual(subject.outcome_duration_histograms["success"].count, 2)
        self.assertEqual(subject.outcome_duration_histograms["failure"].count, 1)

    def test_failure_reason_is_bounded_and_unknown_is_safe_default(self):
        subject = PerformanceMetrics()
        subject.record_failure(1.0, "raw exception containing https://secret.example")

        self.assertEqual(subject.failure_reasons["unknown"], 1)
        self.assertEqual(set(subject.failure_reasons), set(FAILURE_REASONS))
        self.assertNotIn("raw exception containing https://secret.example", subject.failure_reasons)
```

- [ ] **Step 2: Run the new tests and verify RED**

Run: `python -m unittest tests.test_metrics -v`

Expected: import or assertion failure because `FAILURE_REASONS`, outcome histograms, and duration-aware failure recording do not exist.

- [ ] **Step 3: Implement minimal outcome accounting**

In `app/solver/engine.py`, define the bounded reasons and extend `PerformanceMetrics`:

```python
FAILURE_REASONS = (
    "timeout", "budget_exhausted", "http_error", "browser_error",
    "fast_tls_error", "fallback_error", "unknown",
)

class PerformanceMetrics:
    def __init__(self):
        # retain all existing fields
        self.failure_reasons = {reason: 0 for reason in FAILURE_REASONS}
        self.outcome_duration_histograms = {
            "success": Histogram(),
            "failure": Histogram(),
        }

    def _record_success_outcome(self, duration_ms: float) -> None:
        self.outcome_duration_histograms["success"].observe(duration_ms / 1000.0)

    def record_failure(self, duration_ms: float = 0.0, reason: str = "unknown") -> None:
        self.total_requests += 1
        self.failed_requests += 1
        normalized = reason if reason in self.failure_reasons else "unknown"
        self.failure_reasons[normalized] += 1
        self.outcome_duration_histograms["failure"].observe(duration_ms / 1000.0)
```

Call `_record_success_outcome(duration_ms)` from `record_fast`, `record_cache`, `record_browser`, and `record_fallback_proxy`. In `to_dict()`, derive rates and averages from the two outcome histograms, returning `0.0` for API compatibility when a histogram is empty.

- [ ] **Step 4: Verify request metric tests are GREEN**

Run: `python -m unittest tests.test_metrics -v`

Expected: both tests pass.

- [ ] **Step 5: Write a failing Prometheus exposition test**

Extend `tests/test_metrics.py` by patching the module-global metric and resource inputs so the test is deterministic:

```python
from unittest.mock import patch

from app.metrics import generate_prometheus_metrics

def test_new_request_metrics_are_exposed(self):
    subject = PerformanceMetrics()
    subject.record_fast(400.0)
    subject.record_failure(61000.0, "timeout")
    with patch("app.metrics.metrics", subject):
        body = generate_prometheus_metrics()
    self.assertIn('solverr_request_failures_total{reason="timeout"} 1', body)
    self.assertIn('solverr_request_failures_total{reason="unknown"} 0', body)
    self.assertIn('solverr_end_to_end_request_duration_seconds_count{outcome="success"} 1', body)
    self.assertIn('solverr_end_to_end_request_duration_seconds_count{outcome="failure"} 1', body)
```

- [ ] **Step 6: Run the exposition test and verify RED**

Run: `python -m unittest tests.test_metrics.TestPerformanceMetrics.test_new_request_metrics_are_exposed -v`

Expected: failure because the new series are absent.

- [ ] **Step 7: Render bounded failure counters and outcome histograms**

In `app/metrics.py`, append HELP/TYPE blocks, loop over `FAILURE_REASONS`, and render both outcome histograms using the existing cumulative bucket format. Never discover label values dynamically from exception input.

- [ ] **Step 8: Run focused and compatibility tests**

Run: `python -m unittest tests.test_metrics tests.test_api.TestAPIEndpoints.test_prometheus_metrics -v`

Expected: all tests pass and all legacy series remain present.

- [ ] **Step 9: Commit request telemetry**

```powershell
git add app/solver/engine.py app/metrics.py tests/test_metrics.py
git commit -m "feat: measure request outcomes and failures"
```

---

### Task 2: Browser Attempt and Pool Recycle Telemetry

**Files:**
- Modify: `app/solver/browser/browser.py`
- Modify: `app/solver/browser/pool.py`
- Modify: `app/metrics.py`
- Modify: `tests/test_camoufox_pool.py`
- Modify: `tests/test_metrics.py`

**Interfaces:**
- Produces: `BrowserPool.attempts: Dict[str, Dict[str, int]]`
- Produces: `BrowserPool.record_attempt(path: str, outcome: str) -> None`
- Produces: `BrowserSolveError(reason: str, message: str)`, whose `reason` is limited to `http_error` or `browser_error`
- Produces: `CamoufoxPool.recycle_reasons: Dict[str, int]`
- Extends: `BrowserPool.pool_stats()` with `attempts` and `recycle_reasons`
- Produces: `solverr_browser_attempts_total{path,outcome}`
- Produces: `solverr_browser_pool_recycles_by_reason_total{reason}`

- [ ] **Step 1: Write failing recycle-reason tests**

Add to `TestCamoufoxPool`:

```python
async def test_recycle_reason_counts_use_budget(self):
    with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 1), \
         patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 100000):
        pool = FakeCamoufoxPool(1)
        inst = await pool.acquire()
        await pool.release(inst)
    self.assertEqual(pool.recycles_total, 1)
    self.assertEqual(pool.recycle_reasons, {"age": 0, "uses": 1})

async def test_recycle_reason_counts_age_budget(self):
    with patch.object(settings, "CAMOUFOX_POOL_RECYCLE_USES", 100), \
         patch.object(settings, "CAMOUFOX_POOL_RECYCLE_SECONDS", 10):
        pool = FakeCamoufoxPool(1)
        inst = await pool.acquire()
        inst.created_at = time.monotonic() - 11
        await pool.release(inst)
    self.assertEqual(pool.recycle_reasons, {"age": 1, "uses": 0})
```

- [ ] **Step 2: Run recycle tests and verify RED**

Run: `python -m unittest tests.test_camoufox_pool.TestCamoufoxPool.test_recycle_reason_counts_use_budget tests.test_camoufox_pool.TestCamoufoxPool.test_recycle_reason_counts_age_budget -v`

Expected: failure because `recycle_reasons` does not exist.

- [ ] **Step 3: Implement deterministic recycle classification**

In `CamoufoxPool.__init__`, initialize `self.recycle_reasons = {"age": 0, "uses": 0}`. Replace `_should_recycle` with:

```python
def _recycle_reason(self, inst: _PooledCamoufox) -> Optional[str]:
    if inst.uses >= settings.CAMOUFOX_POOL_RECYCLE_USES:
        return "uses"
    if (time.monotonic() - inst.created_at) >= settings.CAMOUFOX_POOL_RECYCLE_SECONDS:
        return "age"
    return None
```

In `release`, recycle when the returned reason is not `None`, increment both `recycles_total` and `recycle_reasons[reason]`, and preserve the existing close/relaunch behavior.

- [ ] **Step 4: Verify recycle tests are GREEN**

Run: `python -m unittest tests.test_camoufox_pool.TestCamoufoxPool -v`

Expected: all pool lifecycle tests pass.

- [ ] **Step 5: Write failing browser-attempt recovery tests**

Add a test class that stubs the two solve paths without launching Camoufox:

```python
from unittest.mock import AsyncMock
from app.models.flaresolverr import SolutionModel

class TestBrowserAttemptMetrics(unittest.IsolatedAsyncioTestCase):
    async def test_pooled_failure_then_ephemeral_success_records_both(self):
        pool = BrowserPool()
        pool.camoufox_pool = object()
        pool._solve_with_pooled_camoufox = AsyncMock(side_effect=RuntimeError("pool failed"))
        pool._solve_with_ephemeral_camoufox = AsyncMock(return_value=SolutionModel(
            url="https://example.com", status=200, response="ok", cookies=[]
        ))

        result = await pool.solve("https://example.com", timeout_ms=5000)

        self.assertEqual(result.status, 200)
        self.assertEqual(pool.attempts["pooled"]["failure"], 1)
        self.assertEqual(pool.attempts["ephemeral"]["success"], 1)

    async def test_unsuccessful_http_status_is_not_called_a_process_failure(self):
        pool = BrowserPool()
        pool.camoufox_pool = None
        pool._solve_with_ephemeral_camoufox = AsyncMock(return_value=SolutionModel(
            url="https://example.com", status=403, response="blocked", cookies=[]
        ))

        with self.assertRaises(RuntimeError):
            await pool.solve("https://example.com", timeout_ms=5000)

        self.assertEqual(pool.attempts["ephemeral"]["http_error"], 1)
```

- [ ] **Step 6: Run attempt tests and verify RED**

Run: `python -m unittest tests.test_camoufox_pool.TestBrowserAttemptMetrics -v`

Expected: failure because `BrowserPool.attempts` does not exist.

- [ ] **Step 7: Implement bounded attempt accounting**

In `browser.py`, define fixed paths/outcomes and initialize the complete matrix:

```python
BROWSER_PATHS = ("pooled", "ephemeral")
BROWSER_OUTCOMES = ("success", "failure", "timeout", "http_error")

self.attempts = {
    path: {outcome: 0 for outcome in BROWSER_OUTCOMES}
    for path in BROWSER_PATHS
}

def record_attempt(self, path: str, outcome: str) -> None:
    if path in self.attempts and outcome in self.attempts[path]:
        self.attempts[path][outcome] += 1
```

Record exactly one outcome beside each pooled and ephemeral attempt. Treat `asyncio.TimeoutError`, built-in `TimeoutError`, and deadline cancellation as `timeout`; status `>= 400` as `http_error`; other exceptions as `failure`; and status `< 400` as `success`. Re-raise cancellation after recording it. Include deep copies of `attempts` and `recycle_reasons` in `pool_stats()` so callers cannot mutate internal counters.

Replace the terminal generic `RuntimeError` from `BrowserPool.solve` with a `BrowserSolveError`. Set its fixed `reason` to `http_error` when the last completed attempt returned status `>= 400`; otherwise use `browser_error`. Preserve the existing human-readable message and exception chaining so API/log diagnostics remain useful without entering metric labels.

```python
class BrowserSolveError(RuntimeError):
    def __init__(self, reason: str, message: str):
        self.reason = reason if reason in ("http_error", "browser_error") else "browser_error"
        super().__init__(message)
```

- [ ] **Step 8: Verify attempt tests are GREEN**

Run: `python -m unittest tests.test_camoufox_pool -v`

Expected: all tests pass.

- [ ] **Step 9: Write and satisfy Prometheus rendering tests**

Extend `tests/test_metrics.py` to assert fixed zero-valued series are present, including:

```python
self.assertIn('solverr_browser_attempts_total{path="pooled",outcome="success"}', body)
self.assertIn('solverr_browser_attempts_total{path="ephemeral",outcome="timeout"}', body)
self.assertIn('solverr_browser_pool_recycles_by_reason_total{reason="age"}', body)
self.assertIn('solverr_browser_pool_recycles_by_reason_total{reason="uses"}', body)
```

Run it once to observe the expected absence, then update `app/metrics.py` to render the fixed path/outcome matrix and recycle reasons. Retain the aggregate recycle and crash series.

- [ ] **Step 10: Run focused tests and commit**

Run: `python -m unittest tests.test_camoufox_pool tests.test_metrics tests.test_api.TestAPIEndpoints.test_prometheus_metrics -v`

Expected: all tests pass.

```powershell
git add app/solver/browser/browser.py app/solver/browser/pool.py app/metrics.py tests/test_camoufox_pool.py tests/test_metrics.py
git commit -m "feat: expose browser recovery telemetry"
```

---

### Task 3: Hard End-to-End Request Deadline and Failure Classification

**Files:**
- Modify: `app/solver/engine.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_metrics.py`

**Interfaces:**
- Produces: `classify_failure(error: BaseException, budget: RequestBudget) -> str`
- Produces: `tag_failure(error: BaseException, reason: str) -> BaseException`
- Changes: the primary `_do_process_request` operation is bounded by `RequestBudget.remaining_s`
- Preserves: `HybridSolverEngine.process_request(req) -> SolutionModel`

- [ ] **Step 1: Write a failing hard-deadline test**

Add to `TestHybridSolverEngine` and patch the module-global metrics with an isolated instance:

```python
async def test_max_timeout_bounds_entire_browser_operation(self):
    import asyncio
    from app.solver.engine import PerformanceMetrics

    observed = PerformanceMetrics()

    async def hangs(*args, **kwargs):
        await asyncio.sleep(2.0)

    with patch("app.solver.engine.metrics", observed), \
         patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=hangs)), \
         patch.object(settings, "FALLBACK_PROXY_URL", "http://fallback.invalid:8080"):
        req = V1Request(cmd="request.get", url="https://example.com", forceBrowser=True, maxTimeout=1000)
        started = asyncio.get_running_loop().time()
        with self.assertRaises(TimeoutError):
            await self.engine.process_request(req)
        elapsed = asyncio.get_running_loop().time() - started

    self.assertLess(elapsed, 1.5)
    self.assertEqual(observed.failed_requests, 1)
    self.assertEqual(observed.failure_reasons["budget_exhausted"], 1)
```

- [ ] **Step 2: Run the deadline test and verify RED**

Run: `python -m unittest tests.test_engine.TestHybridSolverEngine.test_max_timeout_bounds_entire_browser_operation -v`

Expected: without the hard outer deadline it takes about two seconds or records no duration-aware failure. The production code's existing one-second minimum remains unchanged.

- [ ] **Step 3: Apply the hard outer deadline and centralized failure finalization**

In `process_request`, only the owner of the deduplicated future records the outcome. Wrap `_do_process_request`:

```python
try:
    res = await asyncio.wait_for(
        self._do_process_request(req, budget, url, method),
        timeout=budget.remaining_s,
    )
    _cap_response_body(res)
    if not future.done():
        future.set_result(res)
    return res
except Exception as error:
    reason = classify_failure(error, budget)
    metrics.record_failure(budget.elapsed_ms, reason)
    if not future.done():
        future.set_exception(error)
        future.exception()
    raise
```

Remove all inner `metrics.record_failure()` calls so finalization occurs exactly once. Preserve `record_fast`, `record_cache`, `record_browser`, and `record_fallback_proxy` as the success finalizers. Before re-raising terminal fallback and fast-TLS-only errors, attach a fixed private reason marker via this helper; `classify_failure` consumes only that fixed marker, known exception types, and `BrowserSolveError.reason` from Task 2:

```python
def tag_failure(error: BaseException, reason: str) -> BaseException:
    normalized = reason if reason in FAILURE_REASONS else "unknown"
    setattr(error, "_solverr_failure_reason", normalized)
    return error


def classify_failure(error: BaseException, budget: RequestBudget) -> str:
    tagged = getattr(error, "_solverr_failure_reason", None)
    if tagged in FAILURE_REASONS:
        return tagged
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return "budget_exhausted" if budget.is_expired else "timeout"
    if isinstance(error, BrowserSolveError):
        return error.reason
    return "browser_error"
```

Raise terminal fast-only failures with `raise tag_failure(error, "fast_tls_error")`, terminal fallback failures with `raise tag_failure(fallback_error, "fallback_error")`, and pre-tier budget checks with a tagged `TimeoutError` using `budget_exhausted`.

When the outer `asyncio.wait_for` fires, raise a message-bearing built-in `TimeoutError` such as `Request timeout budget exhausted after 1000ms` and classify it as `budget_exhausted`. Do not add timeout grace.

- [ ] **Step 4: Verify the hard deadline test is GREEN**

Run: `python -m unittest tests.test_engine.TestHybridSolverEngine.test_max_timeout_bounds_entire_browser_operation -v`

Expected: pass within the asserted wall time with one failure observation.

- [ ] **Step 5: Add failing classification and no-double-count tests**

Add tests covering:

```python
def test_classify_failure_uses_only_bounded_reasons(self):
    from app.solver.engine import RequestBudget, classify_failure
    budget = RequestBudget(5000)
    self.assertEqual(classify_failure(TimeoutError("operation timed out"), budget), "timeout")
    self.assertEqual(classify_failure(RuntimeError("https://secret.example"), budget), "browser_error")

async def test_terminal_browser_failure_is_recorded_once(self):
    from app.solver.engine import PerformanceMetrics
    observed = PerformanceMetrics()
    with patch("app.solver.engine.metrics", observed), \
         patch("app.solver.engine.browser_pool.solve", new=AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(settings, "FALLBACK_PROXY_URL", None):
        req = V1Request(cmd="request.get", url="https://example.com", forceBrowser=True)
        with self.assertRaises(RuntimeError):
            await self.engine.process_request(req)
    self.assertEqual(observed.total_requests, 1)
    self.assertEqual(observed.failed_requests, 1)
    self.assertEqual(observed.outcome_duration_histograms["failure"].count, 1)
```

Also update existing fallback tests to assert that an early direct-browser failure can still reach and succeed through Tier 4 while budget remains.

- [ ] **Step 6: Run the new tests and verify RED, then implement classification**

Run: `python -m unittest tests.test_engine -v`

Expected before implementation: missing helper or wrong/double accounting. Implement the fixed-marker helper and classifier, with `budget_exhausted` taking precedence only when the outer deadline fired; an ordinary operation timeout while time remains is `timeout`.

- [ ] **Step 7: Run engine and telemetry tests**

Run: `python -m unittest tests.test_engine tests.test_metrics tests.test_camoufox_pool -v`

Expected: all tests pass, existing deduplication behavior remains unchanged, and coalesced waiters do not create extra metric observations.

- [ ] **Step 8: Commit deadline enforcement**

```powershell
git add app/solver/engine.py tests/test_engine.py tests/test_metrics.py
git commit -m "fix: enforce total request timeout budget"
```

---

### Task 4: NAS Process-Tree Resource Metrics

**Files:**
- Create: `app/resource_metrics.py`
- Create: `tests/test_resource_metrics.py`
- Modify: `app/metrics.py`
- Modify: `app/api/dashboard.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Produces: `ResourceSnapshot` dataclass with `parent_rss_bytes`, `tree_rss_bytes`, `process_cpu_percent`, and `host_cpu_percent`
- Produces: `collect_resource_snapshot(proc: Optional[psutil.Process] = None) -> ResourceSnapshot`
- Produces: four explicit Prometheus gauges from the approved design
- Produces: additive `/api/stats` fields `process_ram_usage_mb`, `process_tree_ram_usage_mb`, `process_cpu_usage_pct`, and `host_cpu_usage_pct`

- [ ] **Step 1: Write failing resource aggregation tests**

Create `tests/test_resource_metrics.py`:

```python
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.resource_metrics import collect_resource_snapshot


class TestResourceMetrics(unittest.TestCase):
    def test_process_tree_rss_includes_parent_and_recursive_children(self):
        parent = Mock()
        parent.memory_info.return_value = SimpleNamespace(rss=100)
        parent.cpu_percent.return_value = 4.5
        child_a = Mock()
        child_a.memory_info.return_value = SimpleNamespace(rss=200)
        child_b = Mock()
        child_b.memory_info.return_value = SimpleNamespace(rss=300)
        parent.children.return_value = [child_a, child_b]

        with patch("app.resource_metrics.psutil.cpu_percent", return_value=12.0):
            result = collect_resource_snapshot(parent)

        self.assertEqual(result.parent_rss_bytes, 100)
        self.assertEqual(result.tree_rss_bytes, 600)
        self.assertEqual(result.process_cpu_percent, 4.5)
        self.assertEqual(result.host_cpu_percent, 12.0)
        parent.children.assert_called_once_with(recursive=True)

    def test_disappearing_child_does_not_break_collection(self):
        parent = Mock()
        parent.memory_info.return_value = SimpleNamespace(rss=100)
        parent.cpu_percent.return_value = 1.0
        vanished = Mock()
        vanished.memory_info.side_effect = __import__("psutil").NoSuchProcess(123)
        parent.children.return_value = [vanished]

        result = collect_resource_snapshot(parent)

        self.assertEqual(result.tree_rss_bytes, 100)
```

- [ ] **Step 2: Run resource tests and verify RED**

Run: `python -m unittest tests.test_resource_metrics -v`

Expected: import failure because `app.resource_metrics` does not exist.

- [ ] **Step 3: Implement the non-blocking resource sampler**

Create `app/resource_metrics.py`:

```python
import os
from dataclasses import dataclass
from typing import Optional

import psutil


@dataclass(frozen=True)
class ResourceSnapshot:
    parent_rss_bytes: int
    tree_rss_bytes: int
    process_cpu_percent: float
    host_cpu_percent: float


def collect_resource_snapshot(proc: Optional[psutil.Process] = None) -> ResourceSnapshot:
    active = proc or psutil.Process(os.getpid())
    parent_rss = active.memory_info().rss
    tree_rss = parent_rss
    try:
        children = active.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for child in children:
        try:
            tree_rss += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    try:
        process_cpu = active.cpu_percent(interval=None)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process_cpu = 0.0
    return ResourceSnapshot(
        parent_rss_bytes=parent_rss,
        tree_rss_bytes=tree_rss,
        process_cpu_percent=process_cpu,
        host_cpu_percent=psutil.cpu_percent(interval=None),
    )
```

- [ ] **Step 4: Verify resource tests are GREEN**

Run: `python -m unittest tests.test_resource_metrics -v`

Expected: both tests pass.

- [ ] **Step 5: Write failing endpoint/exposition tests**

In `tests/test_api.py`, assert the four new metric names and API keys. Patch `collect_resource_snapshot` with a fixed `ResourceSnapshot` to verify exact conversion of bytes to MiB. Retain assertions for `ram_usage_mb` and `cpu_usage_pct`.

- [ ] **Step 6: Run endpoint tests and verify RED**

Run: `python -m unittest tests.test_api.TestAPIEndpoints.test_prometheus_metrics tests.test_api.TestAPIEndpoints.test_dashboard_api -v`

Expected: assertions fail because explicit resource fields are absent.

- [ ] **Step 7: Use one sampler in each endpoint**

Replace direct psutil calls in `app/metrics.py` and `app/api/dashboard.py` with `collect_resource_snapshot()`. Keep legacy mappings exact:

```python
legacy_memory = snapshot.parent_rss_bytes
legacy_cpu = snapshot.host_cpu_percent
```

Render explicit gauges and add the four JSON fields. Convert bytes to MiB only for dashboard JSON; Prometheus stays in bytes.

- [ ] **Step 8: Run focused tests and commit**

Run: `python -m unittest tests.test_resource_metrics tests.test_api tests.test_metrics -v`

Expected: all tests pass.

```powershell
git add app/resource_metrics.py app/metrics.py app/api/dashboard.py tests/test_resource_metrics.py tests/test_api.py tests/test_metrics.py
git commit -m "feat: report full Solverr process-tree resources"
```

---

### Task 5: Additive Stats API and Production Dashboard

**Files:**
- Modify: `app/api/dashboard.py`
- Modify: `app/templates/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/style.css`
- Modify: `tests/test_api.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: request outcome fields from `PerformanceMetrics.to_dict()`
- Consumes: `BrowserPool.pool_stats()` attempt and recycle dictionaries
- Consumes: explicit resource fields from Task 4
- Produces: dashboard nodes for success/failure health, request latency, failure reasons, browser recovery, and process-tree memory

- [ ] **Step 1: Write failing additive stats and HTML contract tests**

Extend `tests/test_api.py`:

```python
def test_dashboard_stats_include_production_health_fields(self):
    data = self.client.get("/api/stats").json()
    for key in (
        "successful_requests", "success_rate_pct", "failure_rate_pct",
        "failure_reasons", "avg_end_to_end_success_ms",
        "avg_end_to_end_failure_ms", "process_tree_ram_usage_mb",
    ):
        self.assertIn(key, data)
    self.assertIn("attempts", data["browser_pool"])
    self.assertIn("recycle_reasons", data["browser_pool"])

def test_dashboard_has_insufficient_data_placeholders(self):
    html = self.client.get("/").text
    self.assertIn('id="val-success-rate"', html)
    self.assertIn('id="val-failure-rate"', html)
    self.assertIn('id="val-tree-ram"', html)
    self.assertIn('id="val-failure-reasons"', html)
    self.assertIn('id="val-browser-attempts"', html)
    self.assertIn("Insufficient data", html)
```

- [ ] **Step 2: Run dashboard tests and verify RED**

Run: `python -m unittest tests.test_api.TestAPIEndpoints.test_dashboard_stats_include_production_health_fields tests.test_api.TestAPIEndpoints.test_dashboard_has_insufficient_data_placeholders -v`

Expected: missing dashboard element assertions fail.

- [ ] **Step 3: Add semantic dashboard panels**

In `index.html`, retain the existing cards and add compact cards/panels with the exact IDs above. Use text labels, not color alone, for outcome state. Rename the visible RAM label to `Process-tree RAM` while leaving the old element available or moving `val-ram` to a parent-process footnote. Replace the hard-coded Tier 3 estimate `~1.8-3s` with `Measured below` because the observed workload is much slower.

Render empty metric values initially as `Insufficient data`, and add list containers for failure reasons and browser attempts. Use existing card and panel styles, adding only reusable `.health-grid`, `.health-list`, and `.metric-empty` classes to `style.css`.

- [ ] **Step 4: Populate the new panels safely**

In `app.js`, add pure formatting helpers near `escapeHtml`:

```javascript
function formatPercent(value, count) {
    return count > 0 && Number.isFinite(Number(value)) ? `${Number(value).toFixed(1)}%` : 'Insufficient data';
}

function formatLatency(value, count) {
    return count > 0 && Number.isFinite(Number(value)) ? `${Math.round(Number(value))}ms` : 'Insufficient data';
}

function renderCountMap(target, values) {
    const rows = Object.entries(values || {}).filter(([, count]) => Number(count) > 0);
    target.textContent = '';
    if (!rows.length) {
        target.textContent = 'Insufficient data';
        target.classList.add('metric-empty');
        return;
    }
    target.classList.remove('metric-empty');
    for (const [label, count] of rows) {
        const row = document.createElement('div');
        row.textContent = `${label.replaceAll('_', ' ')}: ${count}`;
        target.appendChild(row);
    }
}
```

Use `textContent` for aggregate data. Populate rates using `data.total_requests`, success latency using `data.successful_requests`, failure latency using `data.failed_requests`, memory using `data.process_tree_ram_usage_mb`, reasons using `data.failure_reasons`, and attempts by flattening fixed path/outcome entries from `data.browser_pool.attempts`. Avoid `value || 0` for health indicators because missing data must not look healthy.

- [ ] **Step 5: Verify dashboard contracts are GREEN**

Run: `python -m unittest tests.test_api -v`

Expected: all API and static HTML contract tests pass.

- [ ] **Step 6: Document metric meaning and restart behavior**

Add a README subsection after environment configuration that lists the new metrics, explains legacy CPU/memory semantics, states that in-process counters reset on restart, and provides these PromQL examples:

```promql
sum(rate(solverr_requests_total{status="success"}[15m]))
/
sum(rate(solverr_requests_total[15m]))

sum(rate(solverr_request_failures_total[15m])) by (reason)

solverr_process_tree_resident_memory_bytes / 1024 / 1024
```

Clarify that tuning decisions should use at least a week of representative traffic and that no target labels are emitted.

- [ ] **Step 7: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass with no errors, unhandled-future warnings, or resource warnings.

- [ ] **Step 8: Run repository hygiene checks**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: only the files named in this task are modified; the pre-existing untracked `.claude/` directory remains untouched.

- [ ] **Step 9: Commit dashboard and documentation**

```powershell
git add app/api/dashboard.py app/templates/index.html app/static/app.js app/static/style.css tests/test_api.py README.md
git commit -m "feat: add production health dashboard"
```

---

### Task 6: Final Compatibility and Operational Verification

**Files:**
- Modify only if verification reveals a defect: files already listed in Tasks 1-5 and their corresponding tests

**Interfaces:**
- Verifies all interfaces and constraints from the approved design.

- [ ] **Step 1: Run the complete suite from a clean process**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 2: Verify Prometheus exposition invariants**

Run: `python -m unittest tests.test_api.TestAPIEndpoints.test_prometheus_metrics tests.test_metrics -v`

Expected: legacy and new series coexist, histogram bucket counts are cumulative, and every emitted label belongs to a fixed enumeration.

- [ ] **Step 3: Verify the application imports**

Run: `python -m py_compile app/solver/engine.py app/solver/browser/browser.py app/solver/browser/pool.py app/resource_metrics.py app/metrics.py app/api/dashboard.py`

Expected: exit code 0 and no output.

- [ ] **Step 4: Review the final diff against the spec**

Run: `git diff HEAD~5 -- docs/superpowers/specs/2026-09-14-production-observability-and-deadlines-design.md app tests README.md`

Check each goal and non-goal explicitly: no tuning defaults changed, no unbounded labels exist, legacy fields remain, failure durations are present, browser attempts are split, process-tree memory is exposed, and total deadlines are enforced.

- [ ] **Step 5: Confirm repository state**

Run: `git status --short`

Expected: no tracked modifications remain. Do not add, edit, or remove the user's pre-existing `.claude/` directory.
