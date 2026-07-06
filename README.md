# Transition System

This module enables conversion of a generic trace to a transition system representation (SMTLib format). Logic queries can be made on the symbolic representation of the trace.

## Installation

```
pip install -r requirements.txt
```

This installs [PySMT](https://github.com/pysmt/pysmt) and the z3 solver backend used to build and query the symbolic representation.

## How to use

### Using the command line

To import a JSON trace, run standard checks on it, and export the transition system:

```
python main.py ./example/test.json
```

The input is a JSON file describing a trace (see [example/test.json](example/test.json) for the expected shape). Records are grouped by signal name, each with a `timestamp` and a nested object of values. The tool:

1. Loads the JSON and flattens each record into a flat set of variables.
2. Builds a profile of every variable (observed min/max, monotonicity, whether it is boolean/constant/list).
3. Constructs a bounded transition system and runs the requested checks.
4. Writes the symbolic path to an SMT-LIB (`.smt2`) file that can be fed to any SMT solver.

### Arguments

| Argument            | Default               | Description                                                                                                   |
| ------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------- |
| `path`              | `./example/test.json` | Path to the input JSON trace file.                                                                            |
| `-b`, `--bound`     | `25`                  | Bound on the length of traces to check.                                                                       |
| `-o`, `--output`    | `output.smt2`         | Path to write the SMT-LIB output.                                                                             |
| `--no-concrete`     | _(off)_               | Emit symbolic (over-approximated) constraints instead of binding concrete observed values to states.          |
| `--array-mode`      | `truncate`            | How to flatten arrays in the trace: `truncate`, `crossproduct`, or `intact`.                                  |
| `--max-list-items`  | `2`                   | Max number of items to keep when flattening lists.                                                            |
| `--margin`          | `0.15`                | Fractional margin added to observed variable ranges.                                                          |
| `--standard-checks` | _(off)_               | Run the built-in standard checks (safety, monotonicity, liveness, reachability) instead of the example check. |

#### Array modes

Traces often contain arrays (e.g. a list of detected objects per frame). `--array-mode` controls how they are flattened:

- `truncate` — keep the first `--max-list-items` elements of each array (arrays are preserved but capped).
- `crossproduct` — expand the trace by taking the cross product of array elements (can lead to state explosion; each array is reduced to a single element per branch).
- `intact` — keep arrays as tuples in the flattened trace.

### Examples

```
# Export the default example with a shorter bound
python main.py ./example/test.json --bound 10

# Run the full battery of standard checks and write to a custom file
python main.py ./example/test.json --standard-checks --output checks.smt2

# Emit symbolic (non-concrete) constraints and keep arrays intact
python main.py ./example/test.json --no-concrete --array-mode intact
```

## Output

The generated `.smt2` file is a self-contained SMT-LIB script using the `QF_LRA` logic (quantifier-free linear real arithmetic). It declares the path variables, asserts the transition/path constraints, and ends with `(check-sat)` / `(get-model)`, so it can be run directly with any compatible solver:

```
z3 output.smt2
```
