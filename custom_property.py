from __future__ import annotations

import ast
from fractions import Fraction
import json
import math
from pathlib import Path
import re
from typing import Any


_COMPARE = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
            ast.Eq: "=", ast.NotEq: "distinct"}


def _smt_num(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    if value < 0:
        return f"(- (/ {-value.numerator} {value.denominator}))"
    return f"(/ {value.numerator} {value.denominator})"


def _number(value: Any) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a finite numeric value, got {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite values cannot be used in a property")
    return Fraction(str(value))


# def _validate_expr(node: ast.AST, aliases: set[str]) -> None:
#     if isinstance(node, ast.Expression):
#         _validate_expr(node.body, aliases)
#     elif isinstance(node, ast.Compare):
#         if len(node.ops) != 1 or type(node.ops[0]) not in _COMPARE:
#             raise ValueError("comparisons must use <, <=, >, >=, ==, or !=")
#         _validate_expr(node.left, aliases)
#         _validate_expr(node.comparators[0], aliases)
#     elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
#         for value in node.values:
#             _validate_expr(value, aliases)
#     elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
#         _validate_expr(node.left, aliases)
#         _validate_expr(node.right, aliases)
#         if isinstance(node.op, ast.Pow) and not (
#             isinstance(node.right, ast.Constant) and isinstance(node.right.value, int)
#             and not isinstance(node.right.value, bool) and 0 <= node.right.value <= 8
#         ):
#             raise ValueError("exponents must be integer constants from 0 through 8")
#     elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
#         _validate_expr(node.operand, aliases)
#     elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
#         _validate_expr(node.operand, aliases)
#     elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
#           and node.func.id == "abs" and len(node.args) == 1 and not node.keywords):
#         _validate_expr(node.args[0], aliases)
#     elif isinstance(node, ast.Name):
#         if node.id not in aliases:
#             raise ValueError(f"unknown property alias: {node.id}")
#     elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
#         if isinstance(node.value, float) and not math.isfinite(node.value):
#             raise ValueError("property constants must be finite")
#     else:
#         raise ValueError(f"unsupported property expression: {ast.dump(node)}")


def _smt_expr(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases[node.id]
    elif isinstance(node, ast.Constant):
        return _smt_num(Fraction(str(node.value)))
    elif isinstance(node, ast.UnaryOp):
        value = _smt_expr(node.operand, aliases)
        return f"(not {value})" if isinstance(node.op, ast.Not) else (
            f"(- {value})" if isinstance(node.op, ast.USub) else value
        )
    elif isinstance(node, ast.BinOp):
        left = _smt_expr(node.left, aliases)
        right = _smt_expr(node.right, aliases)
        operators = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
        if isinstance(node.op, ast.Pow):
            exponent = int(node.right.value)
            if exponent == 0:
                return "1"
            return left if exponent == 1 else "(* " + " ".join([left] * exponent) + ")"
        return f"({operators[type(node.op)]} {left} {right})"
    elif isinstance(node, ast.Compare):
        left = _smt_expr(node.left, aliases)
        right = _smt_expr(node.comparators[0], aliases)
        return f"({_COMPARE[type(node.ops[0])]} {left} {right})"
    elif isinstance(node, ast.BoolOp):
        operator = "and" if isinstance(node.op, ast.And) else "or"
        return f"({operator} {' '.join(_smt_expr(v, aliases) for v in node.values)})"
    elif isinstance(node, ast.Call):
        value = _smt_expr(node.args[0], aliases)
        return f"(ite (>= {value} 0) {value} (- {value}))"
    else:
        raise ValueError("unsupported property expression")


def _division_denominators(node: ast.AST) -> list[ast.AST]:
    denominators = []
    for child in ast.iter_child_nodes(node):
        denominators.extend(_division_denominators(child))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        denominators.append(node.right)
    return denominators


def _safe_path(mapping: dict[str, Any]) -> tuple[str, str]:
    if not isinstance(mapping, dict):
        raise ValueError("each mapping must be a JSON object")
    array = mapping.get("array")
    variable = mapping.get("variable")
    alias = mapping.get("alias")
    if not all(isinstance(v, str) and v.strip() for v in (array, variable, alias)):
        raise ValueError("each mapping needs non-empty string fields: array, variable, alias")
    path = f"{array.strip().rstrip('.')}\u002e{variable.strip().lstrip('.')}"
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\[[0-9]+\])?(?:\.[A-Za-z_]\w*(?:\[[0-9]+\])?)*", path):
        raise ValueError(f"invalid flattened trace path: {path!r}")
    if not re.fullmatch(r"[A-Za-z_]\w*", alias):
        raise ValueError(f"alias must be a simple identifier: {alias!r}")
    return path, alias


def _parse_expr(source: str, aliases: set[str]) -> ast.Expression:
    try:
        parsed = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid property expression: {exc.msg}") from exc
    # _validate_expr(parsed, aliases)
    return parsed


def _load_config(path: Path):
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read property JSON {path}: {exc}") from exc
    # if not isinstance(config, dict) or not isinstance(config.get("mappings"), list):
    #     raise ValueError("property JSON must contain a mappings array")
    mappings = [_safe_path(m) for m in config["mappings"]]
    base_aliases = {alias for _, alias in mappings}
    if len(base_aliases) != len(mappings):
        raise ValueError("mapping aliases must be unique")

    derived = []
    derived_config = config.get("derived", {})
    if not isinstance(derived_config, dict):
        raise ValueError("derived must map aliases to expression strings")
    available = set(base_aliases)
    for alias, source in derived_config.items():
        if not re.fullmatch(r"[A-Za-z_]\w*", alias) or alias in available:
            raise ValueError(f"invalid or repeated derived alias: {alias!r}")
        if not isinstance(source, str):
            raise ValueError(f"derived expression for {alias!r} must be a string")
        parsed = _parse_expr(source, available)
        if isinstance(parsed.body, (ast.Compare, ast.BoolOp)):
            raise ValueError(f"derived alias {alias!r} must be numeric")
        derived.append((alias, parsed))
        available.add(alias)

    prop = config.get("property")
    expression = prop.get("expression") if isinstance(prop, dict) else prop
    if not isinstance(expression, str):
        raise ValueError('property must be an expression string or {"expression": "..."}')
    parsed = _parse_expr(expression, available)
    used = {node.id for node in ast.walk(parsed) if isinstance(node, ast.Name)} - {"abs"}
    for _, derived_expr in derived:
        used.update(node.id for node in ast.walk(derived_expr) if isinstance(node, ast.Name))
    used.difference_update(alias for alias, _ in derived)
    unused = base_aliases - used
    if unused:
        raise ValueError(f"mapped aliases are unused by the property: {', '.join(sorted(unused))}")
    return mappings, derived, parsed, expression


def compile_property(ts, config_path: Path, output_path: Path) -> dict[str, Any]:
    mappings, derived, property_expr, expression = _load_config(config_path)
    if not ts.trace:
        raise ValueError("cannot evaluate a property over an empty trace")
    output = ["(set-logic ALL)", "(declare-const cex_t Int)"]
    input_arrays = {alias: f"input_{alias}" for _, alias in mappings}
    for array in input_arrays.values():
        output.append(f"(declare-fun {array} () (Array Int Real))")

    valid_steps: list[int] = []
    skipped_reasons: dict[str, int] = {"missing": 0, "non_finite": 0}
    checked = 0
    for index, row in enumerate(ts.trace):
        values: dict[str, str] = {}
        reason = None
        for path, alias in mappings:
            raw = row.get(path)
            if raw is None:
                reason = "missing"
                break
            try:
                values[alias] = _smt_num(_number(raw))
            except ValueError:
                reason = "non_finite"
                break
        if reason:
            skipped_reasons[reason] += 1
            continue
        valid_steps.append(index)
        checked += 1
        for alias, value in values.items():
            output.append(
                f"(assert (= (select {input_arrays[alias]} {index}) {value}))"
            )

    aliases = {
        alias: f"(select {array} t)"
        for alias, array in input_arrays.items()
    }
    division_nodes = []
    for alias, derived_expr in derived:
        division_nodes.extend(_division_denominators(derived_expr))
        function = f"derived_{alias}"
        term = _smt_expr(derived_expr.body, aliases)
        output.append(f"(define-fun {function} ((t Int)) Real {term})")
        aliases[alias] = f"({function} t)"

    division_nodes.extend(_division_denominators(property_expr))
    property_term = _smt_expr(property_expr.body, aliases)
    output.append(f"(define-fun property_at ((t Int)) Bool {property_term})")

    if valid_steps:
        choices = [
            f"(= cex_t {index})" for index in valid_steps
        ]
        domain = choices[0] if len(choices) == 1 else "(or " + " ".join(choices) + ")"
        output.append(f"(assert {domain})")
        witness_aliases = {
            alias: f"(select {array} cex_t)"
            for alias, array in input_arrays.items()
        }
        for alias, _ in derived:
            witness_aliases[alias] = f"(derived_{alias} cex_t)"
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
    if valid_steps:
        witness_values = ["cex_t"]
        witness_values.extend(
            f"(select {array} cex_t)" for array in input_arrays.values()
        )
        witness_values.extend(
            f"(derived_{alias} cex_t)" for alias, _ in derived
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(output) + "\n")
    witness_path = ""
    if valid_steps:
        witness_path = str(output_path.with_name(
            output_path.stem + "_witness" + output_path.suffix
        ))
        witness_query = output + [f"(get-value ({' '.join(witness_values)}))"]
        Path(witness_path).write_text("\n".join(witness_query) + "\n")
    return {
        "property": expression,
        "trace_steps": len(ts.trace),
        "checked_steps": checked,
        "missing_steps": skipped_reasons["missing"],
        "non_finite_steps": skipped_reasons["non_finite"],
        "smt_file": str(output_path),
        "witness_smt_file": witness_path,
        "smt_bytes": output_path.stat().st_size,
    }


def validate_property_file(path: Path) -> None:
    _load_config(path)

