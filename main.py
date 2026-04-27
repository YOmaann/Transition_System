from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from enum import Enum
from itertools import product
from typing import Any, Callable, Literal, Sequence

from z3 import (
    And, BoolVal, Implies, Not, Or, Real, RealVal,
    Solver, sat, unsat,
)


class ArrayMode(Enum):
    TRUNCATE = "truncate"
    CROSSPRODUCT = "crossproduct"
    INTACT = "intact"


@dataclass
class TraceOptions:
    array_mode: ArrayMode = ArrayMode.TRUNCATE
    max_list_items: int = 2
    tolerance: float = float("inf")
    timestamps: Sequence[float] | None = None
    missing_policy: Literal["drop", "nan"] = "drop"

    def __post_init__(self):
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        if self.missing_policy not in ("drop", "nan"):
            raise ValueError(f"unknown missing_policy: {self.missing_policy}")


def discover_time_series(data: dict) -> dict[str, list[dict]]:
    series: dict[str, list[dict]] = {}
    for key, val in data.items():
        if (isinstance(val, list)
                and len(val) > 0
                and isinstance(val[0], dict)
                and "timestamp" in val[0]):
            series[key] = val
    return series


def nearest_by_time(entries: list[dict], t: float,
                    tolerance: float = float("inf")) -> dict | None:
    if not entries:
        return None
    lo, hi = 0, len(entries) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if entries[mid]["timestamp"] < t:
            lo = mid + 1
        else:
            hi = mid
    best = lo
    if lo > 0 and abs(entries[lo - 1]["timestamp"] - t) < abs(entries[lo]["timestamp"] - t):
        best = lo - 1
    if abs(entries[best]["timestamp"] - t) > tolerance:
        return None
    return entries[best]


def _split_numeric_string(s: str) -> list[float] | None:
    parts = s.split()
    if len(parts) > 1 and all(p.lstrip('-').replace('.', '', 1).isdigit() for p in parts):
        return [float(p) for p in parts]
    return None


def _flatten_truncate(obj: Any, prefix: str, max_list_items: int) -> dict[str, float | str]:
    result: dict[str, float | str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "timestamp":
                continue
            key = f"{prefix}.{k}" if prefix else k
            result.update(_flatten_truncate(v, key, max_list_items))
    elif isinstance(obj, list):
        items = obj[:max_list_items] if max_list_items else obj
        for i, item in enumerate(items):
            result.update(_flatten_truncate(item, f"{prefix}[{i}]", max_list_items))
    elif isinstance(obj, bool):
        result[prefix] = 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)):
        result[prefix] = float(obj)
    elif isinstance(obj, str) and obj:
        nums = _split_numeric_string(obj)
        if nums is not None:
            for i, x in enumerate(nums):
                result[f"{prefix}[{i}]"] = x
        else:
            result[prefix] = obj
    return result


def _flatten_crossproduct(obj: Any, prefix: str) -> list[dict[str, float | str]]:
    if isinstance(obj, dict):
        results: list[dict[str, float | str]] = [{}]
        for k, v in obj.items():
            if k == "timestamp":
                continue
            key = f"{prefix}.{k}" if prefix else k
            child = _flatten_crossproduct(v, key)
            results = [{**r, **c} for r in results for c in child]
        return results
    if isinstance(obj, list):
        if not obj:
            return [{}]
        variants: list[dict[str, float | str]] = []
        for item in obj:
            variants.extend(_flatten_crossproduct(item, prefix))
        return variants
    if isinstance(obj, bool):
        return [{prefix: 1.0 if obj else 0.0}]
    if isinstance(obj, (int, float)):
        return [{prefix: float(obj)}]
    if isinstance(obj, str) and obj:
        nums = _split_numeric_string(obj)
        if nums is not None:
            return [{f"{prefix}[{i}]": x for i, x in enumerate(nums)}]
        return [{prefix: obj}]
    return [{}]


