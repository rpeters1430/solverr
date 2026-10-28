#!/usr/bin/env python3
"""Opt-in synthetic browser-worker load check; no browser or target site is started."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import psutil

# Support direct invocation from the repository root without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.flaresolverr import SolutionModel
from app.solver.browser import BrowserPool
from app.solver.browser import browser as browser_module


ALLOWED_SCENARIOS = {"fast", "slow", "failure", "timeout"}


def parse_mix(value: str) -> dict[str, int]:
    mix: dict[str, int] = {}
    for item in value.split(","):
        try:
            name, weight = item.strip().split("=", 1)
            weight = int(weight)
        except (ValueError, TypeError) as exc:
            raise argparse.ArgumentTypeError("request mix must look like fast=80,slow=15,failure=3,timeout=2") from exc
        if name not in ALLOWED_SCENARIOS or weight < 0:
            raise argparse.ArgumentTypeError(f"invalid request mix entry: {item}")
        mix[name] = weight
    if not mix or sum(mix.values()) != 100:
        raise argparse.ArgumentTypeError("request mix weights must sum to 100")
    return mix


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.5)))
    return round(ordered[index], 2)


def process_tree_count() -> int:
    try:
        root = psutil.Process()
        return 1 + len(root.children(recursive=True))
    except (psutil.Error, OSError):
        return 1


async def run_load(args: argparse.Namespace) -> dict:
    rng = random.Random(args.seed)
    exact_counts = {name: args.requests * weight // 100 for name, weight in args.request_mix.items()}
    remaining = args.requests - sum(exact_counts.values())
    remainder_order = sorted(
        args.request_mix,
        key=lambda name: (args.requests * args.request_mix[name] % 100, name),
        reverse=True,
    )
    for name in remainder_order[:remaining]:
        exact_counts[name] += 1
    plan = [name for name, count in exact_counts.items() for _ in range(count)]
    rng.shuffle(plan)
    pool = BrowserPool()
    pool.semaphore = asyncio.Semaphore(args.workers)
    # The synthetic run uses the production BrowserPool admission semaphore but never launches Camoufox.
    pool.camoufox_pool = None
    active_workers = 0
    peak_workers = 0
    latencies: list[float] = []
    outcomes: Counter[str] = Counter()
    solution = SolutionModel(url="http://synthetic.invalid/", status=200, cookies=[], userAgent="load-test")

    async def simulated_browser_solve(**kwargs):
        nonlocal active_workers, peak_workers
        scenario = kwargs["url"].rsplit("/", 1)[-1]
        active_workers += 1
        peak_workers = max(peak_workers, active_workers)
        try:
            if scenario == "fast":
                await asyncio.sleep(args.fast_ms / 1000)
                return solution
            if scenario == "slow":
                await asyncio.sleep(args.slow_ms / 1000)
                return solution
            if scenario == "failure":
                await asyncio.sleep(args.fast_ms / 1000)
                raise RuntimeError("synthetic browser failure")
            await asyncio.sleep(args.timeout_delay_ms / 1000)
            raise TimeoutError("synthetic browser timeout")
        finally:
            active_workers -= 1

    async def submit(index: int, scenario: str) -> None:
        started = time.perf_counter()
        try:
            await pool.solve(
                f"http://synthetic.invalid/{index}/{scenario}",
                timeout_ms=args.timeout_ms,
            )
        except TimeoutError:
            outcomes["timeout"] += 1
        except Exception:
            outcomes["failure"] += 1
        else:
            outcomes["success"] += 1
        finally:
            latencies.append((time.perf_counter() - started) * 1000)

    with patch.object(browser_module, "CAMOUFOX_AVAILABLE", True):
        logging.getLogger("solverr.browser").setLevel(logging.CRITICAL)
        pool._solve_with_ephemeral_camoufox = simulated_browser_solve
        started = time.perf_counter()
        tasks = [asyncio.create_task(submit(i, scenario)) for i, scenario in enumerate(plan)]
        peak_queue = 0
        peak_processes = process_tree_count()
        while not all(task.done() for task in tasks):
            peak_queue = max(peak_queue, pool.pool_stats()["queue_depth"])
            peak_processes = max(peak_processes, process_tree_count())
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started

    timeouts = outcomes["timeout"]
    return {
        "mode": "synthetic BrowserPool admission test (no Camoufox or network targets)",
        "workers": args.workers,
        "requests": args.requests,
        "request_mix_percent": args.request_mix,
        "request_mix_counts": exact_counts,
        "seed": args.seed,
        "latency_inputs_ms": {
            "fast": args.fast_ms,
            "slow": args.slow_ms,
            "timeout_delay": args.timeout_delay_ms,
            "timeout_budget": args.timeout_ms,
        },
        "elapsed_seconds": round(elapsed, 3),
        "throughput_requests_per_second": round(args.requests / elapsed, 2) if elapsed else 0,
        "outcomes": dict(outcomes),
        "timeout_rate": round(timeouts / args.requests, 4) if args.requests else 0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0,
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "peak_active_workers": peak_workers,
        "peak_queue_depth": peak_queue,
        "peak_process_count": peak_processes,
        "process_count_note": "Synthetic mode starts no browser processes; this counts the runner and its child processes.",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2, help="Browser worker semaphore capacity")
    parser.add_argument("--requests", type=int, default=100, help="Fixed request count")
    parser.add_argument("--request-mix", type=parse_mix, default=parse_mix("fast=80,slow=15,failure=3,timeout=2"))
    parser.add_argument("--fast-ms", type=int, default=100)
    parser.add_argument("--slow-ms", type=int, default=700)
    parser.add_argument("--timeout-ms", type=int, default=1000)
    parser.add_argument("--timeout-delay-ms", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=1430)
    args = parser.parse_args()
    if args.workers < 1 or args.requests < 1 or min(args.fast_ms, args.slow_ms, args.timeout_ms, args.timeout_delay_ms) < 0:
        parser.error("workers and requests must be positive; latency values cannot be negative")
    return args


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_load(parse_args())), indent=2, sort_keys=True))
