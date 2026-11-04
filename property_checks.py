"""Concrete PRSS and perception-distance property encodings for main.py."""
from __future__ import annotations

from fractions import Fraction
import math
from pathlib import Path
import time


PRSS = {
    "rho": Fraction(3, 5),
    "alpha_max": Fraction(3),
    "beta_min": Fraction(4),
    "beta_max": Fraction(8),
    "gamma_max": Fraction(3, 2),
    "eps_max": Fraction(1),
    "delta": Fraction(3, 5),
}
EGO_SPEED = "ego_estimated_kinematic.twist.linear.x"
EGO_X = "ego_estimated_kinematic.pose.position.x"
LEAD_VX = "perception_objects.objects[0].twist.linear.x"
LEAD_VY = "perception_objects.objects[0].twist.linear.y"
LEAD_X = "perception_objects.objects[0].pose.position.x"
PERCEIVED_X = "perception_objects.objects[0].pose.position.x"
PERCEIVED_Y = "perception_objects.objects[0].pose.position.y"
GROUND_X = "groundtruth_kinematic.groundtruth_vehicles[0].pose.position.x"
GROUND_Y = "groundtruth_kinematic.groundtruth_vehicles[0].pose.position.y"


def _fraction(value) -> Fraction:
    return Fraction(str(value))


def smt_num(value: Fraction | int | float) -> str:
    value = Fraction(value)
    if value.denominator == 1:
        return str(value.numerator)
    if value.numerator < 0:
        return f"(- (/ {-value.numerator} {value.denominator}))"
    return f"(/ {value.numerator} {value.denominator})"


def _finite(row, keys):
    values = tuple(row.get(k) for k in keys)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return None
    return tuple(_fraction(v) for v in values)


def _prss_terms(trace, index):
    values = _finite(trace[index], (EGO_SPEED, EGO_X, LEAD_VX, LEAD_VY, LEAD_X))
    if values is None:
        return None
    ego_speed, ego_x, vx, vy, lead_x = values
    p = PRSS
    q = vx * vx + vy * vy
    k = (
        abs(lead_x - ego_x) - p["eps_max"]
        - ego_speed * p["rho"]
        - p["alpha_max"] * p["rho"] ** 2 / 2
        - (ego_speed + p["rho"] * p["alpha_max"]) ** 2 / (2 * p["beta_min"])
        - (ego_speed + p["gamma_max"]) * p["delta"]
        - (p["alpha_max"] + p["beta_max"]) * p["delta"] ** 2 / 2
    )
    return q, k


def _valid_prss(trace, index):
    return int(_prss_terms(trace, index) is not None)


def _q(trace, index):
    terms = _prss_terms(trace, index)
    return None if terms is None else terms[0]


def _k(trace, index):
    terms = _prss_terms(trace, index)
    return None if terms is None else terms[1]


def _distance_data(trace, index):
    row = trace[index]
    values = _finite(row, ("timestamp", PERCEIVED_X, PERCEIVED_Y))
    if values is None:
        return None
    return values


def _valid_distance(trace, index):
    return int(_distance_data(trace, index) is not None)


def _add_prss_derivations(ts):
    for name, fn in (("prss_valid", _valid_prss),
                     ("prss_v_squared", _q),
                     ("prss_margin_base", _k)):
        ts.add_derivation(name, fn)


def _prss_query(ts):
    _add_prss_derivations(ts)
    trace = ts.trace
    n = len(trace)
    validity = [ts.derivations["prss_valid"](trace, i) for i in range(n)]
    valid = [i for i, value in enumerate(validity) if value]
    p = PRSS
    threshold = p["gamma_max"] + p["beta_max"] * p["delta"]
    threshold_sq = threshold * threshold
    out = ["(set-logic ALL)",
           "(declare-fun prss_valid () (Array Int Int))",
           "(declare-fun prss_v_squared () (Array Int Real))",
           "(declare-fun prss_margin_base () (Array Int Real))"]
    failures = []
    violations = []
    margins = []
    for i, ok in enumerate(validity):
        out.append(f"(assert (= (select prss_valid {i}) {ok}))")
        if not ok:
            continue
        q, k = ts.derivations["prss_v_squared"](trace, i), ts.derivations["prss_margin_base"](trace, i)
        out.append(f"(assert (= (select prss_v_squared {i}) {smt_num(q)}))")
        out.append(f"(assert (= (select prss_margin_base {i}) {smt_num(k)}))")
        qexpr, kexpr = f"(select prss_v_squared {i})", f"(select prss_margin_base {i})"
        l = f"(+ (* {smt_num(2 * p['beta_max'])} {kexpr}) {qexpr} {smt_num(threshold_sq)})"
        safe = (
            f"(ite (<= {qexpr} {smt_num(threshold_sq)}) "
            f"(or (>= {kexpr} 0) (>= (* {smt_num(p['delta'] ** 2)} {qexpr}) (* {kexpr} {kexpr}))) "
            f"(and (>= {l} 0) (>= (* {l} {l}) (* {smt_num(4 * p['gamma_max'] ** 2)} {qexpr}))))"
        )
        failures.append(f"(not {safe})")
        margins.append(float(k) + float(p["delta"]) * math.sqrt(float(q))
                       + max(0.0, math.sqrt(float(q)) - float(threshold)) ** 2 / (2 * float(p["beta_max"])))
        is_safe = ((k >= 0 or p["delta"] ** 2 * q >= k * k) if q <= threshold_sq else
                   (lambda v: v >= 0 and v * v >= 4 * p["gamma_max"] ** 2 * q)(2 * p["beta_max"] * k + q + threshold_sq))
        if not is_safe:
            violations.append(i)
    out.append("(assert " + ("(or " + " ".join(failures) + ")" if failures else "false") + ")")
    out.append("(check-sat)")
    return "\n".join(out) + "\n", {
        "property": "PRSS longitudinal safety", "trace_steps": n,
        "checked_steps": len(valid), "missing_steps": n - len(valid),
        "violating_steps": len(violations),
        "first_violation": violations[0] if violations else "",
        "last_violation": violations[-1] if violations else "",
        "worst_margin_m": min(margins) if margins else "",
        "expected_query": "sat" if violations else "unsat",
    }