def _flatten_intact(obj: Any, prefix: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "timestamp":
                continue
            key = f"{prefix}.{k}" if prefix else k
            result.update(_flatten_intact(v, key))
    elif isinstance(obj, list):
        if not obj:
            return result
        if all(isinstance(x, (int, float, bool)) for x in obj):
            result[prefix] = tuple(float(x) for x in obj)
        else:
            child_dicts = [_flatten_intact(item, "") for item in obj]
            keys: set[str] = set()
            for d in child_dicts:
                keys.update(d.keys())
            for k in keys:
                tup = tuple(d.get(k, float("nan")) for d in child_dicts)
                joined = f"{prefix}.{k}" if k else prefix
                if all(isinstance(x, (int, float)) for x in tup):
                    result[joined] = tup
                else:
                    for i, x in enumerate(tup):
                        result[f"{joined}[{i}]"] = x
    elif isinstance(obj, bool):
        result[prefix] = 1.0 if obj else 0.0
    elif isinstance(obj, (int, float)):
        result[prefix] = float(obj)
    elif isinstance(obj, str) and obj:
        nums = _split_numeric_string(obj)
        if nums is not None:
            result[prefix] = tuple(nums)
        else:
            result[prefix] = obj
    return result


def flatten_record(obj: Any, prefix: str = "",
                   opts: TraceOptions | None = None) -> list[dict[str, Any]]:
    if opts is None:
        opts = TraceOptions()
    if opts.array_mode is ArrayMode.TRUNCATE:
        return [_flatten_truncate(obj, prefix, opts.max_list_items)]
    if opts.array_mode is ArrayMode.CROSSPRODUCT:
        return _flatten_crossproduct(obj, prefix)
    if opts.array_mode is ArrayMode.INTACT:
        return [_flatten_intact(obj, prefix)]
    raise ValueError(f"unknown array_mode: {opts.array_mode}")


def _metadata_dict(data: dict, opts: TraceOptions) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, dict):
            for d in flatten_record(val, prefix=f"meta.{key}", opts=opts):
                meta.update(d)
    return meta


def _default_timestamps(data: dict) -> list[float]:
    series = discover_time_series(data)
    if not series:
        return []
    primary_key = max(series, key=lambda k: len(series[k]))
    return [r["timestamp"] for r in series[primary_key]]


def states_at_one(data: dict, t: float,
                  opts: TraceOptions | None = None) -> list[dict[str, Any]]:
    if opts is None:
        opts = TraceOptions()
    series = discover_time_series(data)
    metadata = _metadata_dict(data, opts)

    per_stream: list[list[dict[str, Any]]] = []
    for stream_name, entries in series.items():
        nearest = nearest_by_time(entries, t, opts.tolerance)
        if nearest is None:
            per_stream.append([{}])
            continue
        per_stream.append(flatten_record(nearest, prefix=stream_name, opts=opts))

    states: list[dict[str, Any]] = []
    for combo in product(*per_stream) if per_stream else [()]:
        s: dict[str, Any] = {"timestamp": t}
        s.update(metadata)
        for d in combo:
            s.update(d)
        states.append(s)
    return states


def states_at(data: dict, timestamps: Sequence[float],
              opts: TraceOptions | None = None) -> list[dict[str, Any]]:
    if opts is None:
        opts = TraceOptions()
    trace: list[dict[str, Any]] = []
    for t in timestamps:
        trace.extend(states_at_one(data, t, opts))
    return trace


def build_trace(data: dict, opts: TraceOptions | None = None) -> list[dict[str, Any]]:
    if opts is None:
        opts = TraceOptions()
    timestamps = opts.timestamps if opts.timestamps is not None else _default_timestamps(data)
    return states_at(data, timestamps, opts)


def extract_flat_trace(data: dict) -> list[dict[str, Any]]:
    return build_trace(data, TraceOptions())


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

    def variable_names(self) -> list[str]:
        return sorted(self.variables.keys())


def _is_nan(x: Any) -> bool:
    return isinstance(x, float) and math.isnan(x)


def _block_starts(trace: list[dict[str, Any]]) -> list[int]:
    if not trace:
        return []
    starts = [0]
    for i in range(1, len(trace)):
        if trace[i].get("timestamp") != trace[i - 1].get("timestamp"):
            starts.append(i)
    return starts


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


StateFn = Callable[[dict], Any]


def _safe(name: str) -> str:
    return name.replace(".", "_").replace("[", "_").replace("]", "")


