"""Build a function-to-tests map using only the clean upstream revision."""
import ast
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import tokenize
import unittest
from contextlib import redirect_stdout
from multiprocessing.util import Finalize
from pathlib import Path

REV = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

def trace_pool_worker(directory, root, initializer, initargs):
    """Record SQLGlot calls and fixture opens in a process pool worker."""
    visited = set()
    opened = set()
    monitor = sys.monitoring
    tool = 3
    monitor.use_tool_id(tool, "blast-radius-worker")

    def entered(code, offset):
        path = os.path.normcase(code.co_filename)
        if path.startswith(root):
            visited.add((path, code.co_firstlineno))
        return monitor.DISABLE

    def record_open(event, args):
        if event == "open" and isinstance(args[0], str):
            path = os.path.normcase(os.path.abspath(args[0]))
            if path.startswith(root):
                opened.add(path)

    def write_trace():
        monitor.set_events(tool, 0)
        output = Path(directory) / f"{os.getpid()}.json"
        output.write_text(json.dumps([sorted(visited), sorted(opened)]), encoding="utf-8")

    Finalize(None, write_trace, exitpriority=10)
    sys.addaudithook(record_open)
    monitor.register_callback(tool, monitor.events.PY_START, entered)
    monitor.set_events(tool, monitor.events.PY_START)
    if initializer is not None:
        initializer(*initargs)


def optimizer_fixture_readers(tests):
    # On the pinned revision, flat optimizer SQL fixtures are loaded by
    # TestOptimizer.check_file or by direct loader calls in its test methods.
    # Executor class fixtures live in subdirectories and use the dynamic map.
    path = Path("tests/test_optimizer.py")
    numbers = {test["id"]: i for i, test in enumerate(tests)}
    readers = {}
    tree = ast.parse(path.read_bytes())
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for method in cls.body:
            if not isinstance(method, ast.FunctionDef) or not method.name.startswith("test_"):
                continue
            index = numbers.get(f"{path.as_posix()}::{cls.name}::{method.name}")
            if index is None:
                continue
            for call in (node for node in ast.walk(method) if isinstance(node, ast.Call)):
                func = call.func
                if isinstance(func, ast.Attribute) and func.attr == "check_file":
                    template = "optimizer/{}.sql"
                elif isinstance(func, ast.Name) and func.id in ("load_sql_fixtures", "load_sql_fixture_pairs"):
                    template = "{}"
                else:
                    continue
                if not call.args or not isinstance(call.args[0], ast.Constant):
                    return {}
                name = Path("tests/fixtures") / template.format(call.args[0].value)
                if name.parent == Path("tests/fixtures/optimizer") and name.is_file():
                    readers.setdefault(name.as_posix(), set()).add(index)
    return {name: sorted(indices) for name, indices in readers.items()}


def protected_insertions(path):
    spans = []
    strings = []
    with path.open("rb") as stream:
        for token in tokenize.tokenize(stream.readline):
            kind = tokenize.tok_name[token.type]
            if token.start[0] < token.end[0]:
                spans.append((token.start[0], token.end[0]))
            if kind in ("FSTRING_START", "TSTRING_START"):
                strings.append(token.start[0])
            elif kind in ("FSTRING_END", "TSTRING_END"):
                spans.append((strings.pop(), token.end[0]))
    # A comment on the next line can interrupt an explicit line continuation.
    spans.extend([n, n + 1] for n, line in enumerate(path.read_bytes().splitlines(), 1)
                 if line.endswith(b"\\"))
    mask = 0
    for first, end in spans:
        mask |= ((1 << (end - first)) - 1) << first
    return format(mask, "x")


