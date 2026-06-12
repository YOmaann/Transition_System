from dataclasses import dataclass, field
from typing import Any
from trace import _block_starts
import math
import statistics
from utils.helper import _is_nan

@dataclass
class VarProfile:
    name: str
    min_val: float = float("inf")
    max_val: float = float("-inf")
    initial: Any = 0.0
    min_delta: float = float("inf")
    max_delta: float = float("-inf")
    is_boolean: bool = False
    is_constant: bool = False
    is_monotone_inc: bool = False
    is_monotone_dec: bool = False
    unique_values: int = 0
    mean: float = 0.0
    q25: float = 0.0
    q75: float = 0.0
    is_list: bool = False
    list_len: int = 0
    presence_count: int = 0


@dataclass
class TraceProfile:
    variables: dict[str, VarProfile] = field(default_factory=dict)
    num_steps: int = 0
    margin_pct: float = 0.15
    block_boundaries: list[int] = field(default_factory=list)
    time_min: float = 0.0
    time_max: float = 0.0
    time_initial: float = 0.0
    time_min_delta: float = 0.0
    time_max_delta: float = 0.0

    def variable_names(self) -> list[str]:
        return sorted(self.variables.keys())



def profile_trace(trace: list[dict[str, Any]], margin_pct: float = 0.15) -> TraceProfile:
    if not trace:
        raise ValueError("Empty trace")

    keys: set[str] = set()
    for state in trace:
        keys.update(k for k in state if k != "timestamp")

    block_starts = _block_starts(trace)
    block_reps = [trace[i] for i in block_starts]
    prof = TraceProfile(num_steps=len(trace), margin_pct=margin_pct,
                        block_boundaries=block_starts)

    times = [s["timestamp"] for s in trace if "timestamp" in s]
    if times:
        prof.time_min = min(times)
        prof.time_max = max(times)
        prof.time_initial = times[0]
        rep_times = [trace[i]["timestamp"] for i in block_starts if "timestamp" in trace[i]]
        time_deltas = [rep_times[i + 1] - rep_times[i] for i in range(len(rep_times) - 1)]
        if time_deltas:
            prof.time_min_delta = min(time_deltas)
            prof.time_max_delta = max(time_deltas)

    for name in sorted(keys):
        first_val = next((s[name] for s in trace if name in s), None)
        if isinstance(first_val, str):
            continue

        is_list = False
        len_min = math.inf
        len_max = 0
        elements: list[float] = []
        per_state: list[Any] = []
        present = 0

        for state in trace:
            if name not in state:
                per_state.append(None)
                continue
            v = state[name]
            if isinstance(v, tuple):
                is_list = True
                len_min = min(len_min, len(v))
                len_max = max(len_max, len(v))
                elements.extend(x for x in v if not _is_nan(x))
                per_state.append(v)
                present += 1
            elif _is_nan(v):
                per_state.append(None)
            else:
                fv = float(v)
                elements.append(fv)
                per_state.append(fv)
                present += 1

        if not elements:
            continue
        if is_list and len_min != len_max:
            continue

        unique = set(elements)
        vp = VarProfile(name=name)
        vp.initial = next((v for v in per_state if v is not None), 0.0)
        vp.min_val = min(elements)
        vp.max_val = max(elements)
        vp.unique_values = len(unique)
        vp.is_boolean = (not is_list) and unique <= {0.0, 1.0}
        vp.is_constant = len(unique) == 1
        vp.mean = statistics.mean(elements)
        vp.presence_count = present
        vp.is_list = is_list
        vp.list_len = int(len_max) if is_list else 0

        sorted_vals = sorted(elements)
        n = len(sorted_vals)
        vp.q25 = sorted_vals[n // 4]
        vp.q75 = sorted_vals[3 * n // 4]

        if not is_list and len(block_reps) > 1:
            rep_vals = []
            for s in block_reps:
                v = s.get(name)
                if v is None or _is_nan(v):
                    rep_vals.append(None)
                else:
                    rep_vals.append(float(v))
            deltas = [rep_vals[i + 1] - rep_vals[i]
                      for i in range(len(rep_vals) - 1)
                      if rep_vals[i] is not None and rep_vals[i + 1] is not None]
            if deltas:
                vp.min_delta = min(deltas)
                vp.max_delta = max(deltas)
                vp.is_monotone_inc = all(d >= 0 for d in deltas)
                vp.is_monotone_dec = all(d <= 0 for d in deltas)

        prof.variables[name] = vp

    return prof
