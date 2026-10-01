#!/usr/bin/env python3
"""Prompt validation and canonical prompt installation."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import (
    DIFFICULTIES,
    SOLO_SCRIPTS,
    SologsbError,
    TASK_TYPES,
    atomic_copy,
    atomic_write_text,
    read_json,
    save_state,
    sha256_bytes,
    write_json,
)

VALIDATE_PROMPT = SOLO_SCRIPTS / "validate-prompt.py"
AUDIT_HUMAN = SOLO_SCRIPTS / "audit-human-writing.py"
MARKDOWN = re.compile(r"(^|\n)\s{0,3}(#{1,6}|[-*+]\s|\d+[.)]\s|```)|[`*_]{2,}|\[[^\]]+\]\([^)]*\)")
PATH_OR_CODE = re.compile(
    r"(?:/Users/|/home/|[A-Za-z]:\\|\b(?:src|frontend|backend|internal|cmd)/[\w./-]+|"
    r"\b(?:class|function|def|func|import|require)\b)"
)
GUIDED_FIX = re.compile(r"(?:改成|改为|修改为|换成|替换为|加上|加入|删除|删掉|加锁|改返回|使用.+修复)")
ROUND_REFERENCE = re.compile(r"(第[一二三四五六七八九十百0-9]+轮|本轮|上一轮|下一轮|首轮|轮次)")
# 2026-09-23 起：提示词要像业务方口头交代需求，不写成条款清单。
# 历史 119 条里约一半以“刷新后……一致”收尾，四分之一带“整次”，既像模板也容易撞查重。
PROMPT_RIGID_RE = re.compile(r"(?:只能|仅能|仅|必须|不得|不能|一律|禁止|严禁|务必|只允许)")
PROMPT_MAX_RIGID = 3
PROMPT_MAX_SEMICOLONS = 2
PROMPT_TEMPLATE_TAIL_RE = re.compile(r"(?:刷新|并发|重复)[^。！？]{0,24}(?:一致|不乱|只生效一次|仍能读回)[。！？]?$")
PROMPT_TEMPLATE_WORD_RE = re.compile(r"(?:整次|任一步失败全部回滚|保持不动)")

# 2026-09-29 G16 加固：平台在本日把“在既有模块里加一层”的题面批量判为中等。
# 光靠自由作文式的难度论证挡不住，所以难度论证必须回指题面原句和真实历史样本。
PLATFORM_SIGNAL_KEYS = ("multiModule", "designTradeoff", "complexConcern")
PLATFORM_SIGNAL_LABELS = {
    "multiModule": "多模块整合",
    "designTradeoff": "关键设计取舍",
    "complexConcern": "复杂技术关注点",
}
# 平台列出的“直接判为中等”形态里最常见的一种：在既有页面上补一个视图/清单/预览/字段。
# 这是摩擦规则不是判难器：命中只要求补写 mediumShapeDefense，没命中也不代表题面就够难。
# 2026-09-29 在真实语料上回归：7 条 G16 废弃命中 3 条、40 条通过命中 10 条，
# 所以它只用来触发一次强制自辩，不能当作难度分类器。
MEDIUM_SHAPE_VERB = r"(?:新增|补一个|补上|补一份|补一套|补|加一个|增加|加一套|做一个|做一套)"
MEDIUM_SHAPE_NOUN = (
    r"(?:视图|页面|面板|弹窗|预览|清单|列表|统计|导出|字段|按钮|标签|卡片|入口|台账|"
    r"工作台|对照台|看板|报表|图表|记录|模式|区|栏|包|回合|批注|意见|修订|版本|流程|规则)"
)
MEDIUM_SHAPE_RE = re.compile(
    r"(?:" + MEDIUM_SHAPE_VERB + r"[^。；！？]{0,14}(?:" + MEDIUM_SHAPE_NOUN + r"))"
    r"|(?:(?:" + MEDIUM_SHAPE_NOUN + r")[^。；！？]{0,10}" + MEDIUM_SHAPE_VERB + r")"
)
# 这些词只用于给难度论证降噪提示，不单独作为阻断依据：真实语料里通过与被废弃的题面都会命中。
PROMPT_SIGNAL_HINTS = {
    "concurrency": re.compile(r"(并发|同时(编辑|保存|提交|校对)|多标签|抢占|接管|交接|竞态|分叉|签出|冲突)"),
    "invalidation": re.compile(r"(失效|过期|作废|退回待|重新确认|级联|连带|不再有效|重算)"),
    "atomicity": re.compile(r"(原子|整批|一起写入|回滚|补偿|幂等|重复(提交|导入)不|去重|不新增)"),
    "migration": re.compile(r"(迁移|升级|旧数据|旧草稿|旧稿|兼容|版本链|快照|历史版本)"),
    "permission": re.compile(r"(权限|角色|越权|租户|授权|审批|审计)"),
    "recovery": re.compile(r"(离线|断网|恢复|重试|降级|超时|异常|失败后|重连|崩溃|白屏)"),
    "capacity": re.compile(r"(性能|容量|限流|吞吐|大批量|超长|内存|耗时)"),
}

# 2026-09-29 G16 二次加固：之前的 platformSignals/crossObjectInvariant 仍可被
# “审阅回合、批注去重、冲突提示、冻结快照修订”这类多步工作流绕过。平台实测把
# 这种题判为中等，所以难度论证还必须在题面原句里出现真正的实质复杂度信号。
SUBSTANTIVE_COMPLEXITY_PATTERNS = {
    "concurrency": re.compile(r"(并发|同时(?:编辑|保存|提交|修改)|多标签.{0,12}(?:覆盖|冲突)|竞态|抢占|租约|加锁)"),
    "failure-recovery": re.compile(r"(失败后.{0,12}(?:恢复|重试|回滚|补偿)|崩溃.{0,12}(?:恢复|找回)|异常.{0,12}(?:恢复|补偿)|断网.{0,12}(?:恢复|重试))"),
    "migration": re.compile(r"((?:旧数据|旧稿|历史数据|已有数据).{0,12}(?:迁移|升级|兼容|回填)|(?:迁移|升级).{0,12}(?:回退|兼容|历史数据))"),
    "permission-boundary": re.compile(r"(越权|租户|角色.{0,8}(?:权限|边界)|权限.{0,8}(?:拒绝|隔离))"),
    "capacity": re.compile(r"((?:大批量|超长|超时|容量|性能|限流).{0,12}(?:拒绝|降级|排队|分批)|(?:拒绝|降级|排队|分批).{0,12}(?:大批量|超长|容量))"),
    "offline-merge": re.compile(r"((?:离线|断网).{0,15}(?:合并|同步|恢复|冲突)|(?:网络恢复|重新连接).{0,15}(?:合并|同步|冲突))"),
    "state-invalidation": re.compile(r"((?:一变|变化|改动|修改|更新).{0,12}(?:失效|重算|重新确认|作废)|(?:失效|过期).{0,12}(?:拒绝|重新确认|重算))"),
    "cross-system-reconciliation": re.compile(r"(对账|跨系统|两套.{0,8}(?:一致|冲突)|外部系统.{0,12}(?:返回|同步|不一致))"),
}

# 2026-09-30 G16 三次加固：平台把“在同一个核对子系统里堆多条规则”的题面判为中等。
# 真实废弃案例 #19738（sologsb-1020 · 档案元数据核对台）：题面同时有“两套核对口径切换、
# 未决分数失效重算、历史结论保留、关键冲突说明、批量跳过、本地保存、撤销重做、导出”，
# 平台原话是“需要在既有核对子系统内实现多项规则，约束明确、没有架构级设计取舍”，真实难度为中等。
# 这类题的功能数量看起来不少，但所有权只有一个、失败也只在本地重算，所以不能只靠规则族数量判难。
# 六个规则族全部只在一个子系统内部生效，命中四个以上就必须额外证明跨边界拓扑，否则阻断。
RULE_STACK_FAMILIES: dict[str, re.Pattern[str]] = {
    "mode-switch": re.compile(
        r"(?:两套|一套|多套|另一套|第二套|两种|多套)[^。；！？，]{0,12}(?:口径|标准|基准|规则|模板|名单|模式)"
        r"|(?:口径|标准|模式|模板)[^。；！？，]{0,8}(?:切换|来回切换|轮流|互换)"
    ),
    "recompute-invalidation": re.compile(
        r"(?:失效|重算|重新计算|作废|过期|不再有效|重新确认|级联|全部重来)"
    ),
    "history-retention": re.compile(
        r"(?:历史(?:结论|记录|版本|结果|判定)|保留(?:旧|原有|之前|历史)|旧(?:结论|记录|版本|判定)"
        r"|原样保留|照旧保留|继续可查)"
    ),
    "conflict-gate": re.compile(r"(?:冲突|拒绝保存|阻止|拦截|闸门|互斥|矛盾|不一致)"),
    "batch-operation": re.compile(r"(?:批量|一键|整批|全部(?:跳过|处理|确认|生效)|逐条)"),
    "persist-undo-export": re.compile(r"(?:本地保存|撤销|重做|导出|另存|持久化保存)"),
}
# “命中四个以上”按 ≥4 执行：一个子系统里凑满四族规则就足以说明它是规则堆叠，而不是偶然多写一步。
RULE_STACK_THRESHOLD = 4

# 2026-10-01 平台规则 P3（题目难度下限）：当天 `sologsb-1115 · 昆虫标本采集记录台` 的一条数据
# （submission 20713）以「困难」提交后被平台按 P3 废弃：“命中题目规则 P3（题目难度过于简单）……
# 本题命中 4 项：修改范围、上下文依赖、交互轮次、技术广度……判定的题目真实难度档位为「中等」……
# 该条数据作废，不可返修：难度是题目本身的属性”。G16/G17 回答的是“够不够难”，回答不了
# “是不是一条自包含的合并规格”。平台把「过于简单」拆成四个可数特征，命中两项及以上一律拒收，
# 所以本版本再补一层难度下限门禁：题面里能识别的特征必须逐项在 difficulty-review.json 里
# 自报并给出反证；命中两项以上时还要证明它真的跨模块、模型一轮做不完，否则 `prompt` 直接阻断。
P3_EASY_FACETS = ("scope", "context", "rounds", "breadth")
P3_EASY_FACET_LABELS = {
    "scope": "修改范围",
    "context": "上下文依赖",
    "rounds": "交互轮次",
    "breadth": "技术广度",
}
# 这四组只是“提示器”，用来把题面里可能被平台判成过于简单的写法标出来，不做自动判难：
# - scope：改动落在一条自包含流程上（把某份记录、清单、草稿或数据包合并/并账/对账回一处）
# - context：判据被写死在题面里（同一个编号、同一份记录、按编号比对），不需要读懂仓库既有结构
# - rounds：题面可以用一次导入加一次确认收尾，或只说“再并一次不多出条目”
# - breadth：只在一台机器、一个端上记和存，不涉及并发、权限、容量、迁移或跨系统
P3_FACET_HINTS: dict[str, re.Pattern[str]] = {
    "scope": re.compile(
        r"(?:合并|并账|对账|合入|并回|导回|汇总)[^。；！？]{0,14}(?:台账|清单|记录|草稿|备份|数据包|文件|表格)"
        r"|(?:台账|清单|草稿|备份|数据包)[^。；！？]{0,10}(?:合并|并账|对账|合入|并回)"
    ),
    "context": re.compile(
        r"(?:同一个|同一份|同编号|按编号|依据|只按)[^。；！？]{0,12}(?:编号|记录|字段|规则|条款|口径)"
    ),
    "rounds": re.compile(
        r"(?:再(?:并|导|合|提)一次|重复(?:导入|并入|提交|并账)|同一份[^。；！？]{0,8}再|一次(?:导入|合并|并账))"
    ),
    "breadth": re.compile(
        r"(?:离线|本机|本地|单机|一个人|各自)[^。；！？]{0,12}(?:记|存|录入|登记|填|改)"
    ),
}
P3_HIT_THRESHOLD = 2
# 平台 P3 的“修改范围”在实测里对应这种自包含形态：把一份记录/清单/草稿/数据包合并或对账回一处。
P3_SELF_CONTAINED_RE = re.compile(
    r"(?:合并|并账|对账|合入|并回|导回)[^。；！？]{0,14}(?:台账|清单|草稿|备份|数据包|文件|表格)"
)
# 允许把命中两项以上的题面救回来的跨边界写法：题面本身要出现两个各自持有状态的系统、部门、岗位或端。
P3_CROSS_BOUNDARY_RE = re.compile(
    r"(?:跨系统|跨库|跨端|跨部门|两个(?:系统|部门|岗位|班组|科室|库|端)|两边各自|各(?:自|管各的|维护)"
    r"|两边|双方|两侧|各有|分区|租户|接口|服务端|并发|权限|容量|限流"
    r"|离线[^。；！？]{0,8}多端|多端[^。；！？]{0,8}离线"
    r"|按侧恢复|按侧重试|对账失败)"
)


def p3_easy_facets(prompt_text: str) -> list[str]:
    """列出题面里能识别的“过于简单”特征，供出题人自检与留痕，不单独作为判难依据。"""
    return sorted(name for name, pattern in P3_FACET_HINTS.items() if pattern.search(prompt_text))

# complexityTopology 只认真正的跨边界拓扑。四个以上规则族命中后，题面必须能指出
# “谁和谁各自持有状态、失败或恢复怎么发生、为什么它不是本地规则表”。
TOPOLOGY_KIND_ALIASES: dict[str, str] = {
    "跨系统对账": "跨系统对账",
    "cross-system-reconciliation": "跨系统对账",
    "reconciliation": "跨系统对账",
    "失败恢复": "失败恢复",
    "failure-recovery": "失败恢复",
    "recovery": "失败恢复",
    "迁移兼容": "迁移兼容",
    "migration": "迁移兼容",
    "migration-compatibility": "迁移兼容",
    "并发冲突": "并发冲突",
    "concurrency": "并发冲突",
    "concurrency-conflict": "并发冲突",
    "权限边界": "权限边界",
    "permission-boundary": "权限边界",
    "容量约束": "容量约束",
    "capacity": "容量约束",
    "capacity-constraint": "容量约束",
    "离线合并": "离线合并",
    "offline-merge": "离线合并",
    "独立所有权拆分": "独立所有权拆分",
    "ownership-split": "独立所有权拆分",
}
TOPOLOGY_KINDS = (
    "跨系统对账",
    "失败恢复",
    "迁移兼容",
    "并发冲突",
    "权限边界",
    "容量约束",
    "离线合并",
    "独立所有权拆分",
)
# 明确禁止当复杂拓扑用的伪类型：口径更新后的失效重算只是本地派生状态，不是跨边界拓扑。
TOPOLOGY_FORBIDDEN_KINDS = {
    "state-invalidation",
    "state_invalidation",
    "invalidation",
    "失效重算",
    "状态失效",
    "失效",
}
# 每种合法拓扑对应的题面跨边界模式：promptQuote 既要是题面原句，也要真的体现这种跨界关系。
TOPOLOGY_BOUNDARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "跨系统对账": re.compile(
        r"(?:对账|跨系统|外部系统|双方|两边|两个(?:系统|部门|岗位|班组|房间|库|科室)|"
        r"(?:两|各)[^。；！？，]{0,6}(?:套|方|边|侧)[^。；！？，]{0,10}(?:口径|标准|基准|账|数据|名单|结果)|"
        r"(?:两|各)[^。；！？，]{0,6}(?:口径|标准|基准|账|数据|名单|结果))"
    ),
    "失败恢复": re.compile(
        r"(?:失败后[^。；！？]{0,12}(?:恢复|重试|回滚|补偿|找回|保住)"
        r"|崩溃[^。；！？]{0,12}(?:恢复|找回)"
        r"|异常[^。；！？]{0,12}(?:恢复|补偿|重试)"
        r"|断网[^。；！？]{0,12}(?:恢复|重试)"
        r"|重试[^。；！？]{0,10}(?:失败|中断))"
    ),
    "迁移兼容": re.compile(
        r"(?:(?:旧数据|旧稿|历史数据|已有数据|旧版本|老数据)[^。；！？]{0,12}(?:迁移|升级|兼容|回填|兼容)"
        r"|(?:迁移|升级)[^。；！？]{0,12}(?:回退|兼容|历史数据|旧))"
    ),
    "并发冲突": re.compile(
        r"(?:并发|同时(?:编辑|保存|提交|修改|操作|核对)|多标签[^。；！？]{0,12}(?:覆盖|冲突)"
        r"|竞态|抢占|租约|加锁|两人[^。；！？]{0,10}同时|前后脚)"
    ),
    "权限边界": re.compile(
        r"(?:越权|租户|角色[^。；！？]{0,8}(?:权限|边界)|权限[^。；！？]{0,8}(?:拒绝|隔离|边界)"
        r"|只有[^。；！？]{0,10}(?:能|可|权限))"
    ),
    "容量约束": re.compile(
        r"(?:(?:大批量|超长|超时|容量|性能|限流)[^。；！？]{0,12}(?:拒绝|降级|排队|分批|截断)"
        r"|(?:拒绝|降级|排队|分批|截断)[^。；！？]{0,12}(?:大批量|超长|容量))"
    ),
    "离线合并": re.compile(
        r"(?:(?:离线|断网)[^。；！？]{0,15}(?:合并|同步|恢复|冲突)"
        r"|(?:网络恢复|重新连接|回到网络)[^。；！？]{0,15}(?:合并|同步|冲突))"
    ),
    "独立所有权拆分": re.compile(
        r"(?:各(?:自|管各的|维护)|分别(?:维护|负责|记账|记录|持有)|独立(?:维护|拥有|归属|负责)"
        r"|分开(?:记|维护|保存)|互不(?:覆盖|干扰|影响)|归属|(?:两个|两边)[^。；！？]{0,10}各自)"
    ),
}


def _history_records(history_path: Path | None) -> list[dict[str, Any]]:
    if history_path is None or not history_path.is_file():
        return []
    data = read_json(history_path, [])
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = data.get("items") or data.get("prompts") or []
    else:
        records = []
    return [item for item in records if isinstance(item, dict)]


def _history_discard_kind(item: dict[str, Any]) -> str:
    rule = str(item.get("qcHitRule") or item.get("qc_hit_rule") or "").strip().upper()
    if rule:
        return rule
    status = str(item.get("status") or item.get("status_label") or "")
    if "废弃" in status or "DISCARD" in status.upper():
        return "DISCARDED"
    return ""


# 平台废弃题目时命中的规则名：G16/G17 是难度，P3 是难度下限（2026-10-01 起）。真实语料里
# 还出现过只给“已废弃”状态、不给规则名的记录，所以 DISCARDED 也认。
DISCARD_RULE_KINDS = {
    "DISCARDED",
    "G16",
    "G17",
    "P3",
}


def _history_passed(item: dict[str, Any]) -> bool:
    status = str(item.get("status") or item.get("status_label") or "")
    return "质检通过" in status or status.upper() in {"QC_PASSED", "PASSED"}


def difficulty_signal_hints(prompt_text: str) -> dict[str, Any]:
    """记录题面里能识别到的复杂信号，只作提示与留痕，不参与判定。"""
    stacked = rule_stack_families(prompt_text)
    easy = p3_easy_facets(prompt_text)
    return {
        "families": sorted(
            name for name, pattern in PROMPT_SIGNAL_HINTS.items() if pattern.search(prompt_text)
        ),
        "mediumShapeMatched": bool(MEDIUM_SHAPE_RE.search(prompt_text)),
        "ruleStackFamilies": stacked,
        "ruleStackCount": len(stacked),
        "ruleStackBlocked": len(stacked) >= RULE_STACK_THRESHOLD,
        "p3Facets": easy,
        "p3FacetLabels": [P3_EASY_FACET_LABELS[name] for name in easy],
        "p3FacetHitCount": len(easy),
        "p3ThresholdReached": len(easy) >= P3_HIT_THRESHOLD,
        "p3SelfContainedShape": bool(P3_SELF_CONTAINED_RE.search(prompt_text)),
    }


def rule_stack_families(prompt_text: str) -> list[str]:
    """统计“单子系统规则堆叠”的六个信号族，命中 ≥4 族就必须补 complexityTopology。"""
    return sorted(name for name, pattern in RULE_STACK_FAMILIES.items() if pattern.search(prompt_text))


def _topology_kind(value: Any) -> str:
    raw = str(value or "").strip()
    if raw in TOPOLOGY_FORBIDDEN_KINDS:
        return raw
    return TOPOLOGY_KIND_ALIASES.get(raw, "")


def _validate_complexity_topology(
    data: dict[str, Any],
    *,
    prompt_text: str,
    clean_prompt: str,
    stack: list[str],
) -> list[str]:
    """规则堆叠命中后，难度论证必须补一份可核对的跨边界拓扑。"""
    errors: list[str] = []
    labels = "、".join(stack)
    topology = data.get("complexityTopology")
    if not isinstance(topology, dict):
        errors.append(
            f"题面命中“单子系统规则堆叠”的 {len(stack)} 个信号族（{labels}）：这些规则都发生在同一个"
            "子系统内部，平台会判为中等。必须在 difficulty-review.json 补 complexityTopology，"
            "写清跨系统对账、失败恢复、迁移兼容、并发冲突、权限边界、容量约束、离线合并或独立所有权拆分"
            "中的哪一种真跨界拓扑成立，才能继续标困难或地狱"
        )
        return errors

    raw_kind = str(topology.get("kind") or "").strip()
    kind = _topology_kind(raw_kind)
    if not raw_kind:
        errors.append("complexityTopology.kind 不能为空")
    elif raw_kind in TOPOLOGY_FORBIDDEN_KINDS:
        errors.append(
            f"complexityTopology.kind={raw_kind} 不能作为复杂拓扑类型：口径更新后失效重算只是本地派生"
            "状态，只有‘口径更新后失效重算’不能证明困难，必须换成真正的跨边界拓扑"
        )
    elif not kind:
        errors.append(
            "complexityTopology.kind 必须是以下之一："
            + "、".join(TOPOLOGY_KINDS)
            + "（可写英文别名 cross-system-reconciliation / failure-recovery / migration / "
            "concurrency / permission-boundary / capacity / offline-merge / ownership-split）"
        )

    quote = str(topology.get("promptQuote") or "").strip()
    normalized_quote = _normalize(quote)
    if len(normalized_quote) < 12:
        errors.append("complexityTopology.promptQuote 至少引用 12 字题面原句")
    elif normalized_quote not in clean_prompt:
        errors.append(f"complexityTopology.promptQuote 不是题面原句，无法核对：{quote}")
    elif raw_kind in TOPOLOGY_KINDS and not TOPOLOGY_BOUNDARY_PATTERNS[raw_kind].search(quote):
        errors.append(
            f"complexityTopology.promptQuote 没有体现所声明的跨边界拓扑（{raw_kind}）："
            "这一句必须本身就能看出跨边界的归属、对账、恢复或拆分关系"
        )

    owners = [str(item).strip() for item in (topology.get("stateOwners") or []) if str(item).strip()]
    if len(owners) < 2:
        errors.append("complexityTopology.stateOwners 至少列出 2 个各自持有状态的系统、部门或岗位")
    for name in owners:
        if _normalize(name) not in clean_prompt:
            errors.append(f"complexityTopology.stateOwners 里的“{name}”没有出现在题面原文里")

    if not _nonempty_text(topology.get("failureOrRecovery"), 20):
        errors.append("complexityTopology.failureOrRecovery 至少 20 字，写清失败、恢复或补偿发生在哪一侧")
    if not _nonempty_text(topology.get("whyNotLocalRuleList"), 30):
        errors.append(
            "complexityTopology.whyNotLocalRuleList 至少 30 字，写清为什么它不是一个子系统内部的规则表"
        )
    if not _nonempty_text(topology.get("negativeOutcome"), 12):
        errors.append("complexityTopology.negativeOutcome 至少 12 字，写清跨界拓扑错了会出现什么可见后果")
    return errors


def _validate_p3_difficulty_floor(
    data: dict[str, Any],
    *,
    prompt_text: str,
    clean_prompt: str,
) -> list[str]:
    """平台 P3 难度下限：四项「过于简单」特征逐项自证，命中两项以上要有跨模块与一轮做不完的证据。

    2026-10-01 实测：一条自包含的“分队离线记录合并回主台账”题面以「困难」提交后被平台按 P3
    废弃，判定真实难度「中等」，理由是同时命中修改范围、上下文依赖、交互轮次、技术广度四项。
    这一层门禁只做两件事：把题面里能识别的特征摊开要求逐项自证，命中达到拒收线时要求真的跨模块。
    """
    errors: list[str] = []
    hinted = p3_easy_facets(prompt_text)
    self_contained = bool(P3_SELF_CONTAINED_RE.search(prompt_text))
    floor = data.get("p3DifficultyFloor")
    if not isinstance(floor, dict):
        errors.append(
            "难度论证缺少 p3DifficultyFloor：平台 P3 把「过于简单」拆成修改范围、上下文依赖、"
            "交互轮次、技术广度四项，命中两项及以上一律拒收；必须逐项引用题面原句、写清反证，"
            "并回答要不要先读懂仓库既有结构、改动是不是跨模块或架构级、模型能不能一轮做完"
        )
        return errors

    facets = floor.get("facets")
    if not isinstance(facets, dict):
        errors.append("p3DifficultyFloor.facets 必须是对象，逐项写 scope/context/rounds/breadth")
        facets = {}
    claimed: list[str] = []
    for key in P3_EASY_FACETS:
        label = P3_EASY_FACET_LABELS[key]
        entry = facets.get(key)
        if not isinstance(entry, dict):
            errors.append(f"p3DifficultyFloor.facets 缺少 {key}（{label}）")
            continue
        if not isinstance(entry.get("hit"), bool):
            errors.append(f"p3DifficultyFloor.facets.{key}.hit 必须是 true/false（{label} 是否命中）")
        quote = _normalize(str(entry.get("promptQuote") or ""))
        if len(quote) < 12:
            errors.append(f"p3DifficultyFloor.facets.{key}.promptQuote 至少引用 12 字题面原句")
        elif quote not in clean_prompt:
            errors.append(
                f"p3DifficultyFloor.facets.{key}.promptQuote 不是题面原句，无法核对："
                f"{entry.get('promptQuote')}"
            )
        if not _nonempty_text(entry.get("counter"), 20):
            errors.append(
                f"p3DifficultyFloor.facets.{key}.counter 至少 20 字：写清 {label} 这一项为什么不成立，"
                "或者改法已经把它抵消"
            )
        if entry.get("hit") is True:
            claimed.append(key)

    # 题面提示命中的项一律计入拒收线：自报未命中也要在 counter 里说清，不能靠少报躲过阈值。
    hit_keys = sorted(set(claimed) | set(hinted))
    hit_count = len(hit_keys)

    cross = floor.get("crossModuleOrArchitecture")
    if not isinstance(cross, dict):
        errors.append(
            "p3DifficultyFloor.crossModuleOrArchitecture 必填：写清本题跨了哪些既有模块、系统或岗位"
        )
        cross = {}
    if not isinstance(cross.get("passed"), bool):
        errors.append("p3DifficultyFloor.crossModuleOrArchitecture.passed 必须是 true/false")
    modules = [str(item).strip() for item in (cross.get("modules") or []) if str(item).strip()]
    if cross.get("passed") is True:
        if len(modules) < 2:
            errors.append("crossModuleOrArchitecture.modules 至少列出 2 个各自持有状态的模块、系统或岗位")
        for name in modules:
            if _normalize(name) not in clean_prompt:
                errors.append(f"crossModuleOrArchitecture.modules 里的“{name}”没有出现在题面原文里")
        quote = str(cross.get("quote") or "")
        clean_quote = _normalize(quote)
        if len(clean_quote) < 12:
            errors.append("crossModuleOrArchitecture.quote 至少引用 12 字题面原句")
        elif clean_quote not in clean_prompt:
            errors.append(f"crossModuleOrArchitecture.quote 不是题面原句，无法核对：{quote}")
        elif not P3_CROSS_BOUNDARY_RE.search(quote):
            errors.append(
                "crossModuleOrArchitecture.quote 没有体现跨模块或跨系统边界：这一句要能看出两个"
                "各自持有状态的系统、部门、岗位或端"
            )
        if not _nonempty_text(cross.get("whyArchitecture"), 30):
            errors.append("crossModuleOrArchitecture.whyArchitecture 至少 30 字")

    repo_context = floor.get("repoContextDependency")
    if not isinstance(repo_context, dict):
        errors.append(
            "p3DifficultyFloor.repoContextDependency 必填：写清要不要先读懂仓库既有结构、数据模型或既有约束"
        )
        repo_context = {}
    if not isinstance(repo_context.get("required"), bool):
        errors.append("p3DifficultyFloor.repoContextDependency.required 必须是 true/false")
    if not _nonempty_text(repo_context.get("whatMustBeRead"), 20):
        errors.append("p3DifficultyFloor.repoContextDependency.whatMustBeRead 至少 20 字")
    if not _nonempty_text(repo_context.get("evidence"), 20):
        errors.append("p3DifficultyFloor.repoContextDependency.evidence 至少 20 字")

    one_round = floor.get("oneRoundEstimate")
    if not isinstance(one_round, dict):
        errors.append("p3DifficultyFloor.oneRoundEstimate 必填：写清模型能不能一轮做完")
        one_round = {}
    if not isinstance(one_round.get("canModelFinishInOneRound"), bool):
        errors.append("p3DifficultyFloor.oneRoundEstimate.canModelFinishInOneRound 必须是 true/false")
    if not _nonempty_text(one_round.get("why"), 30):
        errors.append("p3DifficultyFloor.oneRoundEstimate.why 至少 30 字")

    if hit_count >= P3_HIT_THRESHOLD:
        labels = "、".join(P3_EASY_FACET_LABELS[name] for name in hit_keys)
        if cross.get("passed") is not True:
            errors.append(
                f"题面命中「过于简单」特征 {hit_count} 项（{labels}），达到平台 P3 拒收线："
                "必须证明改动落在两个以上既有模块或真实系统边界上，否则改成跨模块/架构级题目或换题"
            )
        if one_round.get("canModelFinishInOneRound") is not False:
            errors.append(
                f"题面命中「过于简单」特征 {hit_count} 项（{labels}）："
                "p3DifficultyFloor.oneRoundEstimate.canModelFinishInOneRound 必须是 false，"
                "并写清为什么一轮做不完"
            )
    if self_contained and cross.get("passed") is not True:
        errors.append(
            "题面是自包含的合并/对账规格（把一份台账、清单、草稿、备份或数据包合回一处），"
            "正是平台 P3 判「修改范围」过于简单的形态：要么把状态所有权拆到两个以上既有模块或"
            "真实系统，要么改题"
        )
    return errors


def validate_difficulty_evidence(
    data: dict[str, Any],
    *,
    prompt_text: str,
    history_path: Path | None,
    difficulty: str = "困难",
) -> list[str]:
    """把难度论证钉在题面原句和真实历史样本上（2026-09-29 G16 加固）。"""
    errors: list[str] = []
    clean_prompt = _normalize(prompt_text)
    if not clean_prompt:
        return ["难度证据校验缺少题面原文，无法核对难度论证是否落在题面上"]
    if history_path is None or not Path(history_path).is_file():
        errors.append(
            "缺少历史 GSB 缓存，无法核对 corpusEvidence；先运行 "
            "submission/scripts/prompt_dedup.py --task-root ROOT 再安装提示词"
        )

    signals = data.get("platformSignals")
    if not isinstance(signals, dict):
        errors.append(
            "难度论证缺少 platformSignals：逐项回答平台 G16 的多模块整合、关键设计取舍、"
            "复杂技术关注点，并给出题面原句和缺失时的可见后果"
        )
        signals = {}
    for key in PLATFORM_SIGNAL_KEYS:
        label = PLATFORM_SIGNAL_LABELS[key]
        entry = signals.get(key)
        if not isinstance(entry, dict):
            errors.append(f"platformSignals 缺少 {key}（{label}）")
            continue
        if not _nonempty_text(entry.get("answer"), 20):
            errors.append(f"platformSignals.{key}.answer 至少 20 字")
        quote = _normalize(str(entry.get("promptQuote") or ""))
        if len(quote) < 12:
            errors.append(f"platformSignals.{key}.promptQuote 至少引用 12 字题面原句")
        elif quote not in clean_prompt:
            errors.append(
                f"platformSignals.{key}.promptQuote 不是题面原句，无法核对：{entry.get('promptQuote')}"
            )
        if not _nonempty_text(entry.get("ifViolated"), 12):
            errors.append(f"platformSignals.{key}.ifViolated 至少 12 字，写清缺了它会出现什么可见错误")

    invariant = data.get("crossObjectInvariant")
    if not isinstance(invariant, dict):
        errors.append(
            "难度论证缺少 crossObjectInvariant：至少两个业务对象名必须同时出现在题面里，"
            "并写清它们必须守住的不变量和背离时的可见后果"
        )
        invariant = {}
    objects = [str(item).strip() for item in (invariant.get("objects") or []) if str(item).strip()]
    if len(objects) < 2:
        errors.append("crossObjectInvariant.objects 至少列出 2 个业务对象")
    for name in objects:
        if _normalize(name) not in clean_prompt:
            errors.append(f"crossObjectInvariant.objects 里的“{name}”没有出现在题面原文里")
    if not _nonempty_text(invariant.get("invariant"), 20):
        errors.append("crossObjectInvariant.invariant 至少 20 字")
    if not _nonempty_text(invariant.get("divergenceFailure"), 12):
        errors.append("crossObjectInvariant.divergenceFailure 至少 12 字，写清两者不一致时的可见后果")

    corpus = data.get("corpusEvidence")
    if not isinstance(corpus, dict):
        errors.append(
            "难度论证缺少 corpusEvidence：必须核对真实历史语料，写出最近的 G16 废弃样本、"
            "最近的通过样本，以及本题面与它们的差别"
        )
        corpus = {}
    records = _history_records(history_path)
    by_id = {int(item.get("id") or 0): item for item in records}
    if not records and history_path is not None and Path(history_path).is_file():
        errors.append("历史 GSB 缓存为空，无法证明 corpusEvidence 引用的是真实样本")
    discarded_id = corpus.get("nearestDiscardedId")
    try:
        discarded_id_int = int(discarded_id or 0)
    except (TypeError, ValueError):
        discarded_id_int = 0
    if discarded_id_int <= 0:
        errors.append("corpusEvidence.nearestDiscardedId 必须是历史 G16 废弃样本的真实编号")
    elif records:
        item = by_id.get(discarded_id_int)
        if item is None:
            errors.append(f"corpusEvidence.nearestDiscardedId={discarded_id_int} 在历史缓存里不存在")
        elif _history_discard_kind(item) not in DISCARD_RULE_KINDS:
            errors.append(
                f"corpusEvidence.nearestDiscardedId={discarded_id_int} 不是被废弃的样本，"
                "必须挑一条真实的难度废弃记录（G16/G17/P3 或平台状态为已废弃）"
            )
    if not _nonempty_text(corpus.get("differenceFromDiscarded"), 20):
        errors.append("corpusEvidence.differenceFromDiscarded 至少 20 字，逐点写清与废弃样本的差别")
    passed_id = corpus.get("nearestPassedId")
    try:
        passed_id_int = int(passed_id or 0)
    except (TypeError, ValueError):
        passed_id_int = 0
    if passed_id_int <= 0:
        errors.append("corpusEvidence.nearestPassedId 必须是历史质检通过样本的真实编号")
    elif records:
        item = by_id.get(passed_id_int)
        if item is None:
            errors.append(f"corpusEvidence.nearestPassedId={passed_id_int} 在历史缓存里不存在")
        elif not _history_passed(item):
            errors.append(f"corpusEvidence.nearestPassedId={passed_id_int} 不是质检通过的样本")
    if not _nonempty_text(corpus.get("borrowedComplexity"), 20):
        errors.append("corpusEvidence.borrowedComplexity 至少 20 字，说明本题借鉴了通过样本里的哪类复杂度")

    substantive = data.get("substantiveComplexity")
    if not isinstance(substantive, dict):
        errors.append(
            "难度论证缺少 substantiveComplexity：题面必须出现并发、失效重算、失败恢复、迁移兼容、"
            "权限边界、容量约束、离线合并或跨系统对账等实质复杂度信号"
        )
        substantive = {}
    kinds = [str(item).strip() for item in (substantive.get("kinds") or []) if str(item).strip()]
    if not kinds:
        errors.append("substantiveComplexity.kinds 至少提供一项实质复杂度类型")
    unknown = sorted(set(kinds) - set(SUBSTANTIVE_COMPLEXITY_PATTERNS))
    if unknown:
        errors.append("substantiveComplexity.kinds 含未知类型: " + "、".join(unknown))
    supported = [
        kind for kind in kinds
        if kind in SUBSTANTIVE_COMPLEXITY_PATTERNS and SUBSTANTIVE_COMPLEXITY_PATTERNS[kind].search(prompt_text)
    ]
    if not supported:
        errors.append(
            "substantiveComplexity 没有任何类型能由题面原句支撑；审阅回合、去重、冲突提示和快照修订"
            "属于多步实现，不能单独作为困难或地狱信号"
        )
    if difficulty == "地狱" and len(supported) < 2:
        errors.append("地狱题至少需要两类由题面原句支撑的实质复杂度信号")
    quote = str(substantive.get("promptQuote") or "").strip()
    if len(_normalize(quote)) < 12:
        errors.append("substantiveComplexity.promptQuote 至少引用 12 字题面原句")
    elif _normalize(quote) not in clean_prompt:
        errors.append("substantiveComplexity.promptQuote 不是题面原句")
    elif not any(
        pattern.search(quote)
        for kind, pattern in SUBSTANTIVE_COMPLEXITY_PATTERNS.items()
        if kind in kinds
    ):
        errors.append("substantiveComplexity.promptQuote 没有体现所声明的实质复杂度")
    if not _nonempty_text(substantive.get("whyHard"), 20):
        errors.append("substantiveComplexity.whyHard 至少 20 字")
    if not _nonempty_text(substantive.get("visibleFailure"), 12):
        errors.append("substantiveComplexity.visibleFailure 至少 12 字，写清实现错误时的可见后果")

    if MEDIUM_SHAPE_RE.search(prompt_text):
        if not _nonempty_text(data.get("mediumShapeDefense"), 20):
            errors.append(
                "题面命中平台“直接判为中等”的常见形态（在既有模块补视图/清单/预览/字段…），"
                "必须在 mediumShapeDefense 里写清它不是只加一层，并给出题面依据"
            )

    # 2026-10-01 一次加固：平台 P3 先判“是不是过于简单”，G16/G17 够不到这一层。
    errors.extend(
        _validate_p3_difficulty_floor(data, prompt_text=prompt_text, clean_prompt=clean_prompt)
    )

    # 2026-09-30 三次加固：≥4 个规则族命中就说明题面是“单子系统规则堆叠”，
    # 必须用 complexityTopology 证明它真的跨了边界，否则直接阻断。
    stacked = rule_stack_families(prompt_text)
    if len(stacked) >= RULE_STACK_THRESHOLD:
        errors.extend(
            _validate_complexity_topology(
                data,
                prompt_text=prompt_text,
                clean_prompt=clean_prompt,
                stack=stacked,
            )
        )
    return errors


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).strip()


def _shingles(text: str, size: int = 12) -> set[str]:
    cleaned = _normalize(text)
    return {cleaned[index : index + size] for index in range(max(0, len(cleaned) - size + 1))}


def check_history(candidate: str, history_path: Path | None) -> list[str]:
    if history_path is None or not history_path.is_file():
        return []
    data = read_json(history_path, [])
    records: list[Any]
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = data.get("prompts") or data.get("items") or []
    else:
        records = []
    candidate_clean = _normalize(candidate)
    candidate_shingles = _shingles(candidate_clean)
    errors: list[str] = []
    for index, item in enumerate(records):
        if isinstance(item, dict):
            prompt = str(item.get("prompt") or item.get("user_prompt") or item.get("text") or "")
            label = str(item.get("id") or item.get("projectCode") or index)
        else:
            prompt = str(item)
            label = str(index)
        other = _normalize(prompt)
        if not other:
            continue
        if candidate_clean == other:
            errors.append(f"提示词与历史记录 {label} 精确重复")
            continue
        overlap = candidate_shingles & _shingles(other)
        if overlap:
            sample = sorted(overlap, key=len, reverse=True)[0]
            errors.append(f"提示词与历史记录 {label} 存在连续片段复用: {sample}")
    return errors


TECHNICAL_CONCERN_KINDS = {
    "concurrency",
    "idempotency",
    "transaction",
    "permission",
    "compatibility",
    "migration",
    "performance",
    "failure-recovery",
    "state-machine",
    "security",
    "offline-sync",
}

# 2026-09-29 G17：三项信号全部通过也不再自动等于“地狱”。
# 平台会把“多模块 + 事务/幂等 + 确定性排布”判回困难，只有额外证明题面存在
# 开放方案判断、隐蔽约束、架构级取舍或多步深水调试，才允许填写地狱。
HELL_SIGNAL_KINDS = {
    "open-ended",
    "hidden-constraints",
    "architecture-level",
    "multi-step-debugging",
    "cross-system-recovery",
}

# 2026-09-30 G17 实测（sologsb-1117 · 蜜蜂授粉路线规划器）：题面把容量排队、引用失效重算、
# 离线合并冲突复核、旧数据升级迁移串成一条本地链路，三项平台信号里只有多模块整合和复杂技术
# 关注点成立，平台原话是“涉及容量排队、引用失效重算、离线合并冲突复核、旧数据升级迁移，多模块
# 约束交织，但需求边界明确”，结论是“难度标高了，请改填困难”。说明只靠自述“开放/隐蔽/架构”
# 挡不住难度虚高，地狱必须由题面原句本身证明。
HELL_PROMPT_MARKERS: dict[str, re.Pattern[str]] = {
    "open-ended": re.compile(
        r"(开放|由执行者(自行)?(确定|决定)|方案(自定|自行确定)|自行设计|没有给出(具体|明确)(做法|方案)"
        r"|需要在[^。；]{0,16}之间(选|做|权衡)|自己(定|决定)(方案|做法|边界))"
    ),
    "hidden-constraints": re.compile(
        r"(隐蔽|不易察觉|隐含|没有明说|未明说|藏在|互相牵制|互相制约|环环相扣|牵一发动全身"
        r"|只有[^。；]{0,12}才(知道|暴露|发现))"
    ),
    "architecture-level": re.compile(
        r"(架构|整体方案|事务边界|数据所有权|所有权|分层设计|跨系统|跨库|跨端|边界划分|服务拆分)"
    ),
    "multi-step-debugging": re.compile(
        r"(深水|多步调试|逐层排查|反复排查|疑难|难以复现|偶发|竞态|时序错乱|需要调试)"
    ),
    "cross-system-recovery": re.compile(
        r"(跨系统|对账|补偿|双写|重启后恢复|断网后恢复|故障后恢复|人工对账)"
    ),
}
# 关键设计取舍必须由题面原句体现“两条约束互相牵制”，不能只写在难度论证的自由文字里。
TRADEOFF_MARKER_RE = re.compile(
    r"(但|却|既要|又要|同时要|互相矛盾|互相牵制|互相制约|取舍|二选一|顾此失彼|宁可|优先级冲突)"
)


def _nonempty_text(value: Any, minimum: int = 12) -> bool:
    return len(_normalize(str(value or ""))) >= minimum


def hell_prompt_markers(prompt_text: str, kinds: list[str] | None = None) -> list[str]:
    """返回题面里能由原句支撑的地狱级信号族，只认题面文字，不看难度论证自述。"""
    names = [name for name in (kinds or HELL_PROMPT_MARKERS.keys()) if name in HELL_PROMPT_MARKERS]
    return sorted(name for name in names if HELL_PROMPT_MARKERS[name].search(prompt_text))


def validate_difficulty_review(
    path: Path,
    difficulty: str,
    *,
    prompt_text: str = "",
    history_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the structured difficulty proof required by platform G16.

    ``困难`` needs at least two of the three platform signals, with design
    tradeoff or a complex technical concern among them. ``地狱`` needs all
    three. This deliberately rejects multi-file CRUD and ordinary validation.
    """
    if not path.is_file():
        return {"ok": False, "errors": [f"缺少困难/地狱难度论证文件: {path}"]}
    try:
        data = read_json(path, {})
    except Exception as exc:
        return {"ok": False, "errors": [f"难度论证文件不可解析: {exc}"]}
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["难度论证文件顶层必须是 JSON 对象"]}

    errors: list[str] = []
    if data.get("schemaVersion") != 1:
        errors.append("难度论证 schemaVersion 必须是 1")
    if str(data.get("difficulty") or "") != difficulty:
        errors.append(f"难度论证 difficulty 必须与命令行一致: {difficulty}")

    signals = data.get("signals")
    if not isinstance(signals, dict):
        errors.append("难度论证缺少 signals 对象")
        signals = {}

    multi = signals.get("multiModule") if isinstance(signals.get("multiModule"), dict) else {}
    tradeoff = signals.get("designTradeoff") if isinstance(signals.get("designTradeoff"), dict) else {}
    concern = signals.get("complexConcern") if isinstance(signals.get("complexConcern"), dict) else {}
    passed = {
        "multiModule": bool(multi.get("passed")),
        "designTradeoff": bool(tradeoff.get("passed")),
        "complexConcern": bool(concern.get("passed")),
    }

    modules = multi.get("modules")
    if passed["multiModule"]:
        if not isinstance(modules, list) or len([item for item in modules if str(item).strip()]) < 3:
            errors.append("multiModule 通过时 modules 至少列出 3 个已有业务模块或边界上下文")
        if not _nonempty_text(multi.get("crossModuleInvariant"), 20):
            errors.append("multiModule 缺少至少 20 字的 crossModuleInvariant")
        if not _nonempty_text(multi.get("evidence"), 20):
            errors.append("multiModule 缺少至少 20 字的 evidence")

    if passed["designTradeoff"]:
        for key in ("conflict", "decision", "whyNotRoutine", "evidence"):
            if not _nonempty_text(tradeoff.get(key), 20):
                errors.append(f"designTradeoff 缺少至少 20 字的 {key}")

    if passed["complexConcern"]:
        kinds = concern.get("kinds")
        if not isinstance(kinds, list) or not kinds:
            errors.append("complexConcern 通过时 kinds 必须是非空数组")
        else:
            unknown = sorted({str(item) for item in kinds if str(item) not in TECHNICAL_CONCERN_KINDS})
            if unknown:
                errors.append("complexConcern.kinds 含未知类型: " + "、".join(unknown))
        for key in ("technicalRisk", "observableFailure", "evidence"):
            if not _nonempty_text(concern.get(key), 20):
                errors.append(f"complexConcern 缺少至少 20 字的 {key}")

    if data.get("routineOnly") is not False:
        errors.append("routineOnly 必须明确为 false；常规 CRUD、单模块字段或表单校验不能标困难")
    passed_count = sum(1 for value in passed.values() if value)
    if passed_count < 2:
        errors.append("困难/地狱题至少满足多模块整合、关键设计取舍、复杂技术关注点中的两项")
    if not (passed["designTradeoff"] or passed["complexConcern"]):
        errors.append("至少满足关键设计取舍或复杂技术关注点之一，不能只靠多模块堆文件")
    if difficulty == "地狱" and passed_count < 3:
        errors.append("地狱题必须同时满足三项难度特征")
    if difficulty == "地狱":
        hell = data.get("hellSignal") if isinstance(data.get("hellSignal"), dict) else {}
        if hell.get("passed") is not True:
            errors.append("地狱题还必须提供 hellSignal.passed=true，证明题目存在开放方案判断、隐蔽约束、架构级取舍或多步深水调试")
        hell_kinds = hell.get("kinds")
        if not isinstance(hell_kinds, list) or not hell_kinds:
            errors.append("hellSignal.kinds 必须是非空数组")
            hell_kinds = []
        else:
            unknown = sorted({str(item) for item in hell_kinds if str(item) not in HELL_SIGNAL_KINDS})
            if unknown:
                errors.append("hellSignal.kinds 含未知类型: " + "、".join(unknown))
        for key in ("reason", "evidence"):
            if not _nonempty_text(hell.get(key), 20):
                errors.append(f"hellSignal 缺少至少 20 字的 {key}")
        # 2026-09-30 G17：难度标高的真实原因是“地狱级特征”和“关键设计取舍”只写在自述里。
        # 从本版本起，地狱必须由题面原句证明这两项，引不到题面就说明只能填困难。
        if prompt_text:
            clean_prompt_text = _normalize(prompt_text)
            hell_quote_raw = str(hell.get("promptQuote") or "")
            hell_quote = _normalize(hell_quote_raw)
            if len(hell_quote) < 12:
                errors.append(
                    "地狱题必须在 hellSignal.promptQuote 里逐字引用至少 12 字的题面原句，"
                    "证明开放方案判断、隐蔽约束、架构级取舍或多步深水调试；引不到题面就改填「困难」"
                )
            elif hell_quote not in clean_prompt_text:
                errors.append(
                    f"hellSignal.promptQuote 不是题面原句，无法核对：{hell_quote_raw}"
                    "；引不到题面原句就改填「困难」"
                )
            elif not [
                name
                for name in hell_kinds
                if str(name) in HELL_PROMPT_MARKERS
                and HELL_PROMPT_MARKERS[str(name)].search(hell_quote_raw)
            ]:
                errors.append(
                    "hellSignal.promptQuote 没有体现所声明的地狱级特征（"
                    + "、".join(str(item) for item in hell_kinds)
                    + "）；题面本身看不出地狱级特征时请改填「困难」"
                )
            tradeoff_quote_raw = str(tradeoff.get("promptQuote") or "")
            tradeoff_quote = _normalize(tradeoff_quote_raw)
            if not passed["designTradeoff"]:
                errors.append(
                    "地狱题必须同时满足关键设计取舍；题面只是在同一个子系统里做确定性排布或"
                    "多步实现时请改填「困难」"
                )
            elif len(tradeoff_quote) < 12:
                errors.append(
                    "地狱题必须在 signals.designTradeoff.promptQuote 里逐字引用至少 12 字题面原句，"
                    "证明两条业务约束互相牵制；引不到题面原句就改填「困难」"
                )
            elif tradeoff_quote not in clean_prompt_text:
                errors.append(
                    f"designTradeoff.promptQuote 不是题面原句，无法核对：{tradeoff_quote_raw}"
                    "；引不到题面原句就改填「困难」"
                )
            elif not TRADEOFF_MARKER_RE.search(tradeoff_quote_raw):
                errors.append(
                    "designTradeoff.promptQuote 没有体现两条互相牵制的约束"
                    "（例如题面里出现“但/却/既要…又要/取舍”）；只有确定性规则时请改填「困难」"
                )
    if prompt_text:
        errors.extend(
            validate_difficulty_evidence(
                data,
                prompt_text=prompt_text,
                history_path=history_path,
                difficulty=difficulty,
            )
        )
    if str(data.get("verdict") or "") != difficulty:
        errors.append(f"难度论证 verdict 必须是 {difficulty}")
    if not str(data.get("reviewedBy") or "").strip():
        errors.append("难度论证缺少 reviewedBy")

    return {
        "ok": not errors,
        "errors": errors,
        "passed": passed,
        "signalsPassed": passed_count,
        "ruleStackFamilies": rule_stack_families(prompt_text) if prompt_text else [],
        "hellPromptMarkers": hell_prompt_markers(prompt_text) if prompt_text else [],
        "p3Facets": p3_easy_facets(prompt_text) if prompt_text else [],
        "p3FacetLabels": (
            [P3_EASY_FACET_LABELS[name] for name in p3_easy_facets(prompt_text)] if prompt_text else []
        ),
    }


