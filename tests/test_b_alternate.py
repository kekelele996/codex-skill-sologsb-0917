"""B 侧模型交替（开关）：先跑两次 auto_model/urm，之后与 ark/urm-03 交替。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_audit  # noqa: E402
import side_runner  # noqa: E402
from common import SologsbError, sha256_file, write_json  # noqa: E402


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "sologsb_submit_preflight_alt", ROOT / "submission" / "scripts" / "preflight.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class AlternateSwitchTests(unittest.TestCase):
    def test_switch_comes_from_the_device_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            on = Path(temp) / "on.json"
            on.write_text(json.dumps({"claude": {"bAlternateEnabled": "1"}}), encoding="utf-8")
            legacy = Path(temp) / "legacy.json"
            legacy.write_text(json.dumps({"claude": {"bFallbackEnabled": "1"}}), encoding="utf-8")
            off = Path(temp) / "off.json"
            off.write_text(json.dumps({"claude": {"modelA": "a/x", "modelB": "b/y"}}), encoding="utf-8")
            cleared = {"SOLOSB_B_ALTERNATE_ENABLED": "", "SOLOSB_B_FALLBACK_ENABLED": ""}
            with mock.patch.dict(os.environ, cleared, clear=False):
                self.assertTrue(model_audit.resolve_b_alternate(on)["enabled"])
                # 早先一版的字段名继续兜底，旧设备配置不会被静默忽略。
                self.assertTrue(model_audit.resolve_b_alternate(legacy)["enabled"])
                self.assertFalse(model_audit.resolve_b_alternate(off)["enabled"])

    def test_first_two_attempts_use_the_a_side_model_then_alternate(self) -> None:
        on, off = {"enabled": True}, {"enabled": False}
        picks = [model_audit.alternating_uses_a_model(on, n) for n in range(1, 7)]
        self.assertEqual(picks, [True, True, False, True, False, True])
        self.assertFalse(any(model_audit.alternating_uses_a_model(off, n) for n in range(1, 7)))
        self.assertFalse(model_audit.alternating_uses_a_model(on, 0))

    def test_only_side_b_switches_models(self) -> None:
        plan = {
            "A": {"model": "auto_model/urm"},
            "B": {"model": "ark/urm-03"},
            "bAlternate": {"enabled": True},
        }
        b_side = [side_runner.model_for_attempt("candidate-2", "B", plan, n, 6) for n in range(1, 7)]
        self.assertEqual([model for model, _ in b_side],
                         ["auto_model/urm", "auto_model/urm", "ark/urm-03",
                          "auto_model/urm", "ark/urm-03", "auto_model/urm"])
        self.assertEqual([flag for _, flag in b_side],
                         [True, True, False, True, False, True])
        self.assertEqual(
            [side_runner.model_for_attempt("candidate-1", "A", plan, n, 6) for n in range(1, 7)],
            [("auto_model/urm", False)] * 6,
        )

    def test_plan_without_the_switch_never_switches_models(self) -> None:
        plan = {"A": {"model": "auto_model/urm"}, "B": {"model": "ark/urm-03"}}
        self.assertEqual(
            [side_runner.model_for_attempt("candidate-2", "B", plan, n, 6) for n in range(1, 7)],
            [("ark/urm-03", False)] * 6,
        )

    def test_same_model_on_both_sides_has_nothing_to_alternate(self) -> None:
        plan = {
            "A": {"model": "same/x"},
            "B": {"model": "same/x"},
            "bAlternate": {"enabled": True},
        }
        self.assertEqual(side_runner.model_for_attempt("candidate-2", "B", plan, 2, 6), ("same/x", False))

    def test_locked_plan_refuses_a_changed_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            write_json(root / "monitor" / "state.json", {
                "status": "prompt_ready",
                "modelPlan": {
                    "A": {"candidateId": "candidate-1", "model": "auto_model/urm", "source": "config"},
                    "B": {"candidateId": "candidate-2", "model": "ark/urm-03", "source": "config"},
                    "bAlternate": {"enabled": False},
                    "lockedAt": "2026-10-03T00:00:00Z",
                },
            })
            state = json.loads((root / "monitor" / "state.json").read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"SOLOSB_B_ALTERNATE_ENABLED": "1"}, clear=False):
                with self.assertRaisesRegex(SologsbError, "交替开关"):
                    side_runner.lock_model_plan(state)


class PairAlternateRecordTests(unittest.TestCase):
    """交替跑出来的那次模型就是交付模型，表单按它如实填写。"""

    def _run_pair(self, root: Path, b_result_extra: dict) -> dict:
        (root / "monitor").mkdir(parents=True, exist_ok=True)
        origin = root / "source" / "origin"
        origin.mkdir(parents=True)
        (origin / "README.md").write_text("base", encoding="utf-8")
        prompt = root / "prompt.txt"
        prompt.write_text("prompt", encoding="utf-8")
        write_json(root / "monitor" / "state.json", {
            "status": "prompt_ready",
            "taskName": "b-alternate",
            "promptPath": str(prompt),
            "promptSha256": sha256_file(prompt),
        })

        def fake_candidate(task_root, candidate, **kwargs):
            workspace = task_root / "source" / "candidates" / candidate
            self.assertTrue((workspace / ".git").is_dir())
            (workspace / "result.txt").write_text(candidate, encoding="utf-8")
            if candidate == "candidate-1":
                time.sleep(0.05)
            trace = task_root / "workspace" / "轨迹文件" / "candidates" / candidate / f"{candidate}.jsonl"
            trace.parent.mkdir(parents=True, exist_ok=True)
            session = f"s-{candidate}"
            events = [
                {"type": "user", "uuid": "u", "sessionId": session, "message": {"role": "user", "content": "prompt"}},
                {"type": "assistant", "uuid": "a", "sessionId": session,
                 "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "完成"}]}},
            ]
            trace.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")
            record = {
                "candidateId": candidate,
                "attempt": 1,
                "status": "staged",
                "sessionId": session,
                "candidateTracePath": str(trace),
                "tracePath": str(trace),
                "changedFiles": ["result.txt"],
                "diffStat": "",
            }
            if candidate == "candidate-2":
                record.update(b_result_extra)
            return record

        with mock.patch.object(side_runner, "_ensure_image", return_value="image"):
            with mock.patch.object(side_runner, "_run_candidate_locked", side_effect=fake_candidate):
                with mock.patch.dict(os.environ, {"SOLOSB_ANTHROPIC_BASE_URL": "https://llm.example"}, clear=False):
                    return side_runner.run_both(root, timeout=10, live=False, candidate_count=2)

    def test_alternated_attempt_model_is_the_delivered_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._run_pair(root, {
                "model": "auto_model/urm",
                "modelname": "auto_model/urm",
                "plannedModel": "ark/urm-03",
                "modelAlternated": True,
                "attemptsAllowed": 6,
            })
            self.assertEqual(result["candidateMapping"]["B"]["model"], "auto_model/urm")
            self.assertEqual(result["candidateMapping"]["B"]["plannedModel"], "ark/urm-03")
            self.assertTrue(result["candidateMapping"]["B"]["modelAlternated"])
            state = json.loads((root / "monitor" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["sides"]["B"]["model"], "auto_model/urm")
            self.assertTrue(state["sides"]["B"]["modelAlternated"])
            # A 侧不交替，只有计划的模型。
            self.assertEqual(state["sides"]["A"]["model"], "auto_model/urm")
            self.assertFalse(state["sides"]["A"].get("modelAlternated"))

    def test_own_model_attempt_is_recorded_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = self._run_pair(root, {
                "model": "ark/urm-03",
                "modelname": "ark/urm-03",
                "plannedModel": "ark/urm-03",
                "modelAlternated": False,
            })
            self.assertEqual(result["candidateMapping"]["B"]["model"], "ark/urm-03")
            self.assertFalse(result["candidateMapping"]["B"]["modelAlternated"])


class RetryLoopAlternateTests(unittest.TestCase):
    """重试循环里 B 侧两个模型交替出现，A 侧始终只跑自己的模型。"""

    def _task(self, root: Path, side: str) -> None:
        (root / "monitor").mkdir(parents=True)
        write_json(root / "monitor" / "state.json", {
            "status": "prompt_ready",
            "initialSnapshot": "a" * 40,
            "modelPlan": {
                "A": {"candidateId": "candidate-1", "model": "auto_model/urm", "source": "config"},
                "B": {"candidateId": "candidate-2", "model": "ark/urm-03", "source": "config"},
                "bAlternate": {"enabled": True},
                "lockedAt": "2026-10-03T00:00:00Z",
            },
            "candidates": {f"candidate-{1 if side == 'A' else 2}": {
                "candidateId": f"candidate-{1 if side == 'A' else 2}", "status": "idle",
            }},
        })

    def test_b_side_alternates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._task(root, "B")
            seen: list[str] = []

            def fake_attempt(**kwargs):
                seen.append(kwargs["model_override"])
                return {"candidateId": "candidate-2", "attempt": kwargs["attempt"],
                        "status": "attempt_invalid", "error": f"invalid-{kwargs['attempt']}"}

            with mock.patch.dict(os.environ, {"SOLOGBS_ATTEMPT_BACKOFF_SECONDS": "0"}, clear=False):
                with mock.patch.object(side_runner, "_run_candidate_attempt", side_effect=fake_attempt):
                    with mock.patch.object(side_runner, "_remove_container"):
                        with self.assertRaises(SologsbError):
                            side_runner._run_candidate_locked(
                                root, "candidate-2", attempts=6, live=False, mapped_side="B"
                            )
            self.assertEqual(
                seen,
                ["auto_model/urm", "auto_model/urm", "ark/urm-03",
                 "auto_model/urm", "ark/urm-03", "auto_model/urm"],
            )

    def test_a_side_never_alternates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._task(root, "A")
            seen: list[str] = []

            def fake_attempt(**kwargs):
                seen.append(kwargs["model_override"])
                return {"candidateId": "candidate-1", "attempt": kwargs["attempt"],
                        "status": "attempt_invalid", "error": "invalid"}

            with mock.patch.dict(os.environ, {"SOLOGBS_ATTEMPT_BACKOFF_SECONDS": "0"}, clear=False):
                with mock.patch.object(side_runner, "_run_candidate_attempt", side_effect=fake_attempt):
                    with mock.patch.object(side_runner, "_remove_container"):
                        with self.assertRaises(SologsbError):
                            side_runner._run_candidate_locked(
                                root, "candidate-1", attempts=4, live=False, mapped_side="A"
                            )
            self.assertEqual(seen, ["auto_model/urm"] * 4)


class PreflightAlternateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.preflight = _load_preflight()

    @staticmethod
    def _state(attempt: int, model: str, *, enabled: bool = True, alternated: bool = False) -> dict:
        return {
            "modelPlan": {
                "A": {"candidateId": "candidate-1", "model": "auto_model/urm"},
                "B": {"candidateId": "candidate-2", "model": "ark/urm-03"},
                "bAlternate": {"enabled": enabled},
                "lockedAt": "2026-10-03T00:00:00Z",
            },
            "sides": {"B": {"model": model, "modelAlternated": alternated,
                            "attempt": attempt, "attemptsAllowed": 6}},
        }

    def _check(self, **kwargs):
        return self.preflight.b_alternate_evidence_check(self._state(**kwargs))

    def test_second_attempt_on_the_a_model_passes(self) -> None:
        outcome = self._check(attempt=2, model="auto_model/urm", alternated=True)
        self.assertTrue(outcome["ok"], outcome["message"])
        self.assertTrue(outcome["evidence"]["alternated"])
        self.assertIn("第 2 次尝试交替换跑 A 侧模型", outcome["message"])

    def test_first_attempt_on_the_a_model_passes(self) -> None:
        outcome = self._check(attempt=1, model="auto_model/urm", alternated=True)
        self.assertTrue(outcome["ok"], outcome["message"])
        self.assertTrue(outcome["evidence"]["alternated"])

    def test_third_attempt_on_the_own_model_passes(self) -> None:
        outcome = self._check(attempt=3, model="ark/urm-03")
        self.assertTrue(outcome["ok"], outcome["message"])
        self.assertFalse(outcome["evidence"]["alternated"])

    def test_third_attempt_on_the_a_model_is_blocked(self) -> None:
        outcome = self._check(attempt=3, model="auto_model/urm", alternated=True)
        self.assertFalse(outcome["ok"])
        self.assertIn("第 3 次尝试应跑的模型 ark/urm-03", outcome["message"])

    def test_with_the_switch_off_only_the_own_model_is_allowed(self) -> None:
        outcome = self._check(attempt=1, model="auto_model/urm", enabled=False, alternated=True)
        self.assertFalse(outcome["ok"])
        self.assertIn("开关已关闭", outcome["message"])

    def test_delivery_evidence_accepts_the_alternated_model(self) -> None:
        plan = self._state(attempt=2, model="auto_model/urm", alternated=True)["modelPlan"]
        expected, alternated = self.preflight.alternating_expected_model(plan, "B", 1)
        self.assertEqual((expected, alternated), ("auto_model/urm", True))
        expected, alternated = self.preflight.alternating_expected_model(plan, "B", 2)
        self.assertEqual((expected, alternated), ("auto_model/urm", True))
        expected, alternated = self.preflight.alternating_expected_model(plan, "B", 3)
        self.assertEqual((expected, alternated), ("ark/urm-03", False))
        expected, alternated = self.preflight.alternating_expected_model(plan, "B", 5)
        self.assertEqual((expected, alternated), ("ark/urm-03", False))
        expected, alternated = self.preflight.alternating_expected_model(plan, "A", 2)
        self.assertEqual((expected, alternated), ("auto_model/urm", False))


if __name__ == "__main__":
    unittest.main()
