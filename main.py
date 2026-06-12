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


from z3 import *

if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "/Users/luckyfur/Downloads/swerve_sim1.json"
    bound = int(sys.argv[2]) if len(sys.argv) > 2 else 25

    if "--bound" in sys.argv:
        idx = sys.argv.index("--bound")
        bound = int(sys.argv[idx + 1])

    print(f"Loading: {json_path}")
    ts = from_json(json_path)
    run_standard_checks(ts, bound=bound)
