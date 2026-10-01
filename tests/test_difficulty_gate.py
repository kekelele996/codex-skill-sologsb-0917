from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import write_json  # noqa: E402
from prompt_tools import (  # noqa: E402
    HELL_PROMPT_MARKERS,
    MEDIUM_SHAPE_RE,
    P3_EASY_FACET_LABELS,
    RULE_STACK_FAMILIES,
    RULE_STACK_THRESHOLD,
    hell_prompt_markers,
    p3_easy_facets,
    rule_stack_families,
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
    {
        "id": 19738,
        "status": "已废弃",
        "qcHitRule": "G16",
        "userPrompt": "档案元数据核对台请补两套核对口径的切换，未决分数在口径更新后失效重算，历史结论保留，关键冲突说明，批量跳过，本地保存，撤销重做和导出。",
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

# 2026-09-30 真废弃样本 #19738（sologsb-1020 · 档案元数据核对台）：两套核对口径切换、
# 未决分数失效重算、历史结论保留、关键冲突说明、批量跳过、本地保存、撤销重做、导出，
# 全都在同一个核对子系统内部，平台仍判为中等。文本取自 references/g16-discarded-samples.json。
RULE_STACK_PROMPT = (
    "档案元数据核对台现在只有一套核对口径。请补两套口径切换：整理科按馆藏口径、编目科按借阅口径各核一遍。"
    "口径一改，还没定分的未决分数就要失效重算；已经定下来的历史结论照旧保留。"
    "两边对不上的条目要写清冲突原因，可以批量跳过暂时不核的条目。结果本地保存，撤销重做都能用，还能导出。"
)

# 同一件事补上真正的跨边界拓扑后的正确改法：档案室和整理室各自持有状态，失败后有恢复路径。
TOPOLOGY_PROMPT = (
    "档案室和整理室各有一套编目基准，核对员要给档案件打分，两边口径常常对不上。"
    "请做两次对账：整理室先给未决分数，档案室确认后才落定；任一基准更新后只重算受影响的那一份，"
    "历史确认结论照旧保留。关键冲突要写清来源，批量跳过只影响当前批次。"
    "对账失败后保住上一次结果并允许重试，导出也能查。"
)

# 2026-10-01 真废弃样本（submission 20713，`sologsb-1115 · 昆虫标本采集记录台`）：一条自包含的
# “分队离线记录合并回主台账”题面以「困难」提交后被平台按 P3 废弃，命中修改范围、上下文依赖、
# 交互轮次、技术广度四项，真实难度判为中等。文本取自本次任务的提示词原文。
P3_DISCARDED_PROMPT = (
    "野外调查分队离线各自记标本，回营地要把各自那份合并回队里的主台账。"
    "同一个标本编号两边都动过时，分类和采集信息认先登记的那份，鉴定结论和保藏柜位却不能被后来这份盖掉；"
    "采集地代码相同而坐标差得远的，先摆出来让队长挑。"
    "合并失败后这一批要整体回滚，已经并好的留着等重试，同一份记录再并一次不多出条目。"
)

# 同一道题按平台 P3 意见改硬后的形态：把状态所有权拆到分队那一份和队里编目库两侧，
# 两边各自持有记录，合并要求跨侧对账与按侧恢复，并把改动摊到既有模块上。
P3_HARDENED_PROMPT = (
    "野外分队在本机离线记标本，队里的编目库是另一套台账，两边各自持有自己的记录。"
    + P3_DISCARDED_PROMPT
)


def _p3_quotes(prompt: str, preferred: dict[str, str] | None = None) -> dict[str, str]:
    """优先用给好的原句；取不到时从题面截一段，保证测试数据始终落在题面上。"""
    preferred = preferred or {}
    out: dict[str, str] = {}
    for key in ("scope", "context", "rounds", "breadth"):
        value = str(preferred.get(key) or "")
        out[key] = value if value and value in prompt else prompt[:16]
    return out


def _p3_floor(
    prompt: str,
    *,
    quotes: dict[str, str] | None = None,
    hit: list[str],
    cross_passed: bool,
    modules: list[str] | None = None,
    cross_quote: str = "",
    one_round: bool = False,
) -> dict:
    """按真实题面拼一份 p3DifficultyFloor，避免测试里出现题面原句对不上的假数据。"""
    quotes = _p3_quotes(prompt, quotes)
    facets = {}
    for key in ("scope", "context", "rounds", "breadth"):
        quote = quotes[key]
        assert quote in prompt, f"{key} 的测试引用必须来自题面"
        facets[key] = {
            "hit": key in hit,
            "promptQuote": quote,
            "counter": (
                f"{P3_EASY_FACET_LABELS[key]}这一项在本题里由题面原句限定，"
                "反证是改动要同时守住两侧各自的记录，不能靠单端一次合并收尾"
            ),
        }
    return {
        "facets": facets,
        "crossModuleOrArchitecture": {
            "passed": cross_passed,
            "modules": modules or [],
            "quote": cross_quote,
            "whyArchitecture": (
                "两侧各自持有记录，任一侧更新只影响本侧，合并要按侧对账并在失败后按侧恢复，"
                "不是同一张本地规则表能顺序执行完的改动"
            )
            if cross_passed
            else "",
        },
        "repoContextDependency": {
            "required": True,
            "whatMustBeRead": "既有标本、鉴定、保藏与采集地四类记录的结构和既有编号规则",
            "evidence": "题面要求按编号比对并保护鉴定结论与柜位，必须先读懂仓库里的既有字段与约束",
        },
        "oneRoundEstimate": {
            "canModelFinishInOneRound": one_round,
            "why": (
                "要同时改数据结构、持久化与两个既有模块的入口，并按侧恢复，一轮改不完"
            )
            if not one_round
            else "题面是一条自包含的合并规则，按顺序实现一次导入加一次确认即可完成",
        },
    }


def _topology_review(prompt: str = TOPOLOGY_PROMPT, *, with_topology: bool = True) -> dict:
    data = _base_review(prompt)
    data["signals"]["multiModule"] = {
        "passed": True,
        "modules": ["档案基准", "未决分数", "确认结论"],
        "crossModuleInvariant": "整理室的未决分数和档案室的确认结论必须落在同一条档案上，任一侧基准更新只重算本侧",
        "evidence": "核对台读取两份基准、未决分数和确认结论，导出复用同一份对账结果",
    }
    data["signals"]["complexConcern"] = {
        "passed": True,
        "kinds": ["state-machine", "failure-recovery"],
        "technicalRisk": "两侧基准各自更新时对账状态会分叉，失败恢复时容易拿未决分数顶替确认结论",
        "observableFailure": "恢复后同一份档案出现两个互相矛盾的分数，导出文件带着错分数发给档案馆",
        "evidence": "需求要求只重算受影响的一侧，并在对账失败后保住上一版结果并按侧重试",
    }
    data["platformSignals"] = {
        "multiModule": {
            "answer": "整理室的未决分数和档案室的确认结论是两条独立链路，对账要把它们连起来而不互相覆盖",
            "promptQuote": "整理室先给未决分数，档案室确认后才落定",
            "ifViolated": "两边各自落定，核对结果和确认结论长期对不上",
        },
        "designTradeoff": {
            "answer": "基准更新后要选一个事实来源：只重算受影响的那一份，还是把两边全部推倒重来",
            "promptQuote": "任一基准更新后只重算受影响的那一份",
            "ifViolated": "整批分数被无差别重算，已经确认的结论也被抹掉",
        },
        "complexConcern": {
            "answer": "对账失败要保住上一版结果并按侧恢复，属于跨系统的失败恢复与状态所有权问题",
            "promptQuote": "对账失败后保住上一次结果并允许重试",
            "ifViolated": "失败一次就把上一版结果清空，核对员只能重头再核一遍",
        },
    }
    data["crossObjectInvariant"] = {
        "objects": ["档案室", "整理室"],
        "invariant": "整理室的未决分数必须由档案室的确认结论收口，任一侧基准更新都不能覆盖另一侧的确认结果",
        "divergenceFailure": "两边各留一份结果，导出时同一条档案出现两个互相矛盾的分数",
    }
    data["corpusEvidence"] = {
        "nearestDiscardedId": 19738,
        "differenceFromDiscarded": "废弃样本把口径切换、失效重算、冲突说明、批量跳过、撤销重做和导出都堆在同一个核对台里，只有一侧持有状态",
        "nearestPassedId": 9002,
        "borrowedComplexity": "借鉴通过样本里派生状态随上游变化失效的做法，再把所有权拆到档案室和整理室两侧",
    }
    data["substantiveComplexity"] = {
        "kinds": ["cross-system-reconciliation", "failure-recovery"],
        "promptQuote": "请做两次对账：整理室先给未决分数",
        "whyHard": "对账要同时守住两边各自的事实来源，任一侧基准更新只能重算本侧，失败后还要按侧恢复上一版结果",
        "visibleFailure": "一侧基准更新把另一侧的确认结论一起重算，核对台显示的分数和确认记录互相矛盾",
    }
    data["mediumShapeDefense"] = (
        "题面虽然是在既有核对台上加口径，但所有权分在档案室和整理室两侧，"
        "只重算受影响的一侧并在失败后按侧恢复，靠一张本地规则表做不到"
    )
    owners = [name for name in ("档案室", "整理室") if name in prompt]
    data["p3DifficultyFloor"] = _p3_floor(
        prompt,
        quotes={
            "scope": "请做两次对账：整理室先给未决分数",
            "context": "任一基准更新后只重算受影响的那一份",
            "rounds": "对账失败后保住上一次结果并允许重试",
            "breadth": "核对员要给档案件打分，两边口径常常对不上",
        },
        hit=p3_easy_facets(prompt),
        cross_passed=bool(owners),
        modules=owners,
        cross_quote="档案室和整理室各有一套编目基准" if owners else "",
    )
    if with_topology:
        data["complexityTopology"] = {
            "kind": "跨系统对账",
            "promptQuote": "档案室和整理室各有一套编目基准",
            "stateOwners": ["档案室", "整理室"],
            "failureOrRecovery": "对账失败后保住上一版结果并允许重试，恢复时以档案室确认过的结论为准，不拿未决分数顶替",
            "whyNotLocalRuleList": "两套基准分属档案室和整理室，未决分数和确认结论由不同岗位各自持有，"
            "任一侧更新只影响本侧，需要按侧对账和恢复，而不是在同一个子系统里顺序执行一张规则表。",
            "negativeOutcome": "恢复时把整理室的未决分数当成确认结论，历史确认结果被静默覆盖，导出也带着错分数",
        }
    return data


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
    tracks = [name for name in ("原音轨", "校订轨") if name in prompt]
    data["p3DifficultyFloor"] = _p3_floor(
        prompt,
        quotes={
            "scope": "校订员来回切换对时间，常改错段或动到原音",
            "context": "片段正文或时间码一变，原有遮盖就失效并需重新确认",
            "rounds": "两个遮盖范围重叠时拒绝保存并指出冲突",
            "breadth": "导出公开 SRT 时已确认范围显示已遮盖",
        },
        hit=p3_easy_facets(prompt),
        cross_passed=bool(tracks),
        modules=tracks,
        cross_quote="点一侧片段两侧一起定位，原音轨只读，校订轨可改" if tracks else "",
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

    def _validate(self, temp: str, review: dict, prompt: str) -> dict:
        review_path, prompt_path = self._write(temp, review, prompt)
        return validate_difficulty_review(
            review_path,
            "困难",
            prompt_text=prompt_path.read_text(encoding="utf-8"),
            history_path=Path(temp) / "history.json",
        )

    def test_p3_facet_detector_matches_real_discarded_prompt(self) -> None:
        labels = [P3_EASY_FACET_LABELS[name] for name in p3_easy_facets(P3_DISCARDED_PROMPT)]
        self.assertEqual(sorted(labels), sorted(["修改范围", "上下文依赖", "交互轮次", "技术广度"]))
        self.assertEqual([], p3_easy_facets(HARD_PROMPT))
        self.assertEqual([], p3_easy_facets(TOPOLOGY_PROMPT))

    def test_p3_floor_missing_is_blocked(self) -> None:
        review = _base_review(HARD_PROMPT)
        review.pop("p3DifficultyFloor")
        with tempfile.TemporaryDirectory() as temp:
            result = self._validate(temp, review, HARD_PROMPT)
            self.assertFalse(result["ok"])
            self.assertTrue(any("p3DifficultyFloor" in error for error in result["errors"]))

    def test_p3_real_discarded_prompt_is_blocked(self) -> None:
        review = _base_review(P3_DISCARDED_PROMPT)
        review["p3DifficultyFloor"] = _p3_floor(
            P3_DISCARDED_PROMPT,
            quotes={
                "scope": "回营地要把各自那份合并回队里的主台账",
                "context": "同一个标本编号两边都动过时",
                "rounds": "同一份记录再并一次不多出条目",
                "breadth": "野外调查分队离线各自记标本",
            },
            hit=["scope", "context", "rounds", "breadth"],
            cross_passed=False,
            one_round=True,
        )
        with tempfile.TemporaryDirectory() as temp:
            result = self._validate(temp, review, P3_DISCARDED_PROMPT)
            self.assertFalse(result["ok"])
            joined = " ".join(result["errors"])
            self.assertIn("P3 拒收线", joined)
            self.assertIn("自包含的合并", joined)
            self.assertIn("canModelFinishInOneRound", joined)

    def test_p3_hardened_prompt_passes(self) -> None:
        review = _p3_hardened_review()
        with tempfile.TemporaryDirectory() as temp:
            result = self._validate(temp, review, P3_HARDENED_PROMPT)
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(4, len(result["p3Facets"]))

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


class RuleStackTopologyTests(unittest.TestCase):
    """2026-09-30 #19738：单子系统规则堆叠必须补 complexityTopology 才能标困难。"""

    def _write(self, temp: str, review: dict, prompt: str) -> tuple[Path, Path]:
        base = Path(temp)
        prompt_path = base / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        review_path = base / "difficulty.json"
        write_json(review_path, review)
        write_json(base / "history.json", HISTORY_ITEMS)
        return review_path, prompt_path

    def _validate(self, review: dict, prompt: str) -> dict:
        with tempfile.TemporaryDirectory() as temp:
            review_path, prompt_path = self._write(temp, review, prompt)
            return validate_difficulty_review(
                review_path,
                "困难",
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=Path(temp) / "history.json",
            )

    def test_rule_stack_has_six_families(self) -> None:
        self.assertEqual(len(RULE_STACK_FAMILIES), 6)
        self.assertEqual(RULE_STACK_THRESHOLD, 4)

    def test_pure_rule_stack_is_detected(self) -> None:
        families = rule_stack_families(RULE_STACK_PROMPT)
        self.assertGreaterEqual(len(families), RULE_STACK_THRESHOLD, families)
        self.assertIn("mode-switch", families)
        self.assertIn("persist-undo-export", families)
        # 同一件事补上跨边界拓扑后仍然命中规则族，只是可以靠 complexityTopology 自证。
        self.assertGreaterEqual(len(rule_stack_families(TOPOLOGY_PROMPT)), RULE_STACK_THRESHOLD)
        # 已经通过门禁的口述史题面不应被规则堆叠规则误伤。
        self.assertLess(len(rule_stack_families(HARD_PROMPT)), RULE_STACK_THRESHOLD)
        self.assertLess(len(rule_stack_families(MEDIUM_PROMPT)), RULE_STACK_THRESHOLD)

    def test_original_19738_prompt_is_blocked(self) -> None:
        samples = __import__("json").loads(
            (ROOT / "references" / "g16-discarded-samples.json").read_text(encoding="utf-8")
        )
        sample = next(item for item in samples["samples"] if int(item["id"]) == 19738)
        self.assertEqual(sample["shapeTag"], "单子系统规则堆叠")
        self.assertGreaterEqual(
            len(rule_stack_families(sample["userPrompt"])), RULE_STACK_THRESHOLD
        )
        result = self._validate(_base_review(HARD_PROMPT), sample["userPrompt"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("complexityTopology" in error for error in result["errors"]))

    def test_missing_topology_is_blocked(self) -> None:
        result = self._validate(_topology_review(with_topology=False), TOPOLOGY_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("complexityTopology" in error for error in result["errors"]))

    def test_state_invalidation_is_not_a_topology_kind(self) -> None:
        review = _topology_review()
        review["complexityTopology"]["kind"] = "state-invalidation"
        result = self._validate(review, TOPOLOGY_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("state-invalidation" in error for error in result["errors"]))

    def test_topology_quote_must_show_cross_boundary(self) -> None:
        review = _topology_review()
        review["complexityTopology"]["promptQuote"] = "关键冲突要写清来源，批量跳过只影响当前批次"
        result = self._validate(review, TOPOLOGY_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("跨边界拓扑" in error for error in result["errors"]))

    def test_topology_state_owners_must_appear_in_prompt(self) -> None:
        review = _topology_review()
        review["complexityTopology"]["stateOwners"] = ["档案室", "第三方质检所"]
        result = self._validate(review, TOPOLOGY_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有出现在题面原文里" in error for error in result["errors"]))

    def test_topology_why_not_local_rule_list_needs_30_chars(self) -> None:
        review = _topology_review()
        review["complexityTopology"]["whyNotLocalRuleList"] = "比本地规则表复杂"
        result = self._validate(review, TOPOLOGY_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("whyNotLocalRuleList" in error for error in result["errors"]))

    def test_cross_system_topology_passes(self) -> None:
        result = self._validate(_topology_review(), TOPOLOGY_PROMPT)
        self.assertTrue(result["ok"], result["errors"])
        self.assertGreaterEqual(len(result["ruleStackFamilies"]), RULE_STACK_THRESHOLD)


class ModelSplitTests(unittest.TestCase):
    """2026-09-30：两侧可以同模型；只有缺模型名才在开跑前拦掉。"""

    @staticmethod
    def _plan(model_a: str, model_b: str) -> dict:
        return {"A": {"candidateId": "candidate-1", "model": model_a},
                "B": {"candidateId": "candidate-2", "model": model_b}}

    def test_same_model_on_both_sides_is_allowed(self) -> None:
        import side_runner

        plan = self._plan("auto_model/urm", "auto_model/urm")
        side_runner.assert_models_configured(plan)
        self.assertTrue(side_runner.same_model_pair(plan))

    def test_different_models_pass(self) -> None:
        import side_runner

        plan = self._plan("auto_model/urm", "ark/urm-03")
        side_runner.assert_models_configured(plan)
        self.assertFalse(side_runner.same_model_pair(plan))

    def test_missing_model_is_rejected(self) -> None:
        import side_runner

        with self.assertRaisesRegex(Exception, "模型名缺失"):
            side_runner.assert_models_configured(self._plan("auto_model/urm", ""))


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


# 2026-09-30 G17 实测：题面带容量排队、失效重算、离线合并和升级迁移，但全部在一个本地库里，
# 平台判回「困难」并要求改档重提。这段原文用于回归新的地狱门禁。
HELL_INFLATION_PROMPT = (
    "蜂场这季想把安排落到执行。技术员挑好地块和蜂群，按投放点容量排蜂群，排不下的箱数先排队，"
    "并说清是哪个点还差几箱。地块花期或投放点坐标一改动，引用过它的排季安排就要失效重算，"
    "重算前不能当作可执行的方案导出。外勤离线改完，回驻地按编号合并进来，两边都动过的先列出来复核。"
    "老浏览器里上一季的数据，升级后地块、蜂群、投放点、路线一条都不能丢。"
)

# 真正的地狱形态：现场条件不足，方案由执行者自行确定；并且题面本身给出两条互相牵制的约束。
HELL_PROMPT = (
    "蜂场和托管服务队各自维护一套排季数据，技术员在现场没网，排布方案要由执行者自行确定事实来源。"
    "既要保住已经投放的执行进度，又要让改期的排季安排立刻失效重算。"
    "两边都动过的记录按编号合并，冲突时以服务队确认的结论为准。"
    "断网恢复后要按侧对账，失败后保住上一次结果并允许重试，导出也要能查。"
)


def _hell_review(prompt: str = HELL_PROMPT, *, with_hell_quote: bool = True, with_tradeoff_quote: bool = True) -> dict:
    data = _base_review(prompt)
    data["difficulty"] = "地狱"
    data["verdict"] = "地狱"
    data["signals"]["multiModule"] = {
        "passed": True,
        "modules": ["排季安排", "外勤离线记录", "服务队确认结论"],
        "crossModuleInvariant": "两边都动过的记录按编号合并后只能留一份结论，服务队确认的结论优先于本地排季安排",
        "evidence": "排季安排、外勤离线记录和服务队确认结论共用同一条合并链路，导出复用同一份结果",
    }
    data["signals"]["designTradeoff"] = {
        "passed": True,
        "conflict": "已经投放的执行进度要保住，改了花期的排季安排又必须立刻失效重算，两条要求互相牵制",
        "decision": "必须决定事实来源与失效边界：只重算被改动引用的安排，同时保留已经投放的执行进度",
        "whyNotRoutine": "不是普通字段校验，选错会让在园蜂群被重排或让过期安排继续被导出执行",
        "promptQuote": (
            "既要保住已经投放的执行进度，又要让改期的排季安排立刻失效重算" if with_tradeoff_quote else ""
        ),
        "evidence": "题面同时要求保住执行进度和让改动后的安排失效，说明两条约束必须同时成立",
    }
    data["signals"]["complexConcern"] = {
        "passed": True,
        "kinds": ["state-machine", "failure-recovery"],
        "technicalRisk": "断网期间两侧各自改数据，恢复后按侧对账与失效重算交织，容易拿旧结果顶替新结论",
        "observableFailure": "恢复后同一条记录出现两份结论，导出文件带着过期安排发给服务队",
        "evidence": "题面要求断网恢复后按侧对账，并在失败后保住上一次结果再重试",
    }
    data["platformSignals"] = {
        "multiModule": {
            "answer": "排季安排、外勤离线记录和服务队确认结论是三条链路，合并后必须对同一条记录收口",
            "promptQuote": "两边都动过的记录按编号合并",
            "ifViolated": "合并后同一编号留下两份结论，导出对不上服务队的确认结果",
        },
        "designTradeoff": {
            "answer": "必须决定事实来源与失效边界：保住执行进度还是整批重算，只能选一个作为主线",
            "promptQuote": "既要保住已经投放的执行进度，又要让改期的排季安排立刻失效重算",
            "ifViolated": "要么把在园进度一起作废，要么过期安排继续被当成可执行方案",
        },
        "complexConcern": {
            "answer": "断网两侧各自改数据后要按侧对账并在失败后恢复，属于跨侧一致性与失败恢复问题",
            "promptQuote": "断网恢复后要按侧对账，失败后保住上一次结果并允许重试",
            "ifViolated": "恢复后两侧结论互相覆盖，核对员分不清哪一份有效",
        },
    }
    data["crossObjectInvariant"] = {
        "objects": ["排季安排", "记录"],
        "invariant": "两边都动过的记录合并后只能保留一份结论，排季安排必须引用这份确认过的结论",
        "divergenceFailure": "排季安排引用了被覆盖的旧结论，导出结果与服务队确认的不一致",
    }
    data["corpusEvidence"] = {
        "nearestDiscardedId": 19738,
        "differenceFromDiscarded": "废弃样本把口径切换、失效重算、冲突说明和导出堆在一个核对台里，只有一个所有者；本题把状态拆到蜂场、服务队两侧并要求按侧对账",
        "nearestPassedId": 9002,
        "borrowedComplexity": "借鉴通过样本里派生状态随上游变化失效的做法，再把所有权拆成蜂场与服务队两侧",
    }
    data["substantiveComplexity"] = {
        "kinds": ["cross-system-reconciliation", "state-invalidation"],
        "promptQuote": "既要保住已经投放的执行进度，又要让改期的排季安排立刻失效重算",
        "whyHard": "断网两侧各自修改后要按侧对账，并让被改动的安排失效重算，同时保住已经投放的执行进度",
        "visibleFailure": "恢复后旧结论覆盖新结论，排季安排带着过期数据被导出执行",
    }
    data["mediumShapeDefense"] = (
        "题面不只是加一层：状态分在蜂场和服务队两侧，断网恢复后要按侧对账并保留失败前结果，"
        "只靠一张本地规则表做不到"
    )
    if with_hell_quote:
        data["hellSignal"] = {
            "passed": True,
            "kinds": ["open-ended", "cross-system-recovery"],
            "promptQuote": "排布方案要由执行者自行确定事实来源",
            "reason": "现场没网且两侧各自维护数据，执行者必须先确定事实来源和失效边界，题面没有给出具体方案",
            "evidence": "题面写明现场没网、方案由执行者自行确定事实来源，并要求断网恢复后按侧对账",
        }
    else:
        data["hellSignal"] = {
            "passed": True,
            "kinds": ["open-ended", "cross-system-recovery"],
            "reason": "现场没网且两侧各自维护数据，执行者必须先确定事实来源和失效边界",
            "evidence": "题面写明现场没网、方案由执行者自行确定事实来源，并要求断网恢复后按侧对账",
        }
    return data


class HellDifficultyGateTests(unittest.TestCase):
    """2026-09-30 G17：地狱必须由题面原句支撑，否则改填困难。"""

    def _validate(self, review: dict, prompt: str, difficulty: str = "地狱") -> dict:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            prompt_path = base / "prompt.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            review_path = base / "difficulty.json"
            write_json(review_path, review)
            write_json(base / "history.json", HISTORY_ITEMS)
            return validate_difficulty_review(
                review_path,
                difficulty,
                prompt_text=prompt_path.read_text(encoding="utf-8"),
                history_path=base / "history.json",
            )

    def test_hell_marker_helper_detects_hell_features(self) -> None:
        self.assertEqual(hell_prompt_markers(HELL_INFLATION_PROMPT), [])
        markers = hell_prompt_markers(HELL_PROMPT)
        self.assertIn("open-ended", markers)
        self.assertIn("cross-system-recovery", markers)
        for pattern in HELL_PROMPT_MARKERS.values():
            self.assertTrue(pattern.pattern)

    def test_inflated_prompt_cannot_be_hell(self) -> None:
        # 本次真实题面：容量排队 + 失效重算 + 离线合并 + 升级迁移，但题面没有地狱级特征。
        result = self._validate(_hell_review(HELL_PROMPT), HELL_INFLATION_PROMPT)
        self.assertFalse(result["ok"])
        joined = " ".join(result["errors"])
        self.assertIn("不是题面原句", joined)
        self.assertIn("改填「困难」", joined)

    def test_missing_hell_quote_is_blocked(self) -> None:
        result = self._validate(_hell_review(with_hell_quote=False), HELL_PROMPT)
        self.assertFalse(result["ok"])
        joined = " ".join(result["errors"])
        self.assertIn("hellSignal.promptQuote", joined)
        self.assertIn("改填「困难」", joined)

    def test_hell_quote_must_match_declared_kind(self) -> None:
        review = _hell_review()
        review["hellSignal"]["promptQuote"] = "两边都动过的记录按编号合并"
        result = self._validate(review, HELL_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("没有体现所声明的地狱级特征" in error for error in result["errors"]))

    def test_missing_tradeoff_quote_is_blocked(self) -> None:
        result = self._validate(_hell_review(with_tradeoff_quote=False), HELL_PROMPT)
        self.assertFalse(result["ok"])
        joined = " ".join(result["errors"])
        self.assertIn("designTradeoff.promptQuote", joined)
        self.assertIn("改填「困难」", joined)

    def test_tradeoff_quote_must_show_two_constraints(self) -> None:
        review = _hell_review()
        review["signals"]["designTradeoff"]["promptQuote"] = "两边都动过的记录按编号合并"
        result = self._validate(review, HELL_PROMPT)
        self.assertFalse(result["ok"])
        self.assertTrue(any("互相牵制" in error for error in result["errors"]))

    def test_prompt_backed_hell_review_passes(self) -> None:
        result = self._validate(_hell_review(), HELL_PROMPT)
        self.assertTrue(result["ok"], result["errors"])
        self.assertIn("open-ended", result["hellPromptMarkers"])

    def test_difficulty_field_is_downgraded_not_ignored(self) -> None:
        # 同一份难度论证去掉地狱原句后：标困难可以过（不要求地狱证据），标地狱被拦。
        hard_review = _hell_review(with_hell_quote=False, with_tradeoff_quote=False)
        hard_review["difficulty"] = "困难"
        hard_review["verdict"] = "困难"
        hard_review.pop("hellSignal", None)
        hard_result = self._validate(hard_review, HELL_PROMPT, "困难")
        self.assertTrue(hard_result["ok"], hard_result["errors"])

        hell_review = _hell_review(with_hell_quote=False, with_tradeoff_quote=False)
        hell_result = self._validate(hell_review, HELL_PROMPT, "地狱")
        self.assertFalse(hell_result["ok"])
        self.assertIn("hellSignal.promptQuote", " ".join(hell_result["errors"]))
def _p3_hardened_review(prompt: str = P3_HARDENED_PROMPT) -> dict:
    """改硬后的同一道题：题面自己给出两个持有状态的端，难度论证逐项回指题面。"""
    data = _base_review(prompt)
    data["platformSignals"] = {
        "multiModule": {
            "answer": "分队那份记录和队里的编目库各自持有状态，合并要把两侧记录收口到同一条标本编号上",
            "promptQuote": "回营地要把各自那份合并回队里的主台账",
            "ifViolated": "两侧各留一份记录，主台账里同一编号出现互相矛盾的两套信息",
        },
        "designTradeoff": {
            "answer": "要尽量把分队那份并进来，又不能让它覆盖编目库已经采用的鉴定结论和保藏柜位",
            "promptQuote": "鉴定结论和保藏柜位却不能被后来这份盖掉",
            "ifViolated": "按后到覆盖会把队里已经核过的结论冲掉，标本追溯链断开",
        },
        "complexConcern": {
            "answer": "跨两端合并要一次成功或整批回滚，重复并入还要去重，属于跨侧一致性与失败恢复",
            "promptQuote": "合并失败后这一批要整体回滚",
            "ifViolated": "中途失败会留下半截数据，重复导入还会把同一份标本记成多条",
        },
    }
    data["crossObjectInvariant"] = {
        "objects": ["标本编号", "鉴定结论", "保藏柜位"],
        "invariant": "同一个标本编号在两侧合并后只能对应一份生效鉴定结论和一份保藏柜位，分队那份不能改写它",
        "divergenceFailure": "两侧对不上时同一编号会同时出现两份结论与柜位，保藏记录找不到唯一归属",
    }
    data["substantiveComplexity"] = {
        "kinds": ["offline-merge", "failure-recovery"],
        "promptQuote": "野外调查分队离线各自记标本，回营地要把各自那份合并回队里的主台账",
        "whyHard": "两侧各自持有记录，合并要选冲突字段的归属方，失败后整批回滚并允许按侧重试，缺一处就破坏唯一性",
        "visibleFailure": "合并失败后主台账留下只有鉴定没有标本的残缺记录，重复并入把同一编号记成多条",
    }
    data["signals"]["complexConcern"] = {
        "passed": True,
        "kinds": ["offline-sync", "idempotency", "failure-recovery"],
        "technicalRisk": "两侧各自持有记录，合并要同时落标本、鉴定和柜位，失败会留下半截数据，重复并入还会累加",
        "observableFailure": "合并中途出错后主台账出现有鉴定记录却没有对应标本，或者同一编号挂着两条柜位记录",
        "evidence": "题面要求失败整批回滚、已并好的留着等重试，并要求同一份记录再并一次不多出条目",
    }
    data["corpusEvidence"] = {
        "nearestDiscardedId": 9001,
        "differenceFromDiscarded": "废弃样本只是在既有卡片上做预览式批量替换，没有两侧各自持有记录，也没有冲突字段归属",
        "nearestPassedId": 9002,
        "borrowedComplexity": "借鉴通过样本里派生状态随上游变化失效再重新确认的做法，改到两侧记录合并与按侧恢复上",
    }
    data["p3DifficultyFloor"] = _p3_floor(
        prompt,
        quotes={
            "scope": "回营地要把各自那份合并回队里的主台账",
            "context": "同一个标本编号两边都动过时",
            "rounds": "同一份记录再并一次不多出条目",
            "breadth": "野外调查分队离线各自记标本",
        },
        hit=p3_easy_facets(prompt),
        cross_passed=True,
        modules=["野外分队", "编目库"],
        cross_quote="队里的编目库是另一套台账，两边各自持有自己的记录",
    )
    return data
