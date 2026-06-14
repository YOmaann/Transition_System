from fractions import Fraction
from typing import Any, Callable, Sequence

from pysmt.exceptions import SolverReturnedUnknownResultError
from pysmt.fnode import FNode
from pysmt.shortcuts import (
    And,
    Bool,
    Equals,
    GE,
    GT,
    Implies,
    LE,
    LT,
    Minus,
    Not,
    Or,
    Real,
    Solver,
    Symbol,
)
from pysmt.typing import REAL

from profiles import VarProfile, TraceProfile
from utils.helper import _safe


StateFn = Callable[[dict], FNode]


def _real(value: float | int) -> FNode:
    return Real(Fraction(str(value)))


class GenericTransitionSystem:

    def __init__(self, profile: TraceProfile, skip_constant: bool = True,
                 exact_initial: bool = False, use_invariants: bool = True,
                 model_time: bool = False):
        self.profile = profile
        self.exact_initial = exact_initial
        self.use_invariants = use_invariants
        self.model_time = model_time
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
                state[name] = tuple(
                    Symbol(f"{base}_{i}_{t}", REAL) for i in range(vp.list_len)
                )
            else:
                state[name] = Symbol(f"{base}_{t}", REAL)
        for name, val in self.constants.items():
            state[name] = _real(val)
        if self.model_time:
            state["_t"] = Symbol(f"_t_{t}", REAL)
        return state

    def symbolic_states_at(self, timestamps: Sequence[float]) -> list[dict[str, Any]]:
        path = []
        for i, t in enumerate(timestamps):
            s = self.make_state(i)
            s["_t_concrete"] = float(t)
            path.append(s)
        return path

    def pin_time(self, path: list[dict], timestamps: Sequence[float]) -> list:
        if not self.model_time:
            raise ValueError("pin_time requires model_time=True")
        return [
            Equals(path[i]["_t"], _real(timestamps[i]))
            for i in range(len(timestamps))
        ]

    def initial_constraints(self, s0: dict) -> list:
        constraints = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            if vp.is_list:
                init = vp.initial if isinstance(vp.initial, tuple) else (float(vp.initial),) * vp.list_len
                for i, x in enumerate(s0[name]):
                    if self.exact_initial:
                        constraints.append(Equals(x, _real(init[i])))
                    else:
                        margin = self._margin(vp)
                        constraints.append(GE(x, _real(float(init[i]) - margin)))
                        constraints.append(LE(x, _real(float(init[i]) + margin)))
            elif self.exact_initial or vp.is_boolean:
                constraints.append(Equals(s0[name], _real(vp.initial)))
            else:
                margin = self._margin(vp)
                constraints.append(GE(s0[name], _real(float(vp.initial) - margin)))
                constraints.append(LE(s0[name], _real(float(vp.initial) + margin)))
        if self.model_time:
            ti = self.profile.time_initial
            if self.exact_initial:
                constraints.append(Equals(s0["_t"], _real(ti)))
            else:
                tm = self._time_margin()
                constraints.append(GE(s0["_t"], _real(ti - tm)))
                constraints.append(LE(s0["_t"], _real(ti + tm)))
        return constraints

    def invariant_constraints(self, s: dict) -> list:
        constraints = []
        for name in self.var_names:
            vp = self.profile.variables[name]
            xs = s[name] if vp.is_list else (s[name],)
            for x in xs:
                if vp.is_boolean:
                    constraints.append(
                        Or(Equals(x, _real(0)), Equals(x, _real(1)))
                    )
                elif self.use_invariants:
                    margin = self._margin(vp)
                    constraints.append(GE(x, _real(vp.min_val - margin)))
                    constraints.append(LE(x, _real(vp.max_val + margin)))
        if self.model_time and self.use_invariants:
            tm = self._time_margin()
            constraints.append(GE(s["_t"], _real(self.profile.time_min - tm)))
            constraints.append(LE(s["_t"], _real(self.profile.time_max + tm)))
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
            delta = Minus(s_next[name], s_curr[name])
            if vp.is_monotone_inc:
                constraints.append(GE(delta, _real(0)))
            if vp.is_monotone_dec:
                constraints.append(LE(delta, _real(0)))
        if self.model_time:
            dt = Minus(s_next["_t"], s_curr["_t"])
            constraints.append(GE(dt, _real(0)))
            tm = self._time_margin()
            dmin = max(self.profile.time_min_delta - tm, 0.0)
            dmax = self.profile.time_max_delta + tm
            if dmax > 0:
                constraints.append(GE(dt, _real(dmin)))
                constraints.append(LE(dt, _real(dmax)))
        return constraints

    def _margin(self, vp: VarProfile) -> float:
        return max((vp.max_val - vp.min_val) * self.profile.margin_pct, 0.01)

    def _time_margin(self) -> float:
        return max((self.profile.time_max - self.profile.time_min) * self.profile.margin_pct, 0.01)

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
                        lambda s, n=name: Or(*[GE(x, _real(0.5)) for x in s[n]]))
                    self.propositions[f"{base}_all_true"] = (
                        lambda s, n=name: And(*[GE(x, _real(0.5)) for x in s[n]]))
                else:
                    self.propositions[f"{base}_any_high"] = (
                        lambda s, n=name, th=vp.q75: Or(
                            *[GT(x, _real(th)) for x in s[n]]
                        ))
                    self.propositions[f"{base}_all_low"] = (
                        lambda s, n=name, th=vp.q25: And(
                            *[LT(x, _real(th)) for x in s[n]]
                        ))
                    if vp.min_val <= 0 <= vp.max_val:
                        eps = max((vp.max_val - vp.min_val) * 0.05, 0.01)
                        self.propositions[f"{base}_any_near_zero"] = (
                            lambda s, n=name, e=eps: Or(*[
                                And(GE(x, _real(-e)), LE(x, _real(e)))
                                for x in s[n]
                            ]))
            elif vp.is_boolean:
                self.propositions[f"{base}_true"] = lambda s, n=name: GE(s[n], _real(0.5))
                self.propositions[f"{base}_false"] = lambda s, n=name: LT(s[n], _real(0.5))
            else:
                self.propositions[f"{base}_high"] = (
                    lambda s, n=name, th=vp.q75: GT(s[n], _real(th)))
                self.propositions[f"{base}_low"] = (
                    lambda s, n=name, th=vp.q25: LT(s[n], _real(th)))
                if vp.min_val <= 0 <= vp.max_val:
                    eps = max((vp.max_val - vp.min_val) * 0.05, 0.01)
                    self.propositions[f"{base}_near_zero"] = (
                        lambda s, n=name, e=eps: And(
                            GE(s[n], _real(-e)), LE(s[n], _real(e))
                        ))

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
                              if j > 0 else Bool(True))
            clauses.append(And(q_at_j, p_false_before))
        return Or(*clauses)

    def check(self, name: str, phi_fn: Callable, bound: int = 25,
              extra_constraints: Callable | None = None, timeout_ms: int = 30000):
        path = self.make_path(bound)
        solver = Solver(name="z3", solver_options={"timeout": timeout_ms})
        solver.add_assertions(self.path_constraints(path))
        if extra_constraints:
            solver.add_assertion(extra_constraints(path))

        phi = phi_fn(path)
        solver.add_assertion(Not(phi))

        try:
            result = solver.solve()
        except SolverReturnedUnknownResultError:
            result = None
        if result is False:
            print(f"  HOLDS  :: {name}  (bound={bound})")
        elif result is True:
            m = solver.get_model()
            print(f"  FAILS  :: {name}  -- counterexample:")
            self._print_trace(m, path)
        else:
            print(f"  ?????  :: {name}  -- timeout / unknown")
        print()
        return result

    def check_reachable(self, name: str, target_fn: Callable, bound: int = 25,
                        timeout_ms: int = 30000):
        path = self.make_path(bound)
        solver = Solver(name="z3", solver_options={"timeout": timeout_ms})
        solver.add_assertions(self.path_constraints(path))
        solver.add_assertion(target_fn(path))

        try:
            result = solver.solve()
        except SolverReturnedUnknownResultError:
            result = None
        if result is True:
            m = solver.get_model()
            print(f"  REACHABLE :: {name}")
            self._print_trace(m, path)
        elif result is False:
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
        v = model.get_value(var)
        try:
            value = v.constant_value()
            if isinstance(value, Fraction):
                return float(value)
            return float(value)
        except (ValueError, ArithmeticError):
            return str(v)

    def _eval_state(self, model, s: dict) -> dict[str, Any]:
        vals: dict[str, Any] = {}
        for key, var in s.items():
            if key == "_t_concrete":
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
        print("Generic PySMT Transition System")
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

    # get output in SMT-LIB format for use with other tools.
    def to_smtlib(self, path: list[dict]) -> str:
        decls = [] # declarations for all variables in the path
        for s in path:
            for var in s.values():
                if isinstance(var, tuple):
                    for x in var:
                        decls.append(f"(declare-fun {x} () Real)")
                else:
                    decls.append(f"(declare-fun {var} () Real)")

        constraints = self.path_constraints(path)
        constraints_str = "\n  ".join(c.serialize() for c in constraints)

        # set languauge as quantifier-free linear real arithmetic and assert all constraints. and check for sat.
        return f"(set-logic QF_LRA)\n{chr(10).join(decls)}\n(assert\n  (and\n  {constraints_str}\n  )\n)\n(check-sat)\n(get-model)\n"