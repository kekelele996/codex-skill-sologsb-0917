from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import write_json  # noqa: E402
from prompt_tools import (  # noqa: E402
    MEDIUM_SHAPE_RE,
    validate_difficulty_review,
)

HISTORY_ITEMS = [
    {
        "id": 9001,
        "status": "已废弃",
        "qcHitRule": "G16",
        "userPrompt": "译制组改术语译名时，现在只能逐条找台词改。请在术语卡片里补批量换词：提交新译名后先预览会改多少条台词。",
    },
    {
        "id": 9002,
        "status": "质检通过",
        "qcHitRule": "",
        "userPrompt": "口述史素材公开前要遮掉受访者不愿披露的内容。片段正文或时间码一变，原有遮盖就失效并需重新确认。",
    },
]

# 平台 2026-09-29 打回的真实形态：在既有页面补一个对照视图 + 局部守卫，没有跨对象不变量。
MEDIUM_PROMPT = (
    "口述史项目把方言原音稿和普通话校订稿分放在两条轨上，校订员来回切换对时间，常改错段或动到原音。"
    "请补一个双轨对照台：同一时间轴并排显示两条轨，点一侧片段两侧一起定位，原音轨只读，校订轨可改。"
    "拆分或合并校订片段时，原音起止时间不受影响。两轨对不上的区间要标出来，各自没对齐的片段数也要可见。"
)

# 同项目当天通过的形态：跨对象一致性（遮盖范围随正文/时间码失效 + 重叠冲突）。
HARD_PROMPT = (
    "口述史素材公开前要遮掉受访者不愿披露的内容。校对员选中片段文字后划出范围，填写原因并确认；"
    "片段正文或时间码一变，原有遮盖就失效并需重新确认，两个遮盖范围重叠时拒绝保存并指出冲突。"
    "导出公开 SRT 时已确认范围显示已遮盖，普通导出和草稿保留原话。"
)

# 2026-09-29 真废弃样本 #19600：审阅回合本身包含导入、去重、冲突和修订，
# 但没有题面级并发、恢复、迁移、权限、容量或跨系统不变量，平台仍判中等。
INCREMENTAL_REVIEW_PROMPT = (
    "课程送教研组复核时，老师会带着离线批注回来，现有版本一改就说不清哪条意见已处理。"
    "请补审阅回合：把针对模块和步骤的意见贴进工具，逐条选择采纳或保留；采纳先形成候选修改，不覆盖冻结内容。"
    "同一意见重复导入只处理一次，步骤被移动、原步骤不存在或批注基于旧版本时，先列清冲突再决定。"
    "确认后从所选冻结版本生成新修订版，保留旧版和处理记录，重开仍能继续。"
)


def _base_review(prompt: str, *, with_evidence: bool = True, medium_defense: bool = True) -> dict:
    data = {
        "schemaVersion": 1,
        "difficulty": "困难",
        "signals": {
            "multiModule": {
                "passed": True,
                "modules": ["片段与轨道模型", "遮盖状态模块", "导出与持久化"],
                "crossModuleInvariant": "遮盖范围必须跟着片段正文和时间码一起失效，导出文件要和状态一致",
                "evidence": "片段、遮盖记录、公开导出和本地草稿读取同一份遮盖状态",
            },
            "designTradeoff": {"passed": False},
            "complexConcern": {
                "passed": True,
                "kinds": ["state-machine"],
                "technicalRisk": "正文或时间码变化后遮盖状态没有失效，会导出本应遮住的原话",
                "observableFailure": "公开导出里出现未遮盖的受访者原话",
                "evidence": "需求要求遮盖随片段内容变化而失效，重叠范围直接拒绝保存",
            },
        },
        "routineOnly": False,
        "verdict": "困难",
        "reviewedBy": "Codex",
    }
    if with_evidence:
        data["platformSignals"] = {
            "multiModule": {
                "answer": "片段模型、遮盖状态、公开导出三条既有链路必须同时对同一份遮盖状态负责",
                "promptQuote": "导出公开 SRT 时已确认范围显示已遮盖",
                "ifViolated": "导出文件会把不愿披露的原话写给外部单位",
            },
            "designTradeoff": {
                "answer": "遮盖是派生状态，必须选正文与时间码作为失效触发的事实来源，而不是保留确认结果",
                "promptQuote": "片段正文或时间码一变，原有遮盖就失效并需重新确认",
                "ifViolated": "旧确认结果继续生效，遮盖范围与当前文本对不上",
            },
            "complexConcern": {
                "answer": "遮盖状态机要处理失效传播和重叠冲突拒绝，属于状态与一致性关注点",
                "promptQuote": "两个遮盖范围重叠时拒绝保存并指出冲突",
                "ifViolated": "重叠范围被写入，导出时出现互相矛盾的遮盖区间",
            },
        }
        data["crossObjectInvariant"] = {
            "objects": ["片段", "遮盖范围"],
            "invariant": "遮盖范围必须始终落在当前片段文本与时间码之上，文本变化即失效",
            "divergenceFailure": "遮盖范围与片段对不上时，公开导出会露出应当遮住的原话",
        }
        data["corpusEvidence"] = {
            "nearestDiscardedId": 9001,
            "differenceFromDiscarded": "废弃样本只是在术语卡片上做预览式批量替换，没有状态失效链路；本题的遮盖范围由片段内容派生并会失效",
            "nearestPassedId": 9002,
            "borrowedComplexity": "借鉴通过样本里遮盖范围随片段变化失效、重叠范围拒绝保存的一致性设计",
        }
        data["substantiveComplexity"] = {
            "kinds": ["state-invalidation"],
            "promptQuote": "片段正文或时间码一变，原有遮盖就失效并需重新确认",
            "whyHard": "遮盖不是一次性写入，内容或时间码变化后派生状态必须重算并阻止旧确认继续生效",
            "visibleFailure": "旧遮盖被继续导出，不愿披露的原话会出现在公开文件里",
        }
    if medium_defense:
        data["mediumShapeDefense"] = (
            "题面确实是在既有编辑器上补一层遮盖，但遮盖状态由片段正文与时间码派生，"
            "正文一变就必须整体失效并重新确认，缺了这条导出就会露出原话"
        )
    return data


