# Transition System

This module converts a recorded trace to SMT-LIB with every observed value pinned to its logged value. Logic queries can be checked against that concrete trace.

## Installation

```
pip install -r requirements.txt
```

This installs [PySMT](https://github.com/pysmt/pysmt) and the z3 solver backend used to check concrete trace properties.

## How to use

### Using the command line

To import a JSON trace and export its concrete values:

```
python main.py ./example/test.json
```

The input is a JSON file describing a trace (see [example/test.json](example/test.json) for the expected shape). Records are grouped by signal name, each with a `timestamp` and a nested object of values. The tool:

1. Loads the JSON and flattens each record into a flat set of variables.
2. Builds a profile of every variable (observed min/max, monotonicity, whether it is boolean/constant/list).
3. Constructs a bounded representation of the recorded trace.
4. Writes the concrete trace to an SMT-LIB (`.smt2`) file that can be fed to any SMT solver.

### Arguments

| Argument            | Default               | Description                                                                                                   |
| ------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------- |
| `path`              | `./example/test.json` | Path to the input JSON trace file.                                                                            |
| `--batch`            | _(off)_               | Process each matching JSON trace in a directory or glob.                                                       |
| `--jobs N`            | CPU count             | Maximum number of traces processed in parallel in batch mode.                                                 |
| `-b`, `--bound`     | `25`                  | Bound on the length of traces to check.                                                                       |
| `-o`, `--output`    | `output.smt2`         | Path to write the SMT-LIB output.                                                                             |
| `--array-mode`      | `truncate`            | How to flatten arrays in the trace: `truncate`, `crossproduct`, or `intact`.                                  |
| `--max-list-items`  | `2`                   | Max number of items to keep when flattening lists.                                                            |
| `--property-file`   | _(off)_               | Compile a user-defined property from a JSON schema over every observed timestamp.                              |

#### Array modes

Traces often contain arrays (e.g. a list of detected objects per frame). `--array-mode` controls how they are flattened:

- `truncate` — keep the first `--max-list-items` elements of each array (arrays are preserved but capped).
- `crossproduct` — expand the trace by taking the cross product of array elements (can lead to state explosion; each array is reduced to a single element per branch).
- `intact` — keep arrays as tuples in the flattened trace.

### Examples

```
# Export the default example with a shorter bound
python main.py ./example/test.json --bound 10

# Check every JSON trace in a directory and write one SMT-LIB file per trace
python main.py ./example/new --batch --output batch_output

# Run at most four traces at once
python main.py ./example/new --batch --jobs 4 --output batch_output

# A glob can select a subset of traces
python main.py './example/new/trace_sim*.json' --batch --output batch_output

# Compile a user-defined polynomial property
python main.py ./example/test.json --property-file ./example/property.json --output property.smt2

# Check PRSS longitudinal safety with the built-in PRSS formula
python main.py ./example/scrape/camera-lidar-fusion/cutin_sim1.json --property-file ./example/prss_property.json --output prss_property.smt2
```

### User-defined properties

The property JSON maps flattened trace paths to short aliases, then uses those aliases in one numeric comparison. `array` is the flattened path prefix and `variable` is the remaining path; include an explicit list index such as `objects[0]` when selecting an object. The expression is parsed with Python's built-in `ast` module and restricted to numeric arithmetic and one comparison.

```json
{
  "mappings": [
    {"array": "ego_vehicle_kinematicks", "variable": "ego_vehicle_kinematicks.x", "alias": "ego_x"},
    {"array": "npc_vehicle_kinematicks", "variable": "npc_vehicle_kinematicks.x", "alias": "npc_x"}
  ],
  "property": {"expression": "(npc_x - ego_x) ** 2 >= 4"}
}
```

Supported arithmetic is `+`, `-`, `*`, `/`, unary `+`/`-`, and integer powers from 0 through 8. Comparisons are `<`, `<=`, `>`, `>=`, `==`, and `!=`; boolean composition supports `and`, `or`, and `not`, with `abs(...)` for absolute values. The compiler stores mapped signals in SMT arrays, skips steps with missing or non-finite inputs, and defines the property once as a function of the step index. Run the generated query first; if it returns `sat`, run the companion `_witness.smt2` file to retrieve `cex_t` and the mapped signal values at that step.

Derived aliases can name intermediate polynomials in the JSON, evaluated from top to bottom before the property. The PRSS example in [example/prss_property.json](example/prss_property.json) writes `q`, `k`, the threshold, and the piecewise safety condition entirely in JSON; there is no PRSS-specific compiler path. `and`, `or`, `not`, and `abs(...)` are supported alongside arithmetic and comparisons.

In batch mode, `--output` names a directory. The directory is created if needed, and each trace is exported as `<trace-name>.smt2`. Traces run in separate processes, up to `--jobs` at a time; each trace’s report is printed together when that job finishes. If any trace cannot be processed, the remaining traces are still attempted and the command exits with an error summary. Use `--jobs 1` for sequential processing.

## Output

The generated `.smt2` file pins the observed values and ends with `(check-sat)` and `(get-model)`. Run it with Z3:

```
z3 output.smt2
```
