# Production Observability and Deadline Enforcement Design

## Context

A week-long Solverr 1.7.0 run on a UGREEN NAS processed 112 requests. It served 99 successfully, failed 13, sent 74 successful requests through Camoufox, and served 25 through the fast or cookie-cache tiers. The successful browser requests averaged 24.5 seconds, five successful browser requests exceeded 60 seconds, and all 13 request failures coincided numerically with browser attempts that exhausted both pooled and ephemeral Camoufox paths.

The current metrics demonstrate that two browser workers are adequate: average queue wait was 155 milliseconds and both workers were idle at the observation point. They do not, however, reveal why requests failed, whether the pooled or ephemeral browser path was responsible, how long failures took, or how much memory the Firefox process tree consumed. The request timeout is also not a hard end-to-end deadline because sequential pooled and ephemeral attempts can each receive the same remaining timeout plus browser-level grace.

## Goals

- Preserve all existing Prometheus metric names, labels, and dashboard/API fields.
- Measure successful and failed request latency from entry to final outcome.
- Classify failures using bounded, non-sensitive reason labels.
- Distinguish pooled and ephemeral browser attempt outcomes.
- Explain pool recycling by age or use budget.
- Report resource use for the Python parent, the complete Solverr process tree, the Solverr process, and the host.
- Enforce a caller's `maxTimeout` as the total request deadline across all tiers and retries.
- Make the built-in dashboard useful for deciding later NAS tuning changes.

## Non-goals

- Changing `MAX_BROWSER_WORKERS` or its auto-tuning algorithm.
- Changing cookie-cache policy, pool recycle limits, proxy configuration, or challenge-solving logic.
- Persisting counters inside Solverr. Prometheus is responsible for retaining time-series history across container restarts.
- Adding target domains, URLs, exception text, proxy addresses, or other unbounded values as metric labels.

## Compatibility

All current Prometheus series remain available with their existing label sets. Existing JSON fields returned by `/api/stats` remain unchanged. New series and JSON fields are additive.

`solverr_memory_bytes` and `solverr_cpu_usage_percent` retain their current value semantics as legacy compatibility metrics. The former remains parent-process RSS, and the latter remains the host-wide CPU sample despite its historical HELP text. New explicitly named metrics provide unambiguous values. This avoids silently changing existing alert and dashboard behavior.

## Request-level telemetry

`PerformanceMetrics` remains the owner of request-level accounting. Each request is finalized exactly once with an outcome and elapsed duration.

Add a Prometheus histogram:

```text
solverr_end_to_end_request_duration_seconds{outcome="success|failure"}
```

It uses the existing histogram buckets and observes every completed request, including failures. Existing tier histograms continue to describe successful tier outcomes only.

Add a failure counter:

```text
solverr_request_failures_total{reason="timeout|budget_exhausted|http_error|browser_error|fast_tls_error|fallback_error|unknown"}
```

Failure classification occurs at the point where the engine finalizes the request. Categories have these meanings:

- `timeout`: a browser or network operation exceeded its allotted time while the total budget had not already expired.
- `budget_exhausted`: no request budget remained before another tier or retry could begin, or the engine's outer hard deadline fired.
- `http_error`: a terminal upstream HTTP response was unsuccessful and no later tier recovered.
- `browser_error`: pooled and ephemeral Camoufox paths failed for a non-timeout reason.
- `fast_tls_error`: a `fastTlsOnly` request failed without producing a response.
- `fallback_error`: a configured fallback-proxy attempt failed.
- `unknown`: a terminal error did not match a more specific category.

Only the final request failure increments this counter. Intermediate attempt failures do not.

## Browser-attempt telemetry

`BrowserPool` owns attempt-level accounting because it selects the pooled and ephemeral paths. Add:

```text
solverr_browser_attempts_total{path="pooled|ephemeral",outcome="success|failure|timeout|http_error"}
```

Every started browser attempt increments exactly one outcome. A pooled failure followed by an ephemeral success therefore records both facts while the request-level metric records one success.

