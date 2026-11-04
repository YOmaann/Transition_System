from dataclasses import dataclass, field
from typing import Any

from trace import _block_starts
from utils.helper import _is_nan


@dataclass
class VarProfile:
    name: str
    initial: Any = 0.0
    is_constant: bool = False
    is_list: bool = False
    list_len: int = 0


@dataclass
class TraceProfile:
    variables: dict[str, VarProfile] = field(default_factory=dict)
    num_steps: int = 0
    block_boundaries: list[int] = field(default_factory=list)

    def variable_names(self) -> list[str]:
        return sorted(self.variables)


def profile_trace(trace: list[dict[str, Any]]) -> TraceProfile:
    if not trace:
        raise ValueError("Empty trace")

    profile = TraceProfile(
        num_steps=len(trace),
        block_boundaries=_block_starts(trace),
    )
    names = {name for row in trace for name in row if name != "timestamp"}

    for name in sorted(names):
        values = [row[name] for row in trace if name in row]
        if not values or isinstance(values[0], str):
            continue

        is_list = any(isinstance(value, tuple) for value in values)
        lengths = {len(value) for value in values if isinstance(value, tuple)}
        if is_list and len(lengths) != 1:
            continue

        numeric = []
        for value in values:
            items = value if isinstance(value, tuple) else (value,)
            numeric.extend(float(item) for item in items if not _is_nan(item))
        if not numeric:
            continue

        profile.variables[name] = VarProfile(
            name=name,
            initial=next(
                value for value in values
                if isinstance(value, tuple) or not _is_nan(value)
            ),
            is_constant=len(set(numeric)) == 1,
            is_list=is_list,
            list_len=next(iter(lengths), 0),
        )

    return profile
