"""Run one shard of a unittest suite: python -B tests/run_shard.py <suite> <index> <count> [<Class.test>=<seconds> ...].

tests/run_validation.py splits large suites into shards, each its own process, so one long suite does not set the
length of the whole run. Every shard deals the suite's tests out the same way: heaviest first, each onto the shard
with the least so far and the lowest index on a tie, where a test weighs its recorded seconds or else one. With
nothing recorded that is round-robin in test-ID order. So the shards of a suite run each of its tests exactly once,
and a recorded test runs on a shard of its own. Module and class fixtures run in every shard that has one of their
tests.

It imports nothing from the repository and puts the suite's own directory first on sys.path, as `python <suite>`
does, so a suite that passes as shards also passes on its own.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def deal(names: list[str], count: int, seconds: Mapping[str, float]) -> dict[str, int]:
    """Each test's shard: heaviest first onto the shard with the least so far, the lowest index on a tie."""
    loads = [0.0] * count
    shards: dict[str, int] = {}
    for name in sorted(names, key=lambda name: (-seconds.get(name, 1), name)):
        shard = min(range(count), key=lambda candidate: (loads[candidate], candidate))
        shards[name] = shard
        loads[shard] += seconds.get(name, 1)
    return shards


def recorded_seconds(arguments: list[str]) -> dict[str, float] | None:
    """The <Class.test>=<seconds> arguments, or None when one is malformed."""
    seconds: dict[str, float] = {}
    for argument in arguments:
        test, separator, value = argument.partition("=")
        try:
            seconds[test] = float(value)
        except ValueError:
            return None
        if not separator or seconds[test] <= 0:
            return None
    return seconds


def main(argv: list[str]) -> int:
    seconds = recorded_seconds(argv[3:])
    if len(argv) < 3 or seconds is None:
        print("usage: run_shard.py <suite> <index> <count> [<Class.test>=<seconds> ...]", file=sys.stderr)
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
    loaded = flatten(unittest.defaultTestLoader.loadTestsFromModule(module))
    tests = {".".join(test.id().split(".")[-2:]): test for test in loaded}
    unknown = sorted(set(seconds) - set(tests))
    if unknown:
        print(f"{path.name} defines no test {', '.join(unknown)}", file=sys.stderr)
        return 2
    shards = deal(list(tests), count, seconds)
    selected = [tests[name] for name in sorted(tests) if shards[name] == index]
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1).run(unittest.TestSuite(selected))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
