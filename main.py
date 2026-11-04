from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import redirect_stdout
import csv
import glob
from io import StringIO
import os
from pathlib import Path
import resource
import sys
import time

from trace import ArrayMode, TraceOptions
from pipeline import from_json, run_standard_checks
def _process_trace(task: tuple):
    """Run one trace in an isolated process and return its captured report."""
    path, output, array_mode, max_list_items, bound_arg, property_mode, property_file = task
    report = StringIO()
    try:
        with redirect_stdout(report):
            opts = TraceOptions(
                array_mode=ArrayMode(array_mode),
                max_list_items=max_list_items,
            )
            load_start = time.perf_counter()
            ts = from_json(path, opts=opts)
            load_seconds = time.perf_counter() - load_start
            if property_mode:
                from property_checks import generate_property_files
                encode_start = time.perf_counter()
                rows = generate_property_files(ts, Path(output), property_mode)
                encode_seconds = time.perf_counter() - encode_start
                rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                peak_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
                for row in rows:
                    row["trace"] = str(Path(path))
                    row["load_profile_seconds"] = f"{load_seconds:.6f}"
                    row["precompute_encode_seconds"] = f"{float(row['precompute_encode_seconds']):.6f}"
                    row["total_construction_seconds"] = f"{load_seconds + encode_seconds:.6f}"
                    row["construction_peak_memory_mb"] = f"{peak_mb:.3f}"
                return path, rows, None
            if property_file:
                from custom_property import compile_property
                stats = compile_property(ts, Path(property_file), Path(output))
                return path, [stats], None
            bound = bound_arg if bound_arg is not None else ts.profile.num_steps - 1
            run_standard_checks(ts, bound=bound)
            ts.to_smtlib(output, bound=bound)
            print(f"done: wrote {output}")
        return path, report.getvalue(), None
    except Exception as exc:
        return path, report.getvalue(), f"{type(exc).__name__}: {exc}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Convert a generic JSON trace into a transition system, run checks, "
            "and export the concrete trace to SMT-LIB."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="./example/test.json",
        help="Input JSON trace, or directory/glob when --batch is set.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Process all JSON traces in the input directory or glob pattern.",
    )
    parser.add_argument(
        "--recursive", action="store_true",
        help="Search input directories recursively (used with --batch).",
    )
    parser.add_argument(
        "--property", choices=("prss", "distance", "all"), default=None,
        help="Encode an all-observed-timestamps PRSS and/or distance property query. "
             "SMT-LIB files are written under --output; batch mode creates one pair per trace.",
    )
    parser.add_argument(
        "--property-file", default=None, metavar="JSON",
        help="Compile a user-defined all-timestamp property from a JSON schema. "
             "Use --property or --property-file, not both.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=max(1, os.cpu_count() or 1),
        metavar="N",
        help="Maximum number of traces to process at once in batch mode "
             f"(default: {max(1, os.cpu_count() or 1)}).",
    )
    parser.add_argument(
        "-b", "--bound",
        type=int,
        default=None,
        help="Bound on the length of traces to check (default: full trace length).",
    )
    parser.add_argument(
        "-o", "--output",
        default="output.smt2",
        help="SMT-LIB output file, or output directory in batch mode (default: output.smt2).",
    )
    parser.add_argument(
        "--array-mode",
        choices=[mode.value for mode in ArrayMode],
        default=ArrayMode.TRUNCATE.value,
        help="How to flatten arrays in the trace (default: truncate).",
    )
    parser.add_argument(
        "--max-list-items",
        type=int,
        default=0,
        help="Max number of items to keep when flattening lists (0 = unbounded, keep all; default: 0).",
    )
    parser.add_argument(
        "--standard-checks",
        action="store_true",
        help="Run the built-in standard checks (safety, monotonicity, liveness, "
             "reachability) instead of the example speed check.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.property and args.property_file:
        raise SystemExit("--property and --property-file cannot be used together")
    if args.property_file:
        from custom_property import validate_property_file
        try:
            validate_property_file(Path(args.property_file))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    opts = TraceOptions(
        array_mode=ArrayMode(args.array_mode),
        max_list_items=args.max_list_items,
    )

    if args.batch:
        if args.jobs < 1:
            raise SystemExit("--jobs must be at least 1")
        source = Path(args.path)
        if source.is_dir():
            paths = sorted(source.rglob("*.json") if args.recursive else source.glob("*.json"))
        else:
            paths = [Path(p) for p in sorted(glob.glob(args.path))]
            if not paths and source.is_file():
                paths = [source]
        paths = [p for p in paths if p.is_file() and p.suffix.lower() == ".json"]
        if args.property or args.property_file:
            prefixes = ("trace_sim", "cutin_sim", "cutout_sim", "swerve_sim",
                        "uturn_sim", "deceleration_sim")
            paths = [p for p in paths if p.name.startswith(prefixes)]
        if not paths:
            raise SystemExit(f"No JSON trace files found for: {args.path}")

        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        tasks = []
        for trace_path in paths:
            if (args.property or args.property_file) and source.is_dir():
                relative = trace_path.relative_to(source).with_suffix("")
                safe_stem = "__".join(relative.parts)
                output_path = output_dir / (safe_stem if args.property else f"{safe_stem}.smt2")
            else:
                output_path = output_dir / f"{trace_path.stem}.smt2"
            tasks.append((str(trace_path), str(output_path), args.array_mode,
                          args.max_list_items, args.bound,
                          args.property, args.property_file))

        failures = []
        property_rows = []
        with ProcessPoolExecutor(max_workers=min(args.jobs, len(tasks))) as pool:
            pending = [pool.submit(_process_trace, task) for task in tasks]
            for future in as_completed(pending):
                path, report, error = future.result()
                if args.property or args.property_file:
                    if error:
                        failures.append(path)
                        print(f"ERROR processing {path}: {error}")
                    else:
                        property_rows.extend(report)
                        print(f"Generated {len(report)} property file(s) for {path}")
                    continue
                print(f"\n{'#' * 80}\nTrace: {path}\n{'#' * 80}")
                print(report, end="")
                if error:
                    failures.append(path)
                    print(f"ERROR processing {path}: {error}")
        if (args.property or args.property_file) and property_rows:
            fields = list(dict.fromkeys(key for row in property_rows for key in row))
            with (output_dir / "construction.csv").open("w", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows(property_rows)
            print(f"Wrote construction measurements and {len(property_rows)} property encodings under {output_dir}")
        if failures:
            raise SystemExit(f"{len(failures)} of {len(paths)} traces failed")
        return

    print(f"Loading: {args.path}")
    if args.property:
        from property_checks import generate_property_files
        ts = from_json(args.path, opts=opts)
        rows = generate_property_files(ts, Path(args.output).with_suffix(""), args.property)
        for row in rows:
            print(f"{row['property']}: checked={row['checked_steps']} missing={row['missing_steps']} "
                  f"query={row['smt_file']}")
        return
    if args.property_file:
        from custom_property import compile_property
        ts = from_json(args.path, opts=opts)
        stats = compile_property(ts, Path(args.property_file), Path(args.output))
        print(f"{stats['property']}: checked={stats['checked_steps']} "
              f"missing={stats['missing_steps']} non_finite={stats['non_finite_steps']} "
              f"violations={stats['violating_steps']} query={stats['smt_file']}")
        return
    ts = from_json(args.path, opts=opts)

    bound = args.bound if args.bound is not None else ts.profile.num_steps - 1

    if args.standard_checks:
        run_standard_checks(ts, bound=bound)
    ts.to_smtlib(args.output, bound=bound)
    print(f"done: wrote {args.output}")


if __name__ == "__main__":
    main()

# CVC
# s = x_0, x_1 ,....
# And(x_0 > 0, x_1 > 0...) - enough
# \forall i, x_[i] > 0
# Array (for comparison purpose)
# z3 : sort - type of types.

# does it speed up verification ? 
# x' - x > 10
# \exists i, t = i and t' = i + 1 -> cannot check for path. 
# comparison.
# look for encoding variations.

# documentation - high level (todo/ slides)
# workflow (diagram/walkthrough)
# toolchains (PySMT, z3 briefly)

# concrete examples
# precision of the perceived bus (predicted path)

# TOD
# how to make propositions for perception location adn so on.
# planning has collision with predict part. 
# based on the photo.
# delays
# fault tree analysis
# root cause analysis - keep track of time. 
# query language - to  bench capabilities of the encoding.

# symbolic value which captures the index.
# independent property.
# array - upper and lower bound for arrays.
# convert (/ a b) to reals.
# metadatas as constants.
# try cvc solver and other solvers.
# add number of clauses in the table, times and memory usage.


# two modes to add constraints in the encoding (trigger derived conversion or not).
# ghost variables - projection - 1. drop unrequired variables, 2. dervied variable by precomputing in python.
# can use python eval function
# keyword - partial assignment for smt solvers.
# verification using programming language
# fix implementation
# insight in case the implementation fails.
# think about soundness.
# accuracy of predict path with the range.
