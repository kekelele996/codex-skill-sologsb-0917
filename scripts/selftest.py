#!/usr/bin/env python3
"""Run the local sologsb-0917 test suite and CLI smoke checks."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        return 1
    for command in (
        [sys.executable, str(ROOT / "scripts" / "sologsb.py"), "--help"],
    ):
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            print(proc.stderr or proc.stdout, file=sys.stderr)
            return 1
    print("selftest: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
