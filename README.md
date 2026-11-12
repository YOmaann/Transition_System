# Transition System

Convert JSON traces to SMT-LIB and check properties with Z3.

## Setup

```sh
pip install -r requirements.txt
```

## Parameters

- `path`: input JSON file; use a directory or glob with `--batch`.
- `--output`, `-o`: output file, or output directory in batch mode.
- `--bound`, `-b`: limit trace steps for regular SMT export.
- `--property-file JSON`: compile a custom property from a JSON file.
- `--array-mode`: choose `truncate` (default), `crossproduct`, or `intact`.
- `--max-list-items N`: cap list lengths; `0` keeps every item.
- `--batch`: process multiple JSON files; `--recursive` includes subfolders.
- `--jobs N`: maximum number of batch jobs to run at once.

## Examples

```sh
python main.py example/test.json
python main.py example/test.json --bound 10 --output output.smt2
python main.py example/new --batch --jobs 4 --output batch_output
python main.py example/cutin-json/cutin_sim16.json --array-mode intact --property-file example/predicted_actual_path_property.json --output output.smt2
z3 output.smt2
z3 output_witness.smt2
```

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
