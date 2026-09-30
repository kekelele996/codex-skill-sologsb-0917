#!/usr/bin/env python3
"""Run the local sologsb-0917 test suite and CLI smoke checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _isolate_device_config() -> tempfile.TemporaryDirectory:
    """与 tests/conftest.py 一致：测试不读本机真实设备配置（本机可能改过 A/B 模型）。"""
    holder = tempfile.TemporaryDirectory(prefix="sologsb-selftest-")
    path = Path(holder.name) / "device-config.json"
    # 其余字段（代理、Base URL 等）沿用本机配置，只把 A/B 模型换成测试基线。
    source = Path(os.environ.get("SOLOSB_CONFIG") or Path.home() / ".codex" / "sologsb" / "config.json").expanduser()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("configVersion", 1)
    claude = data.setdefault("claude", {})
    claude.update({"modelA": "auto_model/urm", "modelB": "ark/urm-03"})
    path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(path, 0o600)
    os.environ["SOLOSB_CONFIG"] = str(path)
    for name in ("SOLOSB_A_MODEL", "SOLOSB_B_MODEL"):
        os.environ.pop(name, None)
    return holder


def main() -> int:
    holder = _isolate_device_config()
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
    holder.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