class GenericTransitionSystem:

    def __init__(self, profile: TraceProfile, skip_constant: bool = True,
                 exact_initial: bool = False, use_invariants: bool = True):
        self.profile = profile
        self.exact_initial = exact_initial
        self.use_invariants = use_invariants
        if skip_constant:
            self.var_names = [n for n in profile.variable_names()
                              if not profile.variables[n].is_constant]
        else:
            self.var_names = profile.variable_names()
        self.constants: dict[str, float] = {
            n: float(vp.initial) for n, vp in profile.variables.items()
            if vp.is_constant and not vp.is_list
        }
        self.propositions: dict[str, StateFn] = {}
        self._custom_invariants: list[StateFn] = []
        self._build_propositions()

    def make_state(self, t: int) -> dict[str, Any]:
        state: dict[str, Any] = {}
        for name in self.var_names:
            vp = self.profile.variables[name]
            base = _safe(name)
            if vp.is_list:
                state[name] = tuple(Real(f"{base}_{i}_{t}") for i in range(vp.list_len))
            else:
                state[name] = Real(f"{base}_{t}")
        for name, val in self.constants.items():
            state[name] = RealVal(val)
        return state

    def symbolic_states_at(self, timestamps: Sequence[float]) -> list[dict[str, Any]]:
        path = []
        for i, t in enumerate(timestamps):
            s = self.make_state(i)
            s["_t"] = t
            path.append(s)
        return path

    def initial_constraints(self, s0: dict) -> list:
        constraints = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            if vp.is_list:
                init = vp.initial if isinstance(vp.initial, tuple) else (float(vp.initial),) * vp.list_len
                for i, x in enumerate(s0[name]):
                    if self.exact_initial:
                        constraints.append(x == float(init[i]))
                    else:
                        margin = self._margin(vp)
                        constraints.append(x >= float(init[i]) - margin)
                        constraints.append(x <= float(init[i]) + margin)
            elif self.exact_initial or vp.is_boolean:
                constraints.append(s0[name] == float(vp.initial))
            else:
                margin = self._margin(vp)
                constraints.append(s0[name] >= float(vp.initial) - margin)
                constraints.append(s0[name] <= float(vp.initial) + margin)
        return constraints

    def invariant_constraints(self, s: dict) -> list:
        constraints = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            xs = s[name] if vp.is_list else (s[name],)
            for x in xs:
                if vp.is_boolean:
                    constraints.append(Or(x == 0.0, x == 1.0))
                elif self.use_invariants:
                    margin = self._margin(vp)
                    constraints.append(x >= vp.min_val - margin)
                    constraints.append(x <= vp.max_val + margin)
        for inv_fn in self._custom_invariants:
            constraints.append(inv_fn(s))
        return constraints

    def add_invariant(self, fn: StateFn):
        self._custom_invariants.append(fn)

    def transition_constraints(self, s_curr: dict, s_next: dict) -> list:
        constraints = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            if vp.is_constant or vp.is_list:
                continue
            delta = s_next[name] - s_curr[name]
            if vp.is_monotone_inc:
                constraints.append(delta >= 0)
            if vp.is_monotone_dec:
                constraints.append(delta <= 0)
        return constraints

    def _margin(self, vp: VarProfile) -> float:
        return max((vp.max_val - vp.min_val) * self.profile.margin_pct, 0.01)

    def make_path(self, k: int) -> list[dict]:
        return [self.make_state(t) for t in range(k + 1)]

    def path_constraints(self, path: list[dict]) -> list:
        c = []
        c.extend(self.initial_constraints(path[0]))
        for s in path:
            c.extend(self.invariant_constraints(s))
        for i in range(len(path) - 1):
            c.extend(self.transition_constraints(path[i], path[i + 1]))
        return c

    def _build_propositions(self):
        for name, vp in self.profile.variables.items():
            if vp.is_constant:
                continue
            base = _safe(name)
            if vp.is_list:
                if vp.is_boolean:
                    self.propositions[f"{base}_any_true"] = (
                        lambda s, n=name: Or(*[x >= 0.5 for x in s[n]]))
                    self.propositions[f"{base}_all_true"] = (
                        lambda s, n=name: And(*[x >= 0.5 for x in s[n]]))
                else:
                    self.propositions[f"{base}_any_high"] = (
                        lambda s, n=name, th=vp.q75: Or(*[x > th for x in s[n]]))
                    self.propositions[f"{base}_all_low"] = (
                        lambda s, n=name, th=vp.q25: And(*[x < th for x in s[n]]))
                    if vp.min_val <= 0 <= vp.max_val:
                        eps = max((vp.max_val - vp.min_val) * 0.05, 0.01)
                        self.propositions[f"{base}_any_near_zero"] = (
                            lambda s, n=name, e=eps: Or(*[And(x >= -e, x <= e) for x in s[n]]))
            elif vp.is_boolean:
                self.propositions[f"{base}_true"] = lambda s, n=name: s[n] >= 0.5
                self.propositions[f"{base}_false"] = lambda s, n=name: s[n] < 0.5
            else:
                self.propositions[f"{base}_high"] = (
                    lambda s, n=name, th=vp.q75: s[n] > th)
                self.propositions[f"{base}_low"] = (
                    lambda s, n=name, th=vp.q25: s[n] < th)
                if vp.min_val <= 0 <= vp.max_val:
                    eps = max((vp.max_val - vp.min_val) * 0.05, 0.01)
                    self.propositions[f"{base}_near_zero"] = (
                        lambda s, n=name, e=eps: And(s[n] >= -e, s[n] <= e))

    def get_prop(self, prop_name: str) -> StateFn:
        if prop_name not in self.propositions:
            available = sorted(self.propositions.keys())
            raise KeyError(
                f"Unknown proposition '{prop_name}'. "
                f"Available: {available[:20]}{'...' if len(available) > 20 else ''}"
            )
        return self.propositions[prop_name]

    def add_proposition(self, name: str, fn: StateFn):
        self.propositions[name] = fn

    @staticmethod
    def exists_element(s: dict, name: str, pred: Callable[[Any], Any]):
        return Or(*[pred(x) for x in s[name]])

    @staticmethod
    def forall_element(s: dict, name: str, pred: Callable[[Any], Any]):
        return And(*[pred(x) for x in s[name]])

    @staticmethod
    def ltl_G(path: list[dict], prop_fn: StateFn):
        return And(*[prop_fn(s) for s in path])

    @staticmethod
    def ltl_F(path: list[dict], prop_fn: StateFn):
        return Or(*[prop_fn(s) for s in path])

    @staticmethod
    def ltl_G_imp(path: list[dict], p_fn: StateFn, q_fn: StateFn):
        return And(*[Implies(p_fn(s), q_fn(s)) for s in path])

    @staticmethod
    def ltl_G_imp_F(path: list[dict], trigger_fn: StateFn, target_fn: StateFn,
                    horizon: int | None = None):
        k = len(path)
        h = horizon or k
        clauses = []
        for i in range(k):
            future = Or(*[target_fn(path[j]) for j in range(i, min(i + h, k))])
            clauses.append(Implies(trigger_fn(path[i]), future))
        return And(*clauses)

    @staticmethod
    def ltl_neg_U(path: list[dict], forbidden_fn: StateFn, trigger_fn: StateFn):
        clauses = []
        for j in range(len(path)):
            q_at_j = trigger_fn(path[j])
            p_false_before = (And(*[Not(forbidden_fn(path[i])) for i in range(j)])
                              if j > 0 else BoolVal(True))
            clauses.append(And(q_at_j, p_false_before))
        return Or(*clauses)

    def check(self, name: str, phi_fn: Callable, bound: int = 25,
              extra_constraints: Callable | None = None, timeout_ms: int = 30000):
        path = self.make_path(bound)
        solver = Solver()
        solver.set("timeout", timeout_ms)
        solver.add(self.path_constraints(path))
        if extra_constraints:
            solver.add(extra_constraints(path))

        phi = phi_fn(path)
        solver.add(Not(phi))

        result = solver.check()
        if result == unsat:
            print(f"  HOLDS  :: {name}  (bound={bound})")
        elif result == sat:
            m = solver.model()
            print(f"  FAILS  :: {name}  -- counterexample:")
            self._print_trace(m, path)
        else:
            print(f"  ?????  :: {name}  -- timeout / unknown")
        print()
        return result

    def check_reachable(self, name: str, target_fn: Callable, bound: int = 25,
                        timeout_ms: int = 30000):
        path = self.make_path(bound)
        solver = Solver()
        solver.set("timeout", timeout_ms)
        solver.add(self.path_constraints(path))
        solver.add(target_fn(path))

        result = solver.check()
        if result == sat:
            m = solver.model()
            print(f"  REACHABLE :: {name}")
            self._print_trace(m, path)
        elif result == unsat:
            print(f"  UNREACHABLE :: {name}  (bound={bound})")
        else:
            print(f"  ?????  :: {name}  -- timeout")
        print()
        return result

    def _print_trace(self, model, path: list[dict], max_vars_shown: int = 8):
        display_vars = self._pick_display_vars(max_vars_shown)

        header = f"  {'t':>3}"
        for v in display_vars:
            header += f" {self._short_name(v):>10}"
        header += "  propositions"
        print(header)

        show = set(range(min(8, len(path))))
        show |= set(range(max(0, len(path) - 5), len(path)))
        skipped = False

        for i, s in enumerate(path):
            if i not in show:
                if not skipped:
                    print(f"       ... ({len(path) - 13} steps omitted)")
                    skipped = True
                continue

            vals = self._eval_state(model, s)
            active = self._get_active_props(vals)

            row = f"  {i:>3}"
            for v in display_vars:
                val = vals.get(v)
                if isinstance(val, tuple):
                    row += f" {'|'.join(f'{x:.2f}' for x in val):>10}"
                elif isinstance(val, float):
                    row += f" {val:>10.3f}"
                else:
                    row += f" {str(val):>10}"
            row += f"  {{{', '.join(active)}}}"
            print(row)

    def _eval_one(self, model, var) -> float | str:
        v = model.evaluate(var, model_completion=True)
        try:
            if hasattr(v, "as_decimal"):
                return float(v.as_decimal(4).rstrip("?"))
            return float(str(v))
        except (ValueError, ArithmeticError):
            return str(v)

    def _eval_state(self, model, s: dict) -> dict[str, Any]:
        vals: dict[str, Any] = {}
        for key, var in s.items():
            if key == "_t":
                vals[key] = var
            elif isinstance(var, tuple):
                vals[key] = tuple(self._eval_one(model, x) for x in var)
            else:
                vals[key] = self._eval_one(model, var)
        return vals

    def _get_active_props(self, vals: dict) -> list[str]:
        active = []
        for name, vp in sorted(self.profile.variables.items()):
            if vp.is_constant:
                continue
            val = vals.get(name)
            short = self._short_name(name)
            if vp.is_list and isinstance(val, tuple):
                if vp.is_boolean:
                    if any(x >= 0.5 for x in val):
                        active.append(f"{short}:any")
                else:
                    if any(x > vp.q75 for x in val):
                        active.append(f"{short}:hi")
                    elif all(x < vp.q25 for x in val):
                        active.append(f"{short}:lo")
                continue
            if not isinstance(val, float):
                continue
            if vp.is_boolean:
                if val >= 0.5:
                    active.append(short)
            elif val > vp.q75:
                active.append(f"{short}:hi")
            elif val < vp.q25:
                active.append(f"{short}:lo")
        return active

    def _pick_display_vars(self, n: int) -> list[str]:
        scored = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            if vp.is_constant:
                continue
            scored.append((vp.max_val - vp.min_val, name))
        scored.sort(reverse=True)
        seen: set[str] = set()
        result: list[str] = []
        for _, name in scored:
            short = self._short_name(name)
            if short not in seen:
                seen.add(short)
                result.append(name)
                if len(result) >= n:
                    break
        return result

    @staticmethod
    def _short_name(name: str) -> str:
        parts = name.replace("[0]", "").split(".")
        meaningful = [p for p in parts if p not in ("0",)]
        if len(meaningful) <= 2:
            return ".".join(meaningful)
        stream = meaningful[0].split("_")[0]
        return f"{stream}.{'.'.join(meaningful[-2:])}"

    def summary(self):
        print("=" * 70)
        print("Generic Z3 Transition System")
        print("=" * 70)
        print(f"  State variables   : {len(self.var_names)}")
        print(f"  Constants         : {len(self.constants)}")
        print(f"  Propositions      : {len(self.propositions)}")
        print(f"  Trace length used : {self.profile.num_steps}")
        print(f"  Blocks            : {len(self.profile.block_boundaries)}")
        print()

        bools = [n for n in self.var_names if self.profile.variables[n].is_boolean]
        monos = [n for n in self.var_names
                 if self.profile.variables[n].is_monotone_inc
                 or self.profile.variables[n].is_monotone_dec]
        lists = [n for n in self.var_names if self.profile.variables[n].is_list]

        if bools:
            print(f"  Boolean variables : {', '.join(self._short_name(b) for b in bools)}")
        if monos:
            print(f"  Monotone variables: {', '.join(self._short_name(m) for m in monos)}")
        if lists:
            print(f"  List variables    : {', '.join(self._short_name(l) for l in lists)}")
        print()

        print("  Variable profiles (top 15 by range):")
        display = self._pick_display_vars(15)
        print(f"    {'name':<35} {'min':>10} {'max':>10} {'dmin':>10} {'dmax':>10} {'init':>10}")
        for name in display:
            vp = self.profile.variables[name]
            short = self._short_name(name)
            init_disp = vp.initial if not isinstance(vp.initial, tuple) else float(vp.initial[0])
            print(f"    {short:<35} {vp.min_val:>10.3f} {vp.max_val:>10.3f} "
                  f"{vp.min_delta:>10.4f} {vp.max_delta:>10.4f} {float(init_disp):>10.3f}")
        print()


