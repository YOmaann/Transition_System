from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import dataclass, field
from enum import Enum
from itertools import product
from typing import Any, Callable, Literal, Sequence
from trace import TraceOptions, build_trace
from profiles import VarProfile, TraceProfile
from utils.helper import _is_nan, _safe
from generic_ts import GenericTransitionSystem
from pipeline import from_json, run_standard_checks

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "./example/swerve/swerve_sim1.json"
    bound = int(sys.argv[2]) if len(sys.argv) > 2 else 25


    # bound on the length of traces to check
    if "--bound" in sys.argv:
        idx = sys.argv.index("--bound")
        bound = int(sys.argv[idx + 1])

    print(f"Loading: {json_path}")
    ts = from_json(json_path)
    # run_standard_checks(ts, bound=bound)
    ts.to_smtlib("output.smt2", bound = 1)

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
