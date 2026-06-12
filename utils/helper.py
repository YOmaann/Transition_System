from typing import Any
import math

def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)


def _safe(name: str) -> str:
    return name.replace(".", "_").replace("[", "_").replace("]", "")