"""Shared fixtures for the CodSpeed benchmark suite.

Every benchmark in this directory exercises a CPU-bound hot path of the solve
pipeline: challenge detection, the cookie cache, session bookkeeping,
request/response model validation, HTML extraction and metrics rendering.
Nothing here touches the network, the disk, or launches Camoufox - the
browser/HTTP tiers themselves are not measurable in a deterministic CI
environment, so only the pure logic around them is benchmarked.
"""

import logging
import random

import pytest

# INFO logging writes to stdout on every cache/session mutation and would skew timings.
logging.disable(logging.INFO)


@pytest.fixture
def seeded_random():
    """Deterministic RNG so randomized code paths (Bezier control points,
    jitter) do the same amount of work on every run."""
    state = random.getstate()
    random.seed(1234)
    yield random
    random.setstate(state)