class DifficultyGateTests(unittest.TestCase):
    def _write(self, temp: str, review: dict, prompt: str) -> tuple[Path, Path]:
        base = Path(temp)
        prompt_path = base / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        review_path = base / "difficulty.json"
        write_json(review_path, review)
        history_path = base / "history.json"
        write_json(history_path, HISTORY_ITEMS)
        return review_path, prompt_path

    def test_medium_shape_is_detected(self) -> None:
        self.assertTrue(MEDIUM_SHAPE_RE.search(MEDIUM_PROMPT))
        self.assertFalse(MEDIUM_SHAPE_RE.search(HARD_PROMPT))

    def test_missing_history_cache_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review_path, prompt_path = self._write(temp, _base_review(HARD_PROMPT), HARD_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "missing-history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("缺少历史 GSB 缓存" in error for error in result["errors"]))

    def test_review_without_corpus_evidence_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review_path, prompt_path = self._write(
                temp, _base_review(HARD_PROMPT, with_evidence=False), HARD_PROMPT
            )
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            joined = " ".join(result["errors"])
            self.assertIn("platformSignals", joined)
            self.assertIn("crossObjectInvariant", joined)
            self.assertIn("corpusEvidence", joined)

    def test_quote_must_come_from_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review = _base_review(HARD_PROMPT)
            review["platformSignals"]["complexConcern"]["promptQuote"] = "这段文字根本不在题面里出现"
            review_path, prompt_path = self._write(temp, review, HARD_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("不是题面原句" in error for error in result["errors"]))

    def test_objects_must_appear_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review = _base_review(HARD_PROMPT)
            review["crossObjectInvariant"]["objects"] = ["片段", "结算单"]
            review_path, prompt_path = self._write(temp, review, HARD_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("没有出现在题面原文里" in error for error in result["errors"]))

    def test_discarded_id_must_be_real_g16_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review = _base_review(HARD_PROMPT)
            review["corpusEvidence"]["nearestDiscardedId"] = 9002
            review_path, prompt_path = self._write(temp, review, HARD_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("不是被废弃的样本" in error for error in result["errors"]))

    def test_medium_shape_requires_defense(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review = _base_review(HARD_PROMPT, medium_defense=False)
            review_path, prompt_path = self._write(temp, review, HARD_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=MEDIUM_PROMPT,
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("mediumShapeDefense" in error for error in result["errors"]))

    def test_incremental_review_shape_is_detected(self) -> None:
        self.assertIsNotNone(MEDIUM_SHAPE_RE.search(INCREMENTAL_REVIEW_PROMPT))

    def test_incremental_review_without_substantive_signal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review = _base_review(HARD_PROMPT)
            review["substantiveComplexity"] = {
                "kinds": ["migration"],
                "promptQuote": "确认后从所选冻结版本生成新修订版",
                "whyHard": "冻结版本和修订版看起来有历史关系，因此声称这是一条迁移链路",
                "visibleFailure": "错误时会生成重复修订版",
            }
            review_path, prompt_path = self._write(temp, review, INCREMENTAL_REVIEW_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            self.assertFalse(result["ok"])
            self.assertTrue(any("没有任何类型能由题面原句支撑" in error for error in result["errors"]))

    def test_complete_review_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            review_path, prompt_path = self._write(temp, _base_review(MEDIUM_PROMPT), MEDIUM_PROMPT)
            result = validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )
            # 题面里没有“片段”以外的第二个对象名，故意让这条失败，确保门禁真的在核对题面。
            self.assertFalse(result["ok"])
            self.assertTrue(any("没有出现在题面原文里" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()


class ModelSplitTests(unittest.TestCase):
    """2026-09-29：同模型的两侧不构成 Pair-wise 对比，必须在开跑前拦掉。"""

    @staticmethod
    def _plan(model_a: str, model_b: str) -> dict:
        return {"A": {"candidateId": "candidate-1", "model": model_a},
                "B": {"candidateId": "candidate-2", "model": model_b}}

    def test_same_model_is_rejected(self) -> None:
        import side_runner

        with self.assertRaisesRegex(Exception, "模型名相同"):
            side_runner.assert_model_split(self._plan("auto_model/urm", "auto_model/urm"))

    def test_different_models_pass(self) -> None:
        import side_runner

        side_runner.assert_model_split(self._plan("auto_model/urm", "ark/urm-03"))


class DifficultyTemplateScaffoldTests(unittest.TestCase):
    def test_scaffold_written_when_missing(self) -> None:
        import prompt_tools

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "难度论证.json"
            created = prompt_tools.ensure_difficulty_template(target)
            self.assertTrue(created)
            data = __import__("json").loads(target.read_text(encoding="utf-8"))
            for key in ("platformSignals", "crossObjectInvariant", "corpusEvidence", "substantiveComplexity"):
                self.assertIn(key, data)
            self.assertFalse(prompt_tools.ensure_difficulty_template(target))
