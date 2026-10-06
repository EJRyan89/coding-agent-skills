from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_process import ProcessStatus, process_status, same_process, start_detached

# Prints a line, then sleeps until it is terminated, so its liveness can be probed from outside.
SLEEPER = (
    "import sys, time; print('host started', flush=True); "
    "print('to stderr', file=sys.stderr, flush=True); time.sleep(120)"
)


def terminate(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)


def wait_until(condition, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class SameProcessTests(unittest.TestCase):
    """The deployer lock's rule: a live PID is the recorded process only when its start time matches."""

    def test_identity_follows_pid_and_start_time(self) -> None:
        self.assertIs(False, same_process(ProcessStatus(False, None), 100))
        self.assertIs(True, same_process(ProcessStatus(True, 100), 100))
        self.assertIs(False, same_process(ProcessStatus(True, 200), 100), "a reused PID is another process")
        self.assertIsNone(same_process(ProcessStatus(True, None), 100), "an unreadable start time proves nothing")
        self.assertIsNone(same_process(ProcessStatus(True, 100), None), "nothing was recorded to compare")


class ProcessStatusTests(unittest.TestCase):
    def test_this_process_is_alive_with_a_start_time(self) -> None:
        status = process_status(os.getpid())
        self.assertTrue(status.alive)
        self.assertIsInstance(status.start_time, int)
        self.assertEqual(status, process_status(os.getpid()), "the start time is stable")

    def test_an_exited_process_is_not_alive(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        # The Popen handle keeps the exited process queryable, so this reads its exit code, not a missing PID.
        self.assertFalse(process_status(process.pid).alive)


class StartDetachedTests(unittest.TestCase):
    def test_a_detached_host_outlives_its_start_and_logs_its_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run with spaces"
            root.mkdir()
            log = root / "host.log"
            pid = start_detached([sys.executable, "-c", SLEEPER], root, log)
            self.addCleanup(terminate, pid)
            status = process_status(pid)
            self.assertTrue(status.alive)
            assert isinstance(status.start_time, int), "a live process reports its start time"
            self.assertTrue(wait_until(lambda: "to stderr" in log.read_text(encoding="utf-8", errors="replace")))
            text = log.read_text(encoding="utf-8", errors="replace")
            self.assertIn("host started", text)
            self.assertTrue(same_process(process_status(pid), status.start_time))
            self.assertFalse(same_process(process_status(pid), status.start_time + 1), "PID reuse reads as gone")
            terminate(pid)
            self.assertTrue(wait_until(lambda: not process_status(pid).alive))


if __name__ == "__main__":
    unittest.main()
