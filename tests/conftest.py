"""测试不得读本机真实设备配置：A/B 模型名以设备配置为准，本机改了模型就会让测试漂移。"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _isolated_device_config(tmp_path, monkeypatch):
    path = tmp_path / "device-config.json"
    path.write_text(json.dumps({
        "configVersion": 1,
        "claude": {"modelA": "auto_model/urm", "modelB": "ark/urm-03"},
    }), encoding="utf-8")
    monkeypatch.setenv("SOLOSB_CONFIG", str(path))
    for name in ("SOLOSB_A_MODEL", "SOLOSB_B_MODEL"):
        monkeypatch.delenv(name, raising=False)
    yield
