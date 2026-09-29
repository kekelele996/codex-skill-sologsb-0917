#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from difficulty_gate import validate_difficulty_audit  # noqa: E402


PROMPT = "批次保存要同时写入版片表和编排台。两个标签页同时提交时只能有一个成功，另一个要拿到冲突结果。"


class DifficultyGateTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        origin = root / "source" / "origin"
        for relative in (
            "app/service.py",
            "app/repository.py",
            "web/batch.ts",
        ):
            path = origin / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        return origin

    def _audit(self, **overrides):
        payload = {
            "schemaVersion": 1,
            "difficulty": "困难",
            "summary": "题目同时要求跨服务、持久化和页面编排完成后才能成立，并且并发冲突会让其中一侧读到过期结果。",
            "axes": [
                {
                    "kind": "multi_module",
                    "promptQuote": "批次保存要同时写入版片表和编排台",
                    "sourceAnchors": ["app/service.py", "app/repository.py", "web/batch.ts"],
                    "whyNotStraightforward": "页面、事务和数据读取需要按同一份冲突规则协同，任何一个环节单独改动都不能形成完整结果。",
                    "observableFailure": "只改页面会导致批次写入失败，只改事务会让编排台继续显示旧状态。",
                },
                {
                    "kind": "concurrency",
                    "promptQuote": "两个标签页同时提交时只能有一个成功",
                    "sourceAnchors": ["app/service.py", "app/repository.py"],
                    "whyNotStraightforward": "两个写入者可能同时看到相同的旧值，必须让数据库状态、错误返回和页面显示保持一致。",
                    "observableFailure": "并发提交可能覆盖先写入的数据，导致记录丢失或状态不一致。",
                },
            ],
            "mediumCounterexample": "如果题目只是给一个表单增加必填校验并显示提示，所有规则都能顺序实现，那属于中等题。",
            "reviewedBy": "Codex",
        }
        payload.update(overrides)
        return payload

    def test_valid_hard_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._fixture(root)
            path = root / "audit.json"
            path.write_text(json.dumps(self._audit(), ensure_ascii=False), encoding="utf-8")
            result = validate_difficulty_audit(path, task_root=root, difficulty="困难", prompt_text=PROMPT)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["axisKinds"], ["concurrency", "multi_module"])
            self.assertGreaterEqual(result["anchorCount"], 3)

    def test_single_axis_is_medium(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._fixture(root)
            payload = self._audit()
            payload["axes"] = payload["axes"][:1]
            path = root / "audit.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = validate_difficulty_audit(path, task_root=root, difficulty="困难", prompt_text=PROMPT)
            self.assertFalse(result["ok"])
            self.assertTrue(any("至少需要两个" in item for item in result["errors"]))

    def test_missing_prompt_quote_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._fixture(root)
            payload = self._audit()
            payload["axes"][0]["promptQuote"] = "提示词里不存在的困难说明"
            path = root / "audit.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = validate_difficulty_audit(path, task_root=root, difficulty="困难", prompt_text=PROMPT)
            self.assertFalse(result["ok"])
            self.assertTrue(any("未在最终提示词" in item for item in result["errors"]))

    def test_three_real_anchors_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._fixture(root)
            payload = self._audit()
            payload["axes"][0]["sourceAnchors"] = ["app/service.py"]
            payload["axes"][1]["sourceAnchors"] = ["app/repository.py"]
            path = root / "audit.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            result = validate_difficulty_audit(path, task_root=root, difficulty="困难", prompt_text=PROMPT)
            self.assertFalse(result["ok"])
            self.assertTrue(any("三个不同源码锚点" in item for item in result["errors"]))


if __name__ == "__main__":
    unittest.main()