def main(root, catalog, destination):
    root = root.resolve()
    if subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() != REV:
        raise ValueError("Expected the contest revision")
    tests = json.loads(catalog.read_text())["tests"]
    destination = destination.resolve()
    os.chdir(root)
    sys.path.insert(0, str(root))
    paths = {}
    ranges = {}
    for directory in ("tests", "sqlglot"):
        for path in Path(directory).rglob("*.py"):
            key = path.as_posix()
            paths[os.path.normcase(str(path.resolve()))] = key
            ranges[key] = {}
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    start = min([node.lineno] + [x.lineno for x in node.decorator_list])
                    # Restrict selection to edits strictly inside a multi-line body.
                    ranges[key][start] = [node.body[0].lineno, node.end_lineno, []]
                elif isinstance(node, ast.Lambda) and node.end_lineno - node.lineno >= 2:
                    # The first and last lines can also contain code evaluated
                    # by the enclosing declaration or dispatch table.
                    ranges[key].setdefault(
                        node.lineno, [node.lineno + 1, node.end_lineno - 1, []],
                    )

    visited = set()
    fixture_paths = {os.path.normcase(str(p.resolve())): p.as_posix()
                     for p in Path("tests/fixtures").rglob("*.sql")}
    opened = set()
    fixtures = {key: [] for key in fixture_paths.values()}
    subprocess_used = False

    def record_open(event, args):
        nonlocal subprocess_used
        if event == "subprocess.Popen":
            subprocess_used = True
        if event == "open" and isinstance(args[0], str):
            key = fixture_paths.get(os.path.normcase(os.path.abspath(args[0])))
            if key:
                opened.add(key)

    sys.addaudithook(record_open)
    unknown = []
    monitor = sys.monitoring
    tool = 3
    monitor.use_tool_id(tool, "blast-radius")

    def entered(code, offset):
        key = paths.get(os.path.normcase(code.co_filename))
        if key and code.co_firstlineno in ranges[key]:
            visited.add((key, code.co_firstlineno))
        return monitor.DISABLE

    monitor.register_callback(tool, monitor.events.PY_START, entered)
    trace_directory = None
    pool_used = False
    original_pool = concurrent.futures.ProcessPoolExecutor

    class TracedPool(original_pool):
        def __init__(self, *args, initializer=None, initargs=(), **kwargs):
            if not args and kwargs.get("max_workers") is None:
                kwargs["max_workers"] = 4
            super().__init__(*args, initializer=trace_pool_worker, initargs=(
                trace_directory, os.path.normcase(str(root) + os.sep), initializer, initargs,
            ), **kwargs)

        def submit(self, fn, /, *args, **kwargs):
            nonlocal pool_used
            pool_used = True
            return super().submit(fn, *args, **kwargs)

    concurrent.futures.ProcessPoolExecutor = TracedPool
    # Run each item with a fresh trace directory so workers from one item
    # cannot contribute coverage to a different item.
    for index, test in enumerate(tests):
        parts = test["id"].split("::")
        name = ".".join([parts[0][:-3].replace("/", ".")] + parts[1:])
        visited.clear()
        opened.clear()
        pool_used = False
        subprocess_used = False
        try:
            with tempfile.TemporaryDirectory() as trace_directory:
                suite = unittest.defaultTestLoader.loadTestsFromName(name)
                result = unittest.TestResult()
                monitor.restart_events()
                monitor.set_events(tool, monitor.events.PY_START)
                with redirect_stdout(sys.stderr):
                    suite.run(result)
                monitor.set_events(tool, 0)
                trace_files = list(Path(trace_directory).glob("*.json"))
                for path in trace_files:
                    starts, reads = json.loads(path.read_text(encoding="utf-8"))
                    for filename, start in starts:
                        key = paths.get(filename)
                        if key and start in ranges[key]:
                            visited.add((key, start))
                    opened.update(fixture_paths[key] for key in reads if key in fixture_paths)
                ok = (result.testsRun == 1 and result.wasSuccessful() and not result.skipped
                      and not subprocess_used and (not pool_used or bool(trace_files)))
        except Exception:
            ok = False
        finally:
            monitor.set_events(tool, 0)
        if ok:
            for key in opened:
                fixtures[key].append(index)
            for key, start in visited:
                ranges[key][start][2].append(index)
        else:
            unknown.append(index)
        if (index + 1) % 200 == 0:
            print(f"Measured {index + 1}/{len(tests)} catalog entries", file=sys.stderr)
    concurrent.futures.ProcessPoolExecutor = original_pool
    for entries in ranges.values():
        for span in entries.values():
            indices = span[2]
            if len(indices) >= 64:
                mask = sum(1 << index for index in indices)
                span[2] = format(mask, "x")
    index_data = {key: list(entries.values()) for key, entries in ranges.items()}
    destination.write_text(json.dumps({"ids": [t["id"] for t in tests], "unknown": unknown,
                                      "files": index_data, "fixtures": fixtures,
                                      "continued": {key: protected_insertions(Path(key)) for key in ranges},
                                      "optimizer_readers": optimizer_fixture_readers(tests)},
                                     separators=(",", ":")))
    print(f"{len(tests)} tests, {len(unknown)} unmeasured", file=sys.stderr)


if __name__ == "__main__":
    main(*(Path(p) for p in sys.argv[1:]))
