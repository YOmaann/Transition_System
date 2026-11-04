from fractions import Fraction
from functools import lru_cache
import re
from typing import Any, Callable

from pysmt.fnode import FNode
from pysmt.smtlib.printers import to_smtlib
from pysmt.shortcuts import Symbol, Select, Int, Real
from pysmt.typing import INT, REAL, ArrayType

from profiles import TraceProfile
from utils.helper import _safe, _is_nan


list_index_pattern = re.compile(r"\[(\d+)\]")


@lru_cache(maxsize=None)
def _real_str(value: float | int) -> str:
    return to_smtlib(Real(Fraction(str(value))), daggify=False)


def _smt_sort(ty) -> str:
    if ty.is_array_type():
        return f"(Array {_smt_sort(ty.index_type)} {_smt_sort(ty.elem_type)})"
    return "Int" if ty == INT else "Real"


class GenericTransitionSystem:
    def __init__(self, profile: TraceProfile, trace: list[dict] | None = None,
                 model_time: bool = False):
        self.profile = profile
        self.trace = trace
        self.model_time = model_time
        self.var_names = profile.variable_names()
        self.constants = {
            name: float(vp.initial)
            for name, vp in profile.variables.items()
            if vp.is_constant and not vp.is_list
        }
        self.derivations: dict[str, Callable[[list, int], Any]] = {}

    def add_derivation(self, name: str, fn: Callable[[list, int], Any]) -> None:
        self.derivations[_safe(name)] = fn

    def to_smtlib(self, path: str, bound: int = 25) -> None:
        if self.trace is None:
            raise ValueError("concrete export needs a trace")

        symbols: dict[str, FNode] = {}
        for name in self.var_names:
            vp = self.profile.variables[name]
            ty = ArrayType(INT, REAL) if vp.is_list else REAL
            symbols[name] = Symbol(f"{_safe(name)}_0", ty)
        for name in self.constants:
            symbols.setdefault(name, Symbol(f"{_safe(name)}_0", REAL))
        if self.model_time:
            symbols["timestamp"] = Symbol("timestamp_0", REAL)

        array_types: dict[str, Any] = {}
        layout: dict[str, tuple[str, list[int], bool, int]] = {}
        for name, symbol in symbols.items():
            indices = [int(i) for i in list_index_pattern.findall(name)]
            array_name = _safe(list_index_pattern.sub("", name))
            if array_name not in array_types:
                element_type = symbol.symbol_type()
                for _ in indices:
                    element_type = ArrayType(INT, element_type)
                array_types[array_name] = ArrayType(INT, element_type)
            vp = self.profile.variables.get(name)
            layout[name] = (
                array_name,
                indices,
                bool(vp and vp.is_list),
                vp.list_len if vp and vp.is_list else 0,
            )

        owners: dict[str, int] = {}
        for array_name, _, _, _ in layout.values():
            owners[array_name] = owners.get(array_name, 0) + 1

        def select_str(name: str, step: int) -> str:
            array_name, indices, _, _ = layout[name]
            expr = f"(select {array_name} {step})"
            for index in indices:
                expr = f"(select {expr} {index})"
            return expr

        steps = min(bound + 1, len(self.trace))
        const_once: set[str] = set()
        with open(path, "w") as output:
            output.write("(set-logic ALL)\n")
            for array_name, ty in array_types.items():
                output.write(f"(declare-fun {array_name} () {_smt_sort(ty)})\n")
            for name in self.derivations:
                output.write(f"(declare-fun {name} () (Array Int Real))\n")

            for name, value in self.constants.items():
                entry = layout.get(name)
                if entry is None or entry[1] or owners[entry[0]] != 1:
                    continue
                sort = _smt_sort(array_types[entry[0]])
                output.write(
                    f"(assert (= {entry[0]} ((as const {sort}) {_real_str(value)})))\n"
                )
                const_once.add(name)

            for index, row in enumerate(self.trace[:steps]):
                for name in self.var_names:
                    if name in const_once or name not in row:
                        continue
                    vp = self.profile.variables[name]
                    value = row[name]
                    if vp.is_list:
                        if isinstance(value, tuple):
                            base = select_str(name, index)
                            for item_index, item in enumerate(value):
                                if not _is_nan(item):
                                    output.write(
                                        f"(assert (= (select {base} {item_index}) "
                                        f"{_real_str(float(item))}))\n"
                                    )
                    elif not _is_nan(value) and not isinstance(value, str):
                        output.write(
                            f"(assert (= {select_str(name, index)} "
                            f"{_real_str(float(value))}))\n"
                        )
                if self.model_time and "timestamp" in row:
                    output.write(
                        f"(assert (= {select_str('timestamp', index)} "
                        f"{_real_str(row['timestamp'])}))\n"
                    )
                for name, fn in self.derivations.items():
                    try:
                        value = fn(self.trace, index)
                    except (KeyError, TypeError, ZeroDivisionError, IndexError, ValueError):
                        value = None
                    if value is not None and not _is_nan(value):
                        output.write(
                            f"(assert (= (select {name} {index}) "
                            f"{_real_str(float(value))}))\n"
                        )

            output.write("(check-sat)\n(get-model)\n")