def validate_candidate(
    candidate_path: Path,
    review_path: Path,
    *,
    task_type: str,
    difficulty: str,
    difficulty_review_path: Path | None = None,
    history_path: Path | None = None,
    allow_over_170: bool = False,
) -> dict[str, Any]:
    if task_type not in TASK_TYPES:
        if task_type == "代码理解":
            raise SologsbError("本期暂时排除代码理解")
        raise SologsbError(f"未知任务类型: {task_type}")
    if difficulty not in DIFFICULTIES:
        raise SologsbError("任务难度只允许 困难 或 地狱")
    if not candidate_path.is_file():
        raise SologsbError(f"候选提示词不存在: {candidate_path}")
    if not review_path.is_file():
        raise SologsbError(f"ra-人话审核记录不存在: {review_path}")
    text = candidate_path.read_text(encoding="utf-8")
    clean = text.strip()
    difficulty_review = validate_difficulty_review(
        difficulty_review_path or Path("__missing_difficulty_review__.json"),
        difficulty,
        prompt_text=clean,
        history_path=history_path,
    )
    errors: list[str] = list(difficulty_review.get("errors") or [])
    if not clean:
        errors.append("提示词为空")
    if len(clean) > 240:
        errors.append(f"提示词超过 240 字，当前 {len(clean)} 字")
    if len(clean) > 170 and not allow_over_170:
        errors.append(f"首轮提示词优先上限 170 字，当前 {len(clean)} 字；确有必要才显式允许")
    if MARKDOWN.search(clean):
        errors.append("包含 Markdown 格式")
    if PATH_OR_CODE.search(clean):
        errors.append("包含文件路径或代码术语")
    if GUIDED_FIX.search(clean):
        errors.append("包含引导性修复措辞")
    if ROUND_REFERENCE.search(clean):
        errors.append("包含轮次表述")
    warnings: list[str] = []
    hints = difficulty_signal_hints(clean)
    if hints["families"]:
        warnings.append("题面识别到复杂信号：" + "、".join(hints["families"]))
    else:
        warnings.append("题面没有识别到并发/失效/原子性/迁移/权限/恢复/容量类复杂信号，复核时重点看这一项")
    if hints["mediumShapeMatched"]:
        warnings.append("题面命中“在既有模块补一层”的中等形态，必须已填写 mediumShapeDefense")
    if hints["ruleStackBlocked"]:
        warnings.append(
            "题面命中“单子系统规则堆叠”的 "
            + str(hints["ruleStackCount"])
            + " 个信号族（"
            + "、".join(hints["ruleStackFamilies"])
            + "），难度论证必须补齐 complexityTopology，否则阻断"
        )
    if hints["p3FacetHitCount"]:
        warnings.append(
            "题面命中平台 P3「过于简单」特征 "
            + str(hints["p3FacetHitCount"])
            + " 项（"
            + "、".join(hints["p3FacetLabels"])
            + "），难度论证必须逐项自证 p3DifficultyFloor，命中两项以上还要证明跨模块且一轮做不完"
        )
    rigid = PROMPT_RIGID_RE.findall(clean)
    if len(rigid) > PROMPT_MAX_RIGID:
        errors.append(
            f"“只能/必须/不得”这类硬性措辞出现 {len(rigid)} 次，读起来像条款；"
            f"最多 {PROMPT_MAX_RIGID} 处，其余改成业务后果，例如“过期了就提示对方先刷新”"
        )
    semicolons = clean.count("；")
    if semicolons > PROMPT_MAX_SEMICOLONS:
        errors.append(f"分号 {semicolons} 个，规则被逐条罗列；拆成几句连贯的话，最多 {PROMPT_MAX_SEMICOLONS} 个")
    if PROMPT_TEMPLATE_TAIL_RE.search(clean):
        errors.append("结尾是“刷新后/并发后……一致”式模板收尾；改成本题特有的可观察结果，或并进前文")
    template_words = sorted(set(PROMPT_TEMPLATE_WORD_RE.findall(clean)))
    if template_words:
        warnings.append("出现历史提示词里反复使用的套话：" + "、".join(template_words) + "；建议换成本题自己的说法")
    errors.extend(check_history(clean, history_path))

    if VALIDATE_PROMPT.is_file():
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_PROMPT), str(candidate_path), "--review", str(review_path), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            result = json.loads(proc.stdout)
            errors.extend(result.get("errors") or [])
        except json.JSONDecodeError:
            errors.append(proc.stderr.strip() or proc.stdout.strip() or "提示词校验器没有返回 JSON")
    else:
        errors.append(f"缺少提示词校验器: {VALIDATE_PROMPT}")

    if AUDIT_HUMAN.is_file():
        proc = subprocess.run(
            [sys.executable, str(AUDIT_HUMAN), "--text-file", str(candidate_path), "--target-type", "prompt", "--review", str(review_path), "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            result = json.loads(proc.stdout)
            errors.extend(result.get("errors") or [])
        except json.JSONDecodeError:
            errors.append(proc.stderr.strip() or proc.stdout.strip() or "ra-人话审核器没有返回 JSON")
    else:
        errors.append(f"缺少 ra-人话审核器: {AUDIT_HUMAN}")

    errors = list(dict.fromkeys(error for error in errors if error))
    return {
        "ok": not errors,
        "taskType": task_type,
        "difficulty": difficulty,
        "charCount": len(clean),
        "textSha256": sha256_bytes(text.encode("utf-8")),
        "errors": errors,
        "warnings": warnings,
        "difficultyReview": difficulty_review,
        "difficultyHints": hints,
    }


DIFFICULTY_TEMPLATE = Path(__file__).resolve().parents[1] / "references" / "difficulty-review-template.json"


def ensure_difficulty_template(path: Path) -> bool:
    """难度论证缺失时铺一份 1.7.9 模板，避免出题人凭记忆漏填回指字段。"""
    if path.is_file() or not DIFFICULTY_TEMPLATE.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy(DIFFICULTY_TEMPLATE, path)
    return True


def install_prompt(
    task_root: Path,
    *,
    candidate_path: Path,
    review_path: Path,
    task_type: str,
    difficulty: str,
    difficulty_review_path: Path,
    history_path: Path | None = None,
    allow_over_170: bool = False,
) -> dict[str, Any]:
    scaffolded = ensure_difficulty_template(difficulty_review_path)
    result = validate_candidate(
        candidate_path,
        review_path,
        task_type=task_type,
        difficulty=difficulty,
        difficulty_review_path=difficulty_review_path,
        history_path=history_path,
        allow_over_170=allow_over_170,
    )
    review_dir = task_root / "monitor" / "prompt"
    review_dir.mkdir(parents=True, exist_ok=True)
    write_json(review_dir / "validation.json", result)
    atomic_copy(review_path, review_dir / "ra-renhua-review.json")
    if difficulty_review_path.is_file():
        atomic_copy(difficulty_review_path, review_dir / "difficulty-review.json")
    if not result["ok"]:
        raise SologsbError("提示词门禁未通过:\n- " + "\n- ".join(result["errors"]))
    destination = task_root / "workspace" / "评审文件" / "提示词.md"
    text = candidate_path.read_text(encoding="utf-8")
    atomic_write_text(destination, text)
    atomic_write_text(task_root / "workspace" / "评审文件" / "提示词.sha256", result["textSha256"] + "\n")
    state = read_json(task_root / "monitor" / "state.json", {})
    state.update(
        {
            "status": "prompt_ready",
            "taskType": task_type,
            "difficulty": difficulty,
            "promptPath": str(destination),
            "promptSha256": result["textSha256"],
            "difficultyReviewPath": str(review_dir / "difficulty-review.json"),
            "difficultySignalsPassed": (result.get("difficultyReview") or {}).get("signalsPassed"),
        }
    )
    save_state(task_root, state)
    return {
        **result,
        "promptPath": str(destination),
        "difficultyTemplateScaffolded": scaffolded,
    }
