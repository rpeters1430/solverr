import dataclasses
from typing import Any


@dataclasses.dataclass(eq=False)
class _PooledCamoufox:
    cm: Any
    browser: Any
    created_at: float
    uses: int = 0
    # time.monotonic() of the last check-in, for idle retirement.
    last_used_at: float = 0.0
