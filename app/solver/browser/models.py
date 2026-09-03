import dataclasses
from typing import Any


@dataclasses.dataclass(eq=False)
class _PooledCamoufox:
    cm: Any
    browser: Any
    created_at: float
    uses: int = 0
