import json

from generic_ts import GenericTransitionSystem
from trace import TraceOptions, build_trace
from profiles import profile_trace


def from_json(path: str, opts: TraceOptions | None = None) -> GenericTransitionSystem:
    if opts is None:
        opts = TraceOptions()
    with open(path) as f:
        data = json.load(f)
    trace = build_trace(data, opts)
    profile = profile_trace(trace)
    return GenericTransitionSystem(profile, trace=trace, model_time=True)

