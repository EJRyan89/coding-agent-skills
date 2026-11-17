"""The one command-line type for a time limit in seconds.

`float()` reads `nan`, `inf`, `infinity`, and a number too large for a float as infinity, and none of those, zero, or
a negative number is a limit: handed to `run_bounded`, `nan` and `inf` raise, while zero and a negative number end
every command at once as timed out. An option that takes a number of seconds as a decimal uses `positive_seconds` as
its argparse `type`, so it refuses them with a usage error naming the value, while one that takes whole seconds may
use `whole_number.argument` with a lower bound of one instead; validation refuses a `type=float` on an option that
names a time.
"""

from __future__ import annotations

import argparse
import math


def positive_seconds(value: str) -> float:
    """`value` as a finite number of seconds above zero, or an argparse usage error naming it."""
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not 0 < number < math.inf:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, not {value}")
    return number
