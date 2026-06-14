from typing import Any
import math

# returns if x is a NaN value.
def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)

# sanitize varibale names.
def _safe(name: str) -> str:
    return name.replace(".", "_").replace("[", "_").replace("]", "")