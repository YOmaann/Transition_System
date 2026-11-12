from __future__ import annotations

import ast
from decimal import Decimal
from fractions import Fraction
import json
from pathlib import Path
import re
from typing import Any


_COMPARE = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "=",
    ast.NotEq: "distinct",
}


def _smt_num(value: Fraction) -> str:
    text = format(Decimal(str(float(value))), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    if text.startswith("-"):
        return f"(- {text[1:]})"
    return text


def _smt_expr(
    node: ast.AST,
    aliases: dict[str, str],
    arrays: dict[str, str] | None = None,
    vector_aliases: set[str] | None = None,
    bound_indices: set[str] | None = None,
    trace_length: int = 0,
) -> str:
    arrays = arrays or {}
    vector_aliases = vector_aliases or set()
    bound_indices = bound_indices or set()

    if isinstance(node, ast.Name):
        # Quantifier variables stay as SMT names; other names are mapped aliases.
        if node.id in bound_indices:
            return node.id
        return aliases[node.id]

    if isinstance(node, ast.Constant):
        return _smt_num(Fraction(str(node.value)))

    if isinstance(node, ast.Subscript):
        # Vector aliases are indexed within the current trace step.
        index = _smt_expr(node.slice, aliases, arrays, vector_aliases, bound_indices, trace_length)
        if isinstance(node.value, ast.Name) and node.value.id in arrays:
            name = node.value.id
            if name in vector_aliases:
                array = f"(select {arrays[name]} t)"
            elif isinstance(node.slice, ast.Name) and node.slice.id in bound_indices:
                array = arrays[name]
            else:
                array = aliases[name]
        else:
            array = _smt_expr(node.value, aliases, arrays, vector_aliases, bound_indices, trace_length)
        return f"(select {array} {index})"

    if isinstance(node, ast.UnaryOp):
        value = _smt_expr(node.operand, aliases, arrays, vector_aliases, bound_indices, trace_length)
        if isinstance(node.op, ast.Not):
            return f"(not {value})"
        if isinstance(node.op, ast.USub):
            return f"(- {value})"
        return value

    if isinstance(node, ast.BinOp):
        left = _smt_expr(node.left, aliases, arrays, vector_aliases, bound_indices, trace_length)
        right = _smt_expr(node.right, aliases, arrays, vector_aliases, bound_indices, trace_length)
        operators = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
        if isinstance(node.op, ast.Pow):
            exponent = int(node.right.value)
            if exponent == 0:
                return "1"
            if exponent == 1:
                return left
            return f"(* {' '.join([left] * exponent)})"
        return f"({operators[type(node.op)]} {left} {right})"

    if isinstance(node, ast.Compare):
        left = _smt_expr(node.left, aliases, arrays, vector_aliases, bound_indices, trace_length)
        right = _smt_expr(node.comparators[0], aliases, vector_aliases=vector_aliases,
                          arrays=arrays, bound_indices=bound_indices, trace_length=trace_length)
        return f"({_COMPARE[type(node.ops[0])]} {left} {right})"

    if isinstance(node, ast.BoolOp):
        operator = "and" if isinstance(node.op, ast.And) else "or"
        values = " ".join(
            _smt_expr(value, aliases, arrays, vector_aliases, bound_indices, trace_length)
            for value in node.values
        )
        return f"({operator} {values})"

    if isinstance(node, ast.Call):
        if (isinstance(node.func, ast.Name) and node.func.id in {"forall", "exists"}
                and len(node.args) == 3 and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Name)):
            index = node.args[0].id
            domain = node.args[1].id
            # Vectors use their recorded size; scalar time series use trace length.
            if domain in vector_aliases:
                count = f"(select input_{domain}_length t)"
            else:
                count = str(trace_length)
            body = _smt_expr(
                node.args[2], aliases, arrays, vector_aliases,
                bound_indices | {index}, trace_length,
            )
            bounds = f"(and (<= 0 {index}) (< {index} {count}))"
            if node.func.id == "forall":
                return f"(forall (({index} Int)) (=> {bounds} {body}))"
            return f"(exists (({index} Int)) (and {bounds} {body}))"
        value = _smt_expr(node.args[0], aliases, arrays, vector_aliases, bound_indices, trace_length)
        return f"(ite (>= {value} 0) {value} (- {value}))"

    raise ValueError("unsupported property expression")


