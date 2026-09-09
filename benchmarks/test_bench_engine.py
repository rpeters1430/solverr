"""Engine bookkeeping: metrics, response capping and request deduplication.

`PerformanceMetrics` is updated on every solve and rendered on every
`/metrics` scrape; `_cap_response_body` re-encodes every response body to
enforce `MAX_RESPONSE_BODY_MB`; `process_request`'s prologue (SSRF check +
SHA-256 dedup fingerprint + in-flight future bookkeeping) is paid by every
request before any tier runs.
"""

import asyncio

from app.metrics import generate_prometheus_metrics
from app.models.flaresolverr import SolutionModel, V1Request
from app.solver.engine import HybridSolverEngine, PerformanceMetrics, RequestBudget, _cap_response_body

from sample_data import CLEAN_PAGE_HTML, V1_REQUEST_PAYLOAD, make_cookies


def _record_batch(perf: PerformanceMetrics) -> dict:
    for i in range(50):
        perf.record_fast(35.0 + i)
        perf.record_cache(12.0 + i)
        perf.record_browser(1800.0 + i, challenge_type="cloudflare_turnstile")
        perf.record_cookie_cache_lookup(hit=bool(i % 2))
    return perf.to_dict()


def test_metrics_record_and_snapshot(benchmark):
    """Histogram observation plus the aggregation `/api/stats` reads."""
    perf = PerformanceMetrics()
    result = benchmark(_record_batch, perf)
    # The harness may call the benchmark several times (warmup + measured
    # run), so assert on a per-call invariant rather than an absolute count.
    assert result["total_requests"] % 150 == 0


def test_prometheus_exposition(benchmark):
    """Full `/metrics` payload rendering."""
    result = benchmark(generate_prometheus_metrics)
    assert "solverr_requests_total" in result


def test_request_budget(benchmark):
    def budget_cycle():
        budget = RequestBudget(60000)
        return budget.remaining_ms + int(budget.elapsed_ms) + int(budget.is_expired)

    assert benchmark(budget_cycle) >= 0


def test_cap_response_body(benchmark):
    """UTF-8 length check over a full page body (under the cap, so no copy)."""
    solution = SolutionModel(url="https://indexer.example.com/", status=200, response=CLEAN_PAGE_HTML * 4)
    benchmark(_cap_response_body, solution)
    assert solution.response


def test_process_request_dispatch(benchmark):
    """Everything `process_request` does around the actual solve: SSRF policy
    check, dedup fingerprint hashing and in-flight future bookkeeping. The
    tier work itself is stubbed out - it is network/browser bound."""
    engine = HybridSolverEngine()
    payload = dict(V1_REQUEST_PAYLOAD)
    # IP literal so the SSRF check does not perform a DNS lookup.
    payload["url"] = "https://93.184.216.34/api?t=search&q=ubuntu"
    payload["proxy"] = None
    req = V1Request.model_validate(payload)

    solution = SolutionModel(
        url=req.url,
        status=200,
        response="<html></html>",
        cookies=make_cookies(10),
        tier="tier1_fast_tls",
    )

    async def _stub(_req, _budget, _url, _method):
        return solution

    engine._do_process_request = _stub

    loop = asyncio.new_event_loop()
    try:
        result = benchmark(lambda: loop.run_until_complete(engine.process_request(req)))
    finally:
        loop.close()
    assert result.status == 200
