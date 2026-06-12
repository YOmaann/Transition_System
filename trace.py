from __future__ import annotations
from enum import Enum
import math
from dataclasses import dataclass
from itertools import product
from typing import Any, Sequence, Literal

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


def _block_starts(trace: list[dict[str, Any]]) -> list[int]:
    if not trace:
        return []
    starts = [0]
    for i in range(1, len(trace)):
        if trace[i].get("timestamp") != trace[i - 1].get("timestamp"):
            starts.append(i)
    return starts