def _eval_derived(node: ast.AST, aliases: dict[str, Fraction]) -> Fraction:
    if isinstance(node, ast.Expression):
        return _eval_derived(node.body, aliases)

    if isinstance(node, ast.Name):
        return aliases[node.id]

    if isinstance(node, ast.Constant):
        return Fraction(str(node.value))

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval_derived(node.operand, aliases)
        return -value if isinstance(node.op, ast.USub) else value

    if isinstance(node, ast.BinOp):
        left = _eval_derived(node.left, aliases)
        if isinstance(node.op, ast.Pow):
            return left ** node.right.value

        right = _eval_derived(node.right, aliases)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "abs":
        return abs(_eval_derived(node.args[0], aliases))

    raise NotImplementedError(type(node).__name__)


def _value_sort(value: Any) -> str:
    if isinstance(value, (tuple, list)):
        if not value:
            raise ValueError("cannot infer the type of an empty array")
        return f"(Array Int {_value_sort(value[0])})"
    return "Real"


def _trace_value(row: dict[str, Any], path: str) -> Any:
    if path in row:
        return row[path]

    if "[*]" in path:
        # Turn concrete flattened indexes such as [0] and [1] into one tuple.
        pattern = re.escape(path).replace(r"\[\*\]", r"\[(\d+)\]")
        matches = [
            (match.groups(), row[key])
            for key in row
            if (match := re.fullmatch(pattern, key))
        ]
        if matches:
            nested = {}
            for indexes, value in matches:
                current = nested
                for index in indexes[:-1]:
                    current = current.setdefault(int(index), {})
                current[int(indexes[-1])] = value

            def as_tuple(value):
                if isinstance(value, dict):
                    return tuple(as_tuple(value[index]) for index in sorted(value))
                return value

            return as_tuple(nested)

        # Intact mode keeps a list under the flattened path without indexes.
        intact_path = path.replace("[*]", "")
        if intact_path in row:
            return row[intact_path]

    prefix = f"{path.replace('[*]', '')}["
    indexed = sorted(
        (key for key in row if key.startswith(prefix) and key.endswith("]")),
        key=lambda key: int(key[len(prefix):-1]),
    )
    if indexed:
        return tuple(row[key] for key in indexed)
    return None


def _array_assertions(array: str, step: int, value: Any, indexes=()) -> list[str]:
    if not isinstance(value, (tuple, list)):
        selected = array
        for index in (step, *indexes):
            selected = f"(select {selected} {index})"
        return [f"(assert (= {selected} {_smt_num(Fraction(str(value)))}))"]

    assertions = []
    for index, item in enumerate(value):
        assertions.extend(_array_assertions(array, step, item, (*indexes, index)))
    return assertions


def _load_config(path: Path):
    config = json.loads(path.read_text())
    mappings = [
        (
            f"{mapping['array'].strip().rstrip('.')}."
            f"{mapping['variable'].strip().lstrip('.')}",
            mapping["alias"],
        )
        for mapping in config["mappings"]
    ]

    derived = []
    for alias, source in config.get("derived", {}).items():
        derived.append((alias, ast.parse(source, mode="eval")))

    prop = config.get("property")
    expression = prop.get("expression") if isinstance(prop, dict) else prop
    property_expr = ast.parse(expression, mode="eval")

    # Keep only derived values used by the property, including their dependencies.
    derived_by_name = dict(derived)

    def names_in(expr: ast.AST) -> set[str]:
        return {node.id for node in ast.walk(expr) if isinstance(node, ast.Name)} - {"abs"}

    needed = set()
    pending = names_in(property_expr) & set(derived_by_name)
    while pending:
        alias = pending.pop()
        if alias in needed:
            continue
        needed.add(alias)
        pending.update((names_in(derived_by_name[alias]) & set(derived_by_name)) - needed)

    derived = [(alias, expr) for alias, expr in derived if alias in needed]
    return mappings, derived, property_expr, expression