def _distance_query(ts):
    """Encode forall perceived timestamps, exists time-matched GT point within 2 m."""
    trace = ts.trace
    n = len(trace)
    ts.add_derivation("distance_valid", _valid_distance)
    validity = [ts.derivations["distance_valid"](trace, i) for i in range(n)]
    # Precompute temporal candidates with a sorted index, then leave every spatial
    # squared-distance comparison in SMT-LIB for both solvers to evaluate.
    gt = []
    for j, row in enumerate(trace):
        values = _finite(row, ("timestamp", GROUND_X, GROUND_Y))
        if values is not None:
            gt.append((values[0], j, values[1], values[2]))
    gt.sort(key=lambda item: item[0])
    times = [entry[0] for entry in gt]
    from bisect import bisect_left, bisect_right
    tolerance = Fraction(3, 50)
    eps_sq = Fraction(4)
    out = ["(set-logic ALL)", "(define-fun abs_real ((x Real)) Real (ite (>= x 0) x (- x)))"]
    failures = []
    checked = 0
    has_match = 0
    no_match = []
    for i, ok in enumerate(validity):
        if not ok:
            continue
        checked += 1
        tstamp, px, py = _distance_data(trace, i)
        lo = bisect_right(times, tstamp - tolerance)
        hi = bisect_left(times, tstamp + tolerance)
        candidates = gt[lo:hi]  # strict < 0.06 second, as in the source formula
        matches_python = False
        point_checks = []
        for stamp, j, gx, gy in candidates:
            dx, dy = px - gx, py - gy
            d2 = dx * dx + dy * dy
            matches_python |= d2 <= eps_sq
            point_checks.append(f"(<= (+ (* {smt_num(dx)} {smt_num(dx)}) (* {smt_num(dy)} {smt_num(dy)})) 4)")
        match_expr = "(or " + " ".join(point_checks) + ")" if point_checks else "false"
        out.append(f"(define-fun distance_match_{i} () Bool {match_expr})")
        failures.append(f"(not distance_match_{i})")
        if matches_python:
            has_match += 1
        else:
            no_match.append(i)
    out.append("(assert " + ("(or " + " ".join(failures) + ")" if failures else "false") + ")")
    out.append("(check-sat)")
    return "\n".join(out) + "\n", {
        "property": "Perception position error (distance)", "trace_steps": n,
        "checked_steps": checked, "missing_steps": n - checked,
        "time_candidate_pairs": sum(
            bisect_left(times, _distance_data(trace, i)[0] + tolerance)
            - bisect_right(times, _distance_data(trace, i)[0] - tolerance)
            for i, ok in enumerate(validity) if ok
        ),
        "matched_steps": has_match, "violating_steps": len(no_match),
        "first_violation": no_match[0] if no_match else "",
        "last_violation": no_match[-1] if no_match else "",
        "expected_query": "sat" if no_match else "unsat",
    }


def generate_property_files(ts, output_base: Path, selected: str):
    """Generate all-timestamp property queries; returns rows with timing/coverage."""
    output_base.parent.mkdir(parents=True, exist_ok=True)
    reports = []
    for key, writer in (("prss", _prss_query), ("distance", _distance_query)):
        if selected not in (key, "all"):
            continue
        started = time.perf_counter()
        content, stats = writer(ts)
        precompute_and_encode = time.perf_counter() - started
        path = output_base.with_name(output_base.name + f"_{key}.smt2")
        path.write_text(content)
        stats.update({"smt_file": str(path), "smt_bytes": path.stat().st_size,
                      "precompute_encode_seconds": precompute_and_encode})
        reports.append(stats)
    return reports
