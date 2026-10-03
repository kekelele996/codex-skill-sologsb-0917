from __future__ import annotations

# ruff: noqa: E402

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import evidence as evidence_mod  # noqa: E402
import gsb_tools  # noqa: E402

PROMPT = "采集队按自己那套规则编现场编号，馆里另有一套馆藏号，交接时按队名加现场编号配对。"
SESSION = "11111111-2222-3333-4444-555555555555"
SESSION_B = "99999999-8888-7777-6666-555555555555"
MASKED_LINE = "src/pages/CollectPage.tsx(91,42): error TS1005: '=>' expected."


def _write_trace(path: Path, *, tool_result: str, session_id: str = SESSION) -> None:
    events = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": session_id,
            "message": {"role": "user", "content": PROMPT},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "npx tsc --noEmit 2>&1 | head -40"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": session_id,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": False,
                        "content": tool_result,
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "sessionId": session_id,
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "补全类型后构建与启动都通过。"}],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n", encoding="utf-8")


class MaskedFailureEvidenceTest(unittest.TestCase):
    def _evidence_doc(self, side: str, items: list[dict]) -> dict:
        return {
            "evidence": [
                *items,
                {
                    "id": f"{side}-artifact-snapshot",
                    "side": side,
                    "type": "artifact",
                    "polarity": "neutral",
                    "text": f"{side} 产物快照",
                    "artifact": {
                        "commit": "c" * 40,
                        "changedFiles": ["frontend/src/pages/CollectPage.tsx"],
                    },
                },
            ]
        }

    def test_pipe_masked_compile_failure_becomes_citable_product_evidence(self) -> None:
        """`tsc | head` 的退出码是 0，但产物文件上的编译错误仍要留成可引用的失败证据。"""
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "b.jsonl"
            _write_trace(trace, tool_result=MASKED_LINE)
            items, summary = evidence_mod.process_evidence("B", trace, PROMPT, SESSION)
        failures = [item for item in items if item["id"].startswith("B-process-error-")]
        self.assertEqual(len(failures), 1, items)
        self.assertEqual(failures[0]["polarity"], "negative")
        self.assertIn("CollectPage.tsx", failures[0]["trace"]["quote"])
        self.assertFalse(failures[0]["evaluationExcluded"])
        self.assertTrue(summary["toolErrors"][0]["maskedExitCode"])
        doc = self._evidence_doc("B", items)
        self.assertTrue(gsb_tools.delivery_deduction_citation_ok("B", failures[0], doc))

    def test_clean_tool_result_creates_no_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "a.jsonl"
            _write_trace(trace, tool_result="vite build 完成，产物写入 dist。", session_id=SESSION_B)
            items, summary = evidence_mod.process_evidence("A", trace, PROMPT, SESSION_B)
        self.assertEqual([item for item in items if item["id"].startswith("A-process-error-")], [])
        self.assertEqual(summary["toolErrors"], [])

    def test_plain_error_word_in_model_output_is_ignored(self) -> None:
        """模型自己提到 error 不代表产物失败，只有编译器/测试失败输出才算。"""
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "a2.jsonl"
            _write_trace(
                trace,
                tool_result="4 files changed, 12 insertions(+), 3 deletions(-)",
                session_id=SESSION_B,
            )
            items, _ = evidence_mod.process_evidence("A", trace, PROMPT, SESSION_B)
        self.assertEqual([item for item in items if item["id"].startswith("A-process-error-")], [])


if __name__ == "__main__":
    unittest.main()
