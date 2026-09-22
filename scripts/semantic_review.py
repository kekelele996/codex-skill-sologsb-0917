#!/usr/bin/env python3
"""Build and validate semantic completion reviews for A/B runs."""
from __future__ import annotations

import json

from pathlib import Path
from typing import Any

from common import SologsbError, read_json, write_json
from trace_validator import load_trace


def _event_brief(event: dict[str, Any]) -> dict[str, Any]:
    message = event.get("message") or {}
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    tools: list[dict[str, str]] = []
    texts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            payload = block.get("input") if isinstance(block.get("input"), dict) else {}
            tools.append(
                {
                    "name": str(block.get("name") or ""),
                    "summary": str(
                        payload.get("command")
                        or payload.get("file_path")
                        or payload.get("path")
                        or payload.get("pattern")
                        or ""
                    )[:500],
                }
            )
        elif block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
    return {
        "index": int(event.get("_line") or 0) - 1,
        "uuid": str(event.get("uuid") or ""),
        "type": str(event.get("type") or ""),
        "stopReason": str(message.get("stop_reason") or ""),
        "texts": texts,
        "tools": tools,
    }


def build_packet(task_root: Path, side: str) -> dict[str, Any]:
    side = side.upper()
    state = read_json(task_root / "monitor" / "state.json", {})
    side_state = (state.get("sides") or {}).get(side) or {}
    if side_state.get("status") not in {"staged", "clean"}:
        raise SologsbError(f"{side} 尚未完成结构校验")
    trace_path = Path(str(side_state.get("tracePath") or ""))
    events = load_trace(trace_path)
    final_text = ""
    for event in reversed(events):
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            final_text = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if final_text.strip():
            break
    packet = {
        "schemaVersion": 1,
        "side": side,
        "prompt": Path(str(state.get("promptPath"))).read_text(encoding="utf-8"),
        "sessionId": side_state.get("sessionId", ""),
        "tracePath": str(trace_path),
        "finalText": final_text,
        "changedFiles": side_state.get("changedFiles") or [],
        "diffStat": side_state.get("diffStat") or "",
        "status": side_state.get("status"),
        "recentEvents": [_event_brief(event) for event in events[-80:]],
        "reviewSchema": {
            "side": side,
            "completed": True,
            "interrupted": False,
            "unfinished": [],
            "requirements": [
                {
                    "requirement": "提示词中的一条明确需求",
                    "status": "satisfied",
                    "evidence": [
                        {"type": "trace", "eventIndex": 0, "quote": "轨迹证据"},
                        {"type": "artifact", "path": "相对文件路径", "quote": "代码或 diff 证据"},
                    ],
                }
            ],
            "reason": "为什么可以判定本轮完成",
            "reviewedBy": "Codex",
        },
    }
    output = task_root / "monitor" / "semantic" / f"{side.lower()}.packet.json"
    write_json(output, packet)
    return packet


def validate_review(task_root: Path, side: str, review_path: Path) -> dict[str, Any]:
    side = side.upper()
    state = read_json(task_root / "monitor" / "state.json", {})
    side_state = (state.get("sides") or {}).get(side) or {}
    if side_state.get("status") not in {"staged", "clean"}:
        raise SologsbError(f"{side} 尚未完成结构校验")
    review = read_json(review_path)
    errors: list[str] = []
    if not isinstance(review, dict):
        raise SologsbError(f"{side} 语义审核不是 JSON 对象")
    if review.get("side") != side:
        errors.append(f"side 必须为 {side}")
    if review.get("completed") is not True:
        errors.append("completed 必须为 true；未完成时应重跑本侧")
    if review.get("interrupted") is not False:
        errors.append("interrupted 必须为 false；发生中断时应重跑本侧")
    if review.get("unfinished") not in ([], None):
        errors.append("unfinished 必须为空")
    requirements = review.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append("requirements 必须是非空数组")
        requirements = []
    trace_path = Path(str(side_state.get("tracePath") or ""))
    try:
        trace_events = load_trace(trace_path)
    except Exception as exc:
        trace_events = []
        errors.append(f"轨迹不可用: {exc}")
    configured_repo = str(side_state.get("workspacePath") or "")
    candidate = str(side_state.get("candidateId") or "")
    if configured_repo:
        repo = Path(configured_repo)
    elif candidate:
        repo = task_root / "source" / "candidates" / candidate
    else:
        repo = task_root / "source" / side.lower()
    for index, item in enumerate(requirements, 1):
        if not isinstance(item, dict):
            errors.append(f"requirement {index} 不是对象")
            continue
        requirement = str(item.get("requirement") or "").strip()
        if not requirement:
            errors.append(f"requirement {index} 缺少 requirement")
        if item.get("status") != "satisfied":
            errors.append(f"requirement {index} 状态不是 satisfied")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"requirement {index} 缺少 evidence")
            continue
        for evidence_index, evidence_item in enumerate(evidence, 1):
            if not isinstance(evidence_item, dict):
                errors.append(f"requirement {index} evidence {evidence_index} 不是对象")
                continue
            kind = str(evidence_item.get("type") or "")
            quote = str(evidence_item.get("quote") or "").strip()
            if not quote:
                errors.append(f"requirement {index} evidence {evidence_index} 缺少 quote")
            if kind == "trace":
                event_index = evidence_item.get("eventIndex")
                if not isinstance(event_index, int) or event_index < 0 or event_index >= len(trace_events):
                    errors.append(f"requirement {index} trace evidence eventIndex 无效")
                elif quote and quote not in json.dumps(trace_events[event_index], ensure_ascii=False):
                    errors.append(f"requirement {index} trace evidence quote 未命中轨迹事件")
            elif kind == "artifact":
                relative = str(evidence_item.get("path") or "").strip()
                if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                    errors.append(f"requirement {index} artifact path 必须是仓库内相对路径")
                elif not (repo / relative).exists():
                    errors.append(f"requirement {index} artifact path 不存在: {relative}")
            else:
                errors.append(f"requirement {index} evidence type 必须是 trace/artifact")
    reason = str(review.get("reason") or "").strip()
    if len(reason) < 20:
        errors.append("reason 少于 20 个字符")
    if not str(review.get("reviewedBy") or "").strip():
        errors.append("reviewedBy 不能为空")
    result = {
        "ok": not errors,
        "side": side,
        "reviewPath": str(review_path.resolve()),
        "errors": errors,
    }
    write_json(task_root / "monitor" / "semantic" / f"{side.lower()}.validation.json", result)
    if errors:
        raise SologsbError(f"{side} 语义审核未通过:\n- " + "\n- ".join(errors))
    return result


def ensure_packets(task_root: Path, sides: tuple[str, ...] = ("A", "B")) -> dict[str, str]:
    packets: dict[str, str] = {}
    for side in sides:
        side = side.upper()
        if side not in {"A", "B"}:
            raise SologsbError(f"未知 side: {side}")
        build_packet(task_root, side)
        packets[side] = str((task_root / "monitor" / "semantic" / f"{side.lower()}.packet.json").resolve())
    return packets
