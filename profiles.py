from dataclasses import dataclass, field
from typing import Any
from trace import _block_starts
import math
import statistics
from utils.helper import _is_nan


# profile of a single variable across a trace
# includes min/max, initial value, type of variable, q25/q75 etc.
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
    values: tuple = field(default_factory=tuple)


# profile of an entire trace
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

    # return variable names sorted alphabetically
    def variable_names(self) -> list[str]:
        return sorted(self.variables.keys())


# profile a trace using statistics.
def profile_trace(trace: list[dict[str, Any]], margin_pct: float = 0.15) -> TraceProfile:
    if not trace:
        raise ValueError("Empty trace")

    keys: set[str] = set() # gather all variable names across the trace
    for state in trace:
        keys.update(k for k in state if k != "timestamp") # enumeratre all keys except timestamp

    block_starts = _block_starts(trace) # contains blocks of timestamps. 
    block_reps = [trace[i] for i in block_starts] # get unique states
    prof = TraceProfile(num_steps=len(trace), margin_pct=margin_pct,
                        block_boundaries=block_starts) # create profile

    times = [s["timestamp"] for s in trace if "timestamp" in s] # gather timetamps from each state in the trace (does not ignore dublicates)
    if times:
        # compute time based stats 
        # like min/max time, initial time, and deltas between states.
        prof.time_min = min(times)
        prof.time_max = max(times)
        prof.time_initial = times[0]
        rep_times = [trace[i]["timestamp"] for i in block_starts if "timestamp" in trace[i]] # get timestamps for unique states (ignoring dublicates)
        time_deltas = [rep_times[i + 1] - rep_times[i] for i in range(len(rep_times) - 1)] # calculate intermediate deltas b/w states.

        # profile deltas
        if time_deltas:
            prof.time_min_delta = min(time_deltas)
            prof.time_max_delta = max(time_deltas)


    # profile keys across the trace. skip string vars.
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

        # list lists are flattened.
        def per_element(v: Any):
            if isinstance(v, tuple):
                for item in v:
                    yield from per_element(item)
            elif not _is_nan(v):
                yield float(v) 

        for state in trace:
            if name not in state:
                per_state.append(None) # add None for missing keys in a state.
                continue
            v = state[name]
            # if the variable contains a list. Recursively profile the list elements ?
            if isinstance(v, tuple):
                is_list = True
                len_min = min(len_min, len(v))
                len_max = max(len_max, len(v))
                elements.extend(per_element(v))
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

        # Profile the list type variable.
        unique = set(elements) # unique values for the variable across the trace.
        vp = VarProfile(name=name)
        vp.initial = next((v for v in per_state if v is not None), 0.0) # first value that is not None
        vp.min_val = min(elements)
        vp.max_val = max(elements)
        vp.unique_values = len(unique)
        vp.is_boolean = (not is_list) and unique <= {0.0, 1.0} # boolean if only takes values b. like doing subsumption check for bool vars.
        vp.is_constant = len(unique) == 1 # checks if the variable is contstant : can ignore them ?
        vp.mean = statistics.mean(elements)
        vp.presence_count = present # the number of states where this variable is present.
        vp.is_list = is_list
        vp.list_len = int(len_max) if is_list else 0

        sorted_vals = sorted(elements)
        n = len(sorted_vals)

        # compute 25th and 75th percentiles for the variable values across the trace.
        vp.q25 = sorted_vals[n // 4]
        vp.q75 = sorted_vals[3 * n // 4]

        # if the variable is not a list and has multiple unique state (not a constant), compute deltas b/w states and check if it is increasing/decreasing/monotone. This can be useful for checking monotonicity properties later.
        if not is_list and len(block_reps) > 1:
            rep_vals = [] # values between unique states (ignoring dublicates).
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
                # profile deltas
                vp.min_delta = min(deltas)
                vp.max_delta = max(deltas)
                vp.is_monotone_inc = all(d >= 0 for d in deltas)
                vp.is_monotone_dec = all(d <= 0 for d in deltas)

        # storew the concrete values.
        vp.values = tuple(
            x for v in per_state if v is not None for x in per_element(v)
        )

        prof.variables[name] = vp  

    return prof
