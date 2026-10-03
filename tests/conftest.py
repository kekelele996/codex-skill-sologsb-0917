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
    # 本机真实设备配置里可能写着 B 侧回退开关；导入期注入的环境变量会留在整个会话里，
    # 必须逐项清掉，否则用例会跟着本机设置漂移。
    for name in (
        "SOLOSB_A_MODEL",
        "SOLOSB_B_MODEL",
        "SOLOSB_B_ALTERNATE_ENABLED",
        "SOLOSB_B_FALLBACK_ENABLED",
        "SOLOSB_B_FALLBACK_MODEL",
        "SOLOSB_B_FALLBACK_AFTER",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
