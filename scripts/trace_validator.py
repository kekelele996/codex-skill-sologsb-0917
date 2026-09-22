#!/usr/bin/env python3
"""Strict validators for Claude Code native JSONL traces."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import SologsbError, sha256_file

API_ERROR = re.compile(
    r"\bapi error\b|\b429\b|\b5(?:02|03|04)\b|rate limit|overloaded|"
    r"bad gateway|service unavailable|gateway time-?out",
    re.I,
)
PERMISSION = re.compile(r"askuserquestion|需要你确认|请确认|请提供更多信息|等待你的输入|权限询问", re.I)


def _content_text(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []
    texts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        blocks.append(item)
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            texts.append(item["text"])
    return "\n".join(texts), blocks


def _real_user_prompts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompts: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if event.get("type") != "user" or event.get("isMeta") is True:
            continue
        message = event.get("message") or {}
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text, blocks = _content_text(message.get("content"))
        if blocks and all(block.get("type") == "tool_result" for block in blocks):
            continue
        if not text.strip():
            continue
        prompts.append({"eventIndex": index, "text": text, "uuid": event.get("uuid", "")})
    return prompts


def load_trace(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        raise SologsbError(f"轨迹不存在或为空: {path}")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SologsbError(f"轨迹第 {line_number} 行不是 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise SologsbError(f"轨迹第 {line_number} 行不是对象")
        value["_line"] = line_number
        events.append(value)
    if not events:
        raise SologsbError(f"轨迹没有事件: {path}")
    return events


def validate_single_round(
    trace_path: Path,
    *,
    expected_prompt: str,
    expected_session_id: str = "",
) -> dict[str, Any]:
    events = load_trace(trace_path)
    errors: list[str] = []
    expected_session_id = expected_session_id.strip()
    session_values = [
        str(event.get("sessionId") or "").strip()
        for event in events
        if str(event.get("sessionId") or "").strip()
    ]
    session_ids = set(session_values)
    if not expected_session_id:
        errors.append("预期 SessionID 为空")
    if not session_values:
        errors.append("轨迹文件内容不包含 SessionID")
    elif len(session_ids) != 1:
        errors.append(f"SessionID 必须唯一，当前为 {sorted(session_ids)}")
    session_id = next(iter(session_ids), "")
    if expected_session_id and session_id and session_id != expected_session_id:
        errors.append(f"SessionID 不匹配: {session_id} != {expected_session_id}")

    prompts = _real_user_prompts(events)
    if len(prompts) != 1:
        errors.append(f"真人 user prompt 必须恰好一个，当前 {len(prompts)}")
    elif prompts[0]["text"].strip() != expected_prompt.strip():
        errors.append("轨迹 user prompt 与唯一提示词不完全一致")

    assistant_indexes = [i for i, event in enumerate(events) if event.get("type") == "assistant"]
    if not assistant_indexes:
        errors.append("轨迹没有 assistant 事件")
        final_index = -1
        stop_reason = ""
    else:
        final_index = assistant_indexes[-1]
        final_message = events[final_index].get("message") or {}
        stop_reason = str(final_message.get("stop_reason") or "")
        if stop_reason != "end_turn":
            errors.append(f"最终 assistant stop_reason 必须为 end_turn，当前为 {stop_reason or '空'}")
        if any(event.get("type") in {"assistant", "user"} for event in events[final_index + 1 :]):
            errors.append("最终 end_turn 之后仍有 assistant/user 事件")

    provider_texts: list[str] = []
    permission_hit = False
    max_token_hit = False
    for event in events:
        message = event.get("message") or {}
        if event.get("type") == "assistant":
            text, blocks = _content_text(message.get("content"))
            provider_texts.append(text)
            if any(isinstance(block, dict) and block.get("name") == "AskUserQuestion" for block in blocks):
                permission_hit = True
            if PERMISSION.search(text):
                permission_hit = True
            if str(message.get("stop_reason") or "") == "max_tokens":
                max_token_hit = True
        elif event.get("type") == "system":
            provider_texts.append(json.dumps(message, ensure_ascii=False))
    api_hits = API_ERROR.findall("\n".join(provider_texts))
    if api_hits:
        errors.append(f"轨迹包含 API/网络错误: {api_hits[:5]}")
    if permission_hit:
        errors.append("轨迹包含 AskUserQuestion 或权限询问")
    if max_token_hit:
        errors.append("轨迹出现 max_tokens 截断")

    return {
        "ok": not errors,
        "tracePath": str(trace_path.resolve()),
        "traceSha256": sha256_file(trace_path),
        "sessionId": session_id,
        "eventCount": len(events),
        "userPromptCount": len(prompts),
        "promptEventIndex": prompts[0]["eventIndex"] if len(prompts) == 1 else None,
        "promptEventUuid": prompts[0]["uuid"] if len(prompts) == 1 else "",
        "finalAssistantIndex": final_index,
        "finalStopReason": stop_reason,
        "errors": errors,
    }


def trace_event_index(trace_path: Path, *, event_index: int | None = None, event_uuid: str = "") -> dict[str, Any]:
    events = load_trace(trace_path)
    if event_index is not None:
        if event_index < 0 or event_index >= len(events):
            raise SologsbError(f"轨迹事件索引越界: {event_index}")
        event = events[event_index]
    elif event_uuid:
        matches = [event for event in events if event.get("uuid") == event_uuid]
        if len(matches) != 1:
            raise SologsbError(f"轨迹 UUID 未唯一命中: {event_uuid}")
        event = matches[0]
    else:
        raise SologsbError("必须提供 event_index 或 event_uuid")
    message = event.get("message") or {}
    text, blocks = _content_text(message.get("content"))
    tools = [str(block.get("name") or "") for block in blocks if block.get("type") == "tool_use"]
    return {
        "line": event.get("_line"),
        "uuid": event.get("uuid", ""),
        "type": event.get("type", ""),
        "text": text,
        "toolNames": [name for name in tools if name],
    }
