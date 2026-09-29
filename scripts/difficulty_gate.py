#!/usr/bin/env python3
"""Validate the auditable difficulty contract used before prompt installation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import SologsbError, read_json, sha256_file, write_json

SCHEMA_VERSION = 1
ALLOWED_AXES = {
    "multi_module",
    "design_tradeoff",
    "complex_state",
    "concurrency",
    "compatibility",
    "permissions",
    "performance",
    "exception_recovery",
    "migration_consistency",
}
CORE_HARD_AXES = {"multi_module", "design_tradeoff", "complex_state"}
FAILURE_WORDS = (
    "失败",
    "丢失",
    "覆盖",
    "不一致",
    "拒绝",
    "阻塞",
    "死锁",
    "回滚",
    "报错",
    "超限",
    "泄漏",
    "越权",
    "重复",
    "中断",
    "无法",
    "异常",
    "性能下降",
    "数据损坏",
)


def _nonempty(value: Any) -> str:
    return str(value or "").strip()


def _normalize_anchor(raw: str, origin: Path) -> Path | None:
    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        candidate = path.resolve(strict=False)
    else:
        if ".." in path.parts:
            return None
        candidate = (origin / path).resolve(strict=False)
    try:
        candidate.relative_to(origin.resolve())
    except ValueError:
        return None
    return candidate


def validate_difficulty_audit(
    audit_path: Path,
    *,
    task_root: Path,
    difficulty: str,
    prompt_text: str,
) -> dict[str, Any]:
    """Validate the submitted difficulty definition against source and prompt."""
    if not audit_path.is_file():
        raise SologsbError(f"难度证明文件不存在: {audit_path}")
    document = read_json(audit_path, {})
    if not isinstance(document, dict):
        raise SologsbError("难度证明必须是 JSON 对象")

    errors: list[str] = []
    origin = (task_root / "source" / "origin").resolve()
    if not origin.is_dir():
        errors.append(f"缺少源码目录，无法核对难度锚点: {origin}")

    if document.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"难度证明 schemaVersion 必须为 {SCHEMA_VERSION}")
    if _nonempty(document.get("difficulty")) != difficulty:
        errors.append(f"难度证明 difficulty 必须为 {difficulty}")
    summary = _nonempty(document.get("summary"))
    if len(summary) < 40:
        errors.append("难度证明 summary 过短，需写清为什么不是中等题")
    reviewed_by = _nonempty(document.get("reviewedBy"))
    if not reviewed_by:
        errors.append("难度证明 reviewedBy 不能为空")
    medium_counterexample = _nonempty(document.get("mediumCounterexample"))
    if len(medium_counterexample) < 40 or "中等" not in medium_counterexample:
        errors.append("mediumCounterexample 需写出一个同类中等题的边界并明确写“中等”")

    raw_axes = document.get("axes")
    if not isinstance(raw_axes, list):
        errors.append("难度证明 axes 必须是数组")
        raw_axes = []
    if len(raw_axes) < 2:
        errors.append("困难/地狱题至少需要两个独立困难轴")

    kinds: list[str] = []
    anchor_values: set[str] = set()
    valid_axes: list[dict[str, Any]] = []
    for index, raw_axis in enumerate(raw_axes, 1):
        label = f"axes[{index}]"
        if not isinstance(raw_axis, dict):
            errors.append(f"{label} 必须是对象")
            continue
        kind = _nonempty(raw_axis.get("kind"))
        if kind not in ALLOWED_AXES:
            errors.append(f"{label}.kind 无效: {kind!r}")
        else:
            kinds.append(kind)
        prompt_quote = _nonempty(raw_axis.get("promptQuote"))
        if not prompt_quote:
            errors.append(f"{label}.promptQuote 不能为空")
        elif prompt_quote not in prompt_text:
            errors.append(f"{label}.promptQuote 未在最终提示词中原样出现")
        why = _nonempty(raw_axis.get("whyNotStraightforward"))
        if len(why) < 40:
            errors.append(f"{label}.whyNotStraightforward 过短，需说明为什么不能顺序实现")
        failure = _nonempty(raw_axis.get("observableFailure"))
        if len(failure) < 12 or not any(word in failure for word in FAILURE_WORDS):
            errors.append(f"{label}.observableFailure 需写出可观察失败后果")
        raw_anchors = raw_axis.get("sourceAnchors")
        if not isinstance(raw_anchors, list) or not raw_anchors:
            errors.append(f"{label}.sourceAnchors 必须是非空数组")
            raw_anchors = []
        axis_anchor_count = 0
        for anchor_index, raw_anchor in enumerate(raw_anchors, 1):
            anchor = _normalize_anchor(_nonempty(raw_anchor), origin)
            if anchor is None:
                errors.append(f"{label}.sourceAnchors[{anchor_index}] 越出源码目录或为空")
                continue
            if not anchor.exists():
                errors.append(f"{label}.sourceAnchors[{anchor_index}] 不存在: {raw_anchor}")
                continue
            axis_anchor_count += 1
            if anchor.is_file():
                anchor_values.add(str(anchor.relative_to(origin)))
            else:
                anchor_values.add(str(anchor.relative_to(origin)))
        if axis_anchor_count == 0:
            errors.append(f"{label} 至少需要一个能落到源码的锚点")
        valid_axes.append(
            {
                "kind": kind,
                "promptQuote": prompt_quote,
                "sourceAnchors": [str(item) for item in raw_anchors],
                "whyNotStraightforward": why,
                "observableFailure": failure,
            }
        )

    if len(set(kinds)) < 2:
        errors.append("不同困难轴至少要有两项，不能把同一件事拆成两个标签")
    if not (set(kinds) & CORE_HARD_AXES):
        errors.append("至少一项困难轴必须属于多模块整合、关键设计取舍或复杂状态")
    if len(anchor_values) < 3:
        errors.append("难度证明至少要有三个不同源码锚点，避免同一文件内的小改动冒充困难题")

    result = {
        "ok": not errors,
        "schemaVersion": SCHEMA_VERSION,
        "difficulty": difficulty,
        "summary": summary,
        "mediumCounterexample": medium_counterexample,
        "axes": valid_axes,
        "axisKinds": sorted(set(kinds)),
        "sourceAnchors": sorted(anchor_values),
        "anchorCount": len(anchor_values),
        "reviewedBy": reviewed_by,
        "auditPath": str(audit_path.resolve()),
        "auditSha256": sha256_file(audit_path),
        "errors": errors,
    }
    return result


def install_difficulty_audit(
    task_root: Path,
    *,
    audit_path: Path,
    difficulty: str,
    prompt_text: str,
) -> dict[str, Any]:
    result = validate_difficulty_audit(
        audit_path,
        task_root=task_root,
        difficulty=difficulty,
        prompt_text=prompt_text,
    )
    if result["errors"]:
        raise SologsbError("难度证明未通过:\n- " + "\n- ".join(result["errors"]))
    destination = task_root / "monitor" / "prompt" / "difficulty-audit.json"
    write_json(destination, read_json(audit_path, {}))
    result["path"] = str(destination.resolve())
    return result
