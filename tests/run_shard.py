"""Run one shard of a unittest suite: python -B tests/run_shard.py <suite> <index> <count>.

tests/run_validation.py splits large suites into shards, each its own process, so one long suite does not set the
length of the whole run. A shard runs the suite's tests whose position, in test-ID order, is <index> modulo <count>,
so the shards of a suite run each of its tests exactly once. Module and class fixtures run in every shard that has
one of their tests.

It imports nothing from the repository and puts the suite's own directory first on sys.path, as `python <suite>`
does, so a suite that passes as shards also passes on its own.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: run_shard.py <suite> <index> <count>", file=sys.stderr)
        return 2
    path = Path(argv[0]).resolve()
    index, count = int(argv[1]), int(argv[2])
    if not 0 <= index < count:
        print(f"shard index {index} is outside 0..{count - 1}", file=sys.stderr)
        return 2
    sys.path[0] = str(path.parent)
    # Any name but __main__, so the suite's own `if __name__ == "__main__":` block does not run all its tests.
    name = "validation_shard_" + re.sub(r"\W", "_", path.stem)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        print(f"{path} cannot be loaded as a Python module", file=sys.stderr)
        return 2
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    tests = sorted(flatten(unittest.defaultTestLoader.loadTestsFromModule(module)), key=lambda test: test.id())
    selected = [test for position, test in enumerate(tests) if position % count == index]
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