def compile_property(ts, config_path: Path, output_path: Path) -> dict[str, Any]:
    mappings, derived, property_expr, expression = _load_config(config_path)
    output = ["(set-logic ALL)", "(declare-const cex_t Int)"]

    input_arrays = {alias: f"input_{alias}" for _, alias in mappings}
    input_sorts = {}
    vector_aliases = set()
    for path, alias in mappings:
        # A tuple-valued trace field becomes an array at each trace step.
        sample = next((_trace_value(row, path) for row in ts.trace
                       if _trace_value(row, path) is not None), 0)
        input_sorts[alias] = _value_sort(sample)
        if isinstance(sample, (tuple, list)):
            vector_aliases.add(alias)
        output.append(
            f"(declare-fun {input_arrays[alias]} () (Array Int {input_sorts[alias]}))"
        )
        if alias in vector_aliases:
            output.append(f"(declare-fun input_{alias}_length () (Array Int Int))")

    derived_arrays = {alias: f"derived_{alias}" for alias, _ in derived}
    for array in derived_arrays.values():
        output.append(f"(declare-fun {array} () (Array Int Real))")

    valid_steps = []
    checked = 0
    previous_values = {}
    previous_derived_values = {}
    for index, row in enumerate(ts.trace):
        values = {alias: _trace_value(row, path) for path, alias in mappings}
        aliases = {
            alias: Fraction(str(value))
            for alias, value in values.items()
            if not isinstance(value, (tuple, list))
        }
        derived_values = {}

        for alias, derived_expr in derived:
            value = _eval_derived(derived_expr, aliases)
            aliases[alias] = value
            derived_values[alias] = value

        valid_steps.append(index)
        checked += 1
        for alias, value in values.items():
            # Link repeated values to the previous step to keep the SMT file smaller.
            if alias in previous_values and value == previous_values[alias]:
                output.append(
                    f"(assert (= (select {input_arrays[alias]} {index}) "
                    f"(select {input_arrays[alias]} {index - 1})))"
                )
                if alias in vector_aliases:
                    output.append(
                        f"(assert (= (select input_{alias}_length {index}) "
                        f"(select input_{alias}_length {index - 1})))"
                    )
            elif isinstance(value, (tuple, list)):
                output.append(
                    f"(assert (= (select input_{alias}_length {index}) {len(value)}))"
                )
                output.extend(_array_assertions(input_arrays[alias], index, value))
            else:
                output.append(
                    f"(assert (= (select {input_arrays[alias]} {index}) {_smt_num(Fraction(str(value)))}))"
                )
            previous_values[alias] = value
        for alias, value in derived_values.items():
            if (alias in previous_derived_values
                    and value == previous_derived_values[alias]):
                output.append(
                    f"(assert (= (select {derived_arrays[alias]} {index}) "
                    f"(select {derived_arrays[alias]} {index - 1})))"
                )
            else:
                output.append(
                    f"(assert (= (select {derived_arrays[alias]} {index}) {_smt_num(value)}))"
                )
            previous_derived_values[alias] = value

    aliases = {
        alias: f"(select {array} t)"
        for alias, array in input_arrays.items()
    }
    aliases.update({
        alias: f"(select {array} t)"
        for alias, array in derived_arrays.items()
    })

    division_nodes = [
        node.right for node in ast.walk(property_expr)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
    ]
    property_term = _smt_expr(
        property_expr.body,
        aliases,
        arrays=input_arrays,
        vector_aliases=vector_aliases,
        trace_length=len(ts.trace),
    )
    output.append(f"(define-fun property_at ((t Int)) Bool {property_term})")

    if valid_steps:
        choices = [f"(= cex_t {index})" for index in valid_steps]
        domain = choices[0] if len(choices) == 1 else f"(or {' '.join(choices)})"
        output.append(f"(assert {domain})")

        witness_aliases = {
            alias: f"(select {array} cex_t)"
            for alias, array in input_arrays.items()
        }
        witness_aliases.update({
            alias: f"(select {array} cex_t)"
            for alias, array in derived_arrays.items()
        })
        guards = [
            f"(not (= {_smt_expr(node, witness_aliases)} 0))"
            for node in division_nodes
        ]
        condition = [*guards, "(not (property_at cex_t))"]
        body = condition[0] if len(condition) == 1 else f"(and {' '.join(condition)})"
        output.append(f"(assert {body})")
    else:
        output.append("(assert false)")

    output.append("(check-sat)")
    witness_values = []
    if valid_steps:
        witness_values.append("cex_t")
        witness_values.extend(f"(select {array} cex_t)" for array in input_arrays.values())
        witness_values.extend(f"(select {array} cex_t)" for array in derived_arrays.values())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output) + "\n")

    witness_path = ""
    if valid_steps:
        witness_file = output_path.with_name(
            output_path.stem + "_witness" + output_path.suffix
        )
        witness_query = output + [f"(get-value ({' '.join(witness_values)}))"]
        witness_file.write_text("\n".join(witness_query) + "\n")
        witness_path = str(witness_file)

    return {
        "property": expression,
        "trace_steps": len(ts.trace),
        "checked_steps": checked,
        "missing_steps": 0,
        "non_finite_steps": 0,
        "smt_file": str(output_path),
        "witness_smt_file": witness_path,
        "smt_bytes": output_path.stat().st_size,
    }


# perception object vs ground_truth object.
