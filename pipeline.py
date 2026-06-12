from generic_ts import GenericTransitionSystem
from trace import TraceOptions, build_trace
from profiles import profile_trace
import json
from z3 import *


# load a json file and build a transition system with profiles
def from_json(path: str, opts: TraceOptions | None = None,
              margin_pct: float = 0.15) -> GenericTransitionSystem:
    if opts is None:
        opts = TraceOptions()
    with open(path) as f:
        data = json.load(f)
    trace = build_trace(data, opts)
    profile = profile_trace(trace, margin_pct=margin_pct)
    return GenericTransitionSystem(profile)



# Run standard checks
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
