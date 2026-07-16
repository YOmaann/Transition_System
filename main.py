from __future__ import annotations

import argparse
from fractions import Fraction

from trace import ArrayMode, TraceOptions
from pipeline import from_json, run_standard_checks
from pysmt.shortcuts import LE, Real


def _real(value: float | int):
    return Real(Fraction(str(value)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Convert a generic JSON trace into a transition system, run checks, "
            "and export the symbolic path to SMT-LIB."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="./example/test.json",
        help="Path to the input JSON trace file (default: ./example/test.json).",
    )
    parser.add_argument(
        "-b", "--bound",
        type=int,
        default=None,
        help="Bound on the length of traces to check (default: full trace length).",
    )
    parser.add_argument(
        "-o", "--output",
        default="output.smt2",
        help="Path to write the SMT-LIB output (default: output.smt2).",
    )
    parser.add_argument(
        "--no-concrete",
        dest="concrete",
        action="store_false",
        help="Emit symbolic (over-approximated) constraints instead of binding "
             "concrete observed values to states.",
    )
    parser.add_argument(
        "--array-mode",
        choices=[mode.value for mode in ArrayMode],
        default=ArrayMode.TRUNCATE.value,
        help="How to flatten arrays in the trace (default: truncate).",
    )
    parser.add_argument(
        "--max-list-items",
        type=int,
        default=0,
        help="Max number of items to keep when flattening lists (0 = unbounded, keep all; default: 0).",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=0.15,
        help="Fractional margin added to observed variable ranges (default: 0.15).",
    )
    parser.add_argument(
        "--standard-checks",
        action="store_true",
        help="Run the built-in standard checks (safety, monotonicity, liveness, "
             "reachability) instead of the example speed check.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    opts = TraceOptions(
        array_mode=ArrayMode(args.array_mode),
        max_list_items=args.max_list_items,
    )

    print(f"Loading: {args.path}")
    ts = from_json(args.path, opts=opts, margin_pct=args.margin)

    bound = args.bound if args.bound is not None else ts.profile.num_steps - 1

    if args.standard_checks:
        run_standard_checks(ts, bound=bound)
    # else:
        # speed_var = "ego_vehicle_kinematicks.ego_vehicle_kinematicks.x"
        # limit = 100.0
        # ts.check(
        #     f"G({ts._short_name(speed_var)} <= {limit})",
        #     lambda p, n=speed_var, hi=limit: ts.ltl_G(
        #         p, lambda s, n=n, hi=hi: LE(s[n], _real(hi))
        #     ),
        #     args.bound,
        # )

    ts.to_smtlib(args.output, bound=bound, concrete=args.concrete)
    print(f"done: wrote {args.output}")


if __name__ == "__main__":
    main()

# CVC
# s = x_0, x_1 ,....
# And(x_0 > 0, x_1 > 0...) - enough
# \forall i, x_[i] > 0
# Array (for comparison purpose)
# z3 : sort - type of types.

# one step transition (over approximation)
# 0 1 0 2
# 1 2 3 4
# 0 2 (counterexample)

# T = (x = 0 and x' = 1) or (x = 1 and x' = 2)
# using timestamps.
# T = (t = 1 and x = 0 and x' = 1 and t' = 1) or (t = 1 and x = 1 and x' = 2 and t' = 2)
# does it speed up verification ? 
# x' - x > 10
# \exists i, t = i and t' = i + 1 -> cannot check for path. 
# comparison.
# look for encoding variations.

# documentation - high level (todo/ slides)
# workflow (diagram/walkthrough)
# toolchains (PySMT, z3 briefly)

# concrete examples
# precision of the perceived bus (predicted path)