The existing `solverr_browser_crashes_total` remains an aggregate compatibility counter. Its HELP text continues to describe exhaustion of all usable Camoufox paths, rather than a literal process crash.

Retain `solverr_browser_pool_recycles_total` and add:

```text
solverr_browser_pool_recycles_by_reason_total{reason="age|uses"}
```

When both limits are reached, `uses` takes precedence so each recycle has one reason and the sum of the new reason series equals the legacy aggregate.

## Hard request deadline

`RequestBudget` continues to use `time.monotonic()`. The engine applies an outer asynchronous timeout around the complete Tier 3 browser operation using the budget remaining after Fast TLS work. The browser pool may try pooled Camoufox and then ephemeral Camoufox only while that outer deadline remains active.

An early pooled error or unsuccessful HTTP response may still fall back to ephemeral Camoufox. A pooled attempt that consumes the remaining request budget cannot start a new full-duration ephemeral attempt. Tier 4 receives only whatever remains after Tier 3 and is skipped when insufficient budget remains.

The browser's existing per-operation timeouts and wall-clock grace remain internal safety nets, but they cannot extend the engine's public request deadline. Normal scheduling jitter is allowed only at event-loop resolution; no multi-second grace is added to `maxTimeout`.

## Resource telemetry

Add explicitly named gauges:

```text
solverr_process_resident_memory_bytes
solverr_process_tree_resident_memory_bytes
solverr_process_cpu_usage_percent
solverr_host_cpu_usage_percent
```

The parent-process RSS gauge reports the Python process. Process-tree RSS sums the parent and all recursive child processes that still exist while sampled, including Camoufox/Firefox. Processes that exit or become inaccessible during collection are ignored so `/metrics` remains available.

Process CPU uses the Python process sample. Host CPU uses the existing host-wide `psutil.cpu_percent(interval=None)` sample. Collection is non-blocking.

## API and dashboard

`/api/stats` retains all existing fields and adds:

- total successes and total requests;
- success and failure percentages;
- end-to-end success and failure latency summaries available from in-process counters;
- normalized failure counts;
- browser attempt counts by path and outcome;
- pool recycle counts by reason;
- parent and process-tree memory plus process and host CPU values.

The dashboard presents total requests, success rate, failure rate, actual process-tree memory, failure reasons, pooled-versus-ephemeral outcomes, and end-to-end latency. Empty rate and latency datasets render as `Insufficient data`, not a green zero. Existing panels and values remain functional.

Percentiles are not calculated from cumulative Prometheus bucket counters in browser JavaScript. The API supplies any displayed histogram-derived summary, keeping metric interpretation in the backend and dashboard presentation in the frontend.

## Error handling and privacy

Failure normalization is deterministic and does not expose raw exception messages. Raw details continue to be available in sanitized application logs and SSE error events under their existing behavior. Metrics collection never causes `/metrics` or `/api/stats` to fail solely because a child process exits during sampling.

No target-specific values are introduced into metrics or aggregate dashboard data.

## Testing

Automated tests cover:

- all legacy Prometheus series remaining present;
- new request outcome, failure reason, attempt, recycle-reason, and resource series;
- a failed request contributing to failure duration and exactly one normalized reason;
- pooled failure followed by ephemeral success producing two attempt outcomes and one successful request;
- hard deadline exhaustion preventing an over-budget ephemeral or proxy retry;
- early pooled failure retaining ephemeral recovery while time remains;
- process-tree RSS aggregation and disappearing-child handling;
- additive `/api/stats` fields;
- dashboard handling of empty datasets without misleading zero-health values.

Implementation follows test-driven development. Focused tests run after each behavior change, followed by the complete test suite.

## Operational follow-up

After deploying this change, collect at least another week of representative traffic. Pool/cache/retry tuning should then be based on pooled recovery rate, ephemeral recovery rate, failure reasons, process-tree peak memory, request latency including failures, and recycle reasons. A fallback proxy should only be introduced when failure evidence points to network or IP reputation rather than browser instability or exhausted deadlines.
