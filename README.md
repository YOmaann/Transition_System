# Transition System

Convert JSON traces to SMT-LIB and check properties with Z3.

## Setup

```sh
pip install -r requirements.txt
```

## Examples

```sh
python main.py example/test.json
python main.py example/test.json --bound 10 --output output.smt2
python main.py example/new --batch --jobs 4 --output batch_output
python main.py example/cutin-json/cutin_sim16.json --array-mode intact --property-file example/predicted_actual_path_property.json --output output.smt2
z3 output.smt2
z3 output_witness.smt2
```

Array modes: `truncate` limits list items; `crossproduct` expands list combinations; `intact` keeps lists as arrays.

`--max-list-items 0` keeps all items; use `--array-mode intact` for array-indexed properties.

## Property example

See [predicted_actual_path_property.json](example/predicted_actual_path_property.json) for wildcard mappings and bounded `forall` expressions.

```json
{
  "mappings": [
    {"array": "ego_vehicle", "variable": "position.x", "alias": "ego_x"},
    {"array": "npc_vehicle", "variable": "position.x", "alias": "npc_x"}
  ],
  "property": {"expression": "(npc_x - ego_x)**2 >= 4"}
}
```

Array expressions use `signal[index]`; bounded quantifiers use `forall(i, alias, expression)` and `exists(i, alias, expression)`.

`sat` means a counterexample was found; `output_witness.smt2` returns its trace step and mapped values.