def from_json(path: str, opts: TraceOptions | None = None,
              margin_pct: float = 0.15) -> GenericTransitionSystem:
    if opts is None:
        opts = TraceOptions()
    with open(path) as f:
        data = json.load(f)
    trace = build_trace(data, opts)
    profile = profile_trace(trace, margin_pct=margin_pct)
    return GenericTransitionSystem(profile)


def run_standard_checks(ts: GenericTransitionSystem, bound: int = 25):
    ts.summary()

    print("=" * 70)
    print("SAFETY -- invariant bounds hold for all variables")
    print("=" * 70)

    for name in ts._pick_display_vars(5):
        vp = ts.profile.variables[name]
        if vp.is_list:
            continue
        short = ts._short_name(name)
        ts.check(
            f"G({short} in [{vp.min_val:.1f}, {vp.max_val:.1f}])",
            lambda p, n=name, lo=vp.min_val, hi=vp.max_val:
                ts.ltl_G(p, lambda s, n=n, lo=lo, hi=hi:
                         And(s[n] >= lo, s[n] <= hi)),
            bound,
        )

    print("=" * 70)
    print("MONOTONICITY -- variables that only increase/decrease in data")
    print("=" * 70)
    for name in ts.var_names:
        vp = ts.profile.variables[name]
        if vp.is_constant or vp.is_list:
            continue
        short = ts._short_name(name)
        if vp.is_monotone_inc:
            ts.check(
                f"G({short}[t+1] >= {short}[t]) -- monotone increasing",
                lambda p, n=name: And(*[
                    p[i + 1][n] >= p[i][n] for i in range(len(p) - 1)
                ]),
                bound,
            )
        if vp.is_monotone_dec:
            ts.check(
                f"G({short}[t+1] <= {short}[t]) -- monotone decreasing",
                lambda p, n=name: And(*[
                    p[i + 1][n] <= p[i][n] for i in range(len(p) - 1)
                ]),
                bound,
            )

    print("=" * 70)
    print("LIVENESS -- boolean variables eventually become true")
    print("=" * 70)
    for name in ts.var_names:
        vp = ts.profile.variables[name]
        if vp.is_boolean and vp.max_val == 1.0 and not vp.is_list:
            short = ts._short_name(name)
            ts.check(
                f"F({short}) -- eventually true",
                lambda p, n=name: ts.ltl_F(p, lambda s, n=n: s[n] >= 0.5),
                bound,
            )

    print("=" * 70)
    print("REACHABILITY -- can extreme observed values be reached?")
    print("=" * 70)
    for name in ts._pick_display_vars(5):
        vp = ts.profile.variables[name]
        if vp.is_list:
            continue
        short = ts._short_name(name)
        ts.check_reachable(
            f"Can {short} reach its max ({vp.max_val:.2f})?",
            lambda p, n=name, mx=vp.max_val:
                Or(*[p[i][n] >= mx for i in range(len(p))]),
            bound,
        )


if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/luckyfur/Downloads/swerve_sim1.json"
    bound = int(sys.argv[2]) if len(sys.argv) > 2 else 25

    if "--bound" in sys.argv:
        idx = sys.argv.index("--bound")
        bound = int(sys.argv[idx + 1])

    print(f"Loading: {json_path}")
    ts = from_json(json_path)
    run_standard_checks(ts, bound=bound)
