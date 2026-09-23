#!/usr/bin/env python3
"""Build evidence indexes and side audit reports from traces and real checks."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from common import SologsbError, read_json, save_state, sha256_file, utc_now, write_json
from trace_validator import load_trace, validate_single_round


EVALUATION_EXCLUDED_NOISE_RE = re.compile(
    r"(?:String to replace not found|文本替换失败|替换文本未找到|编辑未匹配|"
    r"No module named|ModuleNotFoundError|command not found|命令未找到|"
    r"(?:python|python3|node|npm|pnpm|git|bash)\s*(?:命令)?\s*(?:未找到|缺失|返回\s*127)|"
    r"退出码\s*127|临时工作目录|工作目录不存在|测试\s*PYTHONPATH\s*缺失)",
    re.I,
)


def _evaluation_excluded_noise(value: Any) -> bool:
    return bool(EVALUATION_EXCLUDED_NOISE_RE.search(str(value or "")))


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _event_tools(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict) and item.get("type") == "tool_use"]


def _tool_result_errors(event: dict[str, Any]) -> list[str]:
    if event.get("type") != "user":
        return []
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    errors: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        if block.get("is_error"):
            raw = block.get("content")
            errors.append(str(raw)[:800])
    return errors


def process_evidence(side: str, trace_path: Path, expected_prompt: str, session_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validation = validate_single_round(trace_path, expected_prompt=expected_prompt, expected_session_id=session_id)
    if not validation["ok"]:
        raise SologsbError(f"{side} 轨迹不干净: {'; '.join(validation['errors'])}")
    events = load_trace(trace_path)
    evidence: list[dict[str, Any]] = []
    read_counts: Counter[str] = Counter()
    bash_commands: list[str] = []
    tool_names: Counter[str] = Counter()
    tool_errors: list[dict[str, Any]] = []
    final_text = ""
    for index, event in enumerate(events):
        for tool in _event_tools(event):
            name = str(tool.get("name") or "")
            tool_names[name] += 1
            tool_input = tool.get("input") or {}
            if name == "Read":
                path = str(tool_input.get("file_path") or tool_input.get("path") or "")
                if path:
                    read_counts[path] += 1
            if name == "Bash":
                command = str(tool_input.get("command") or "")
                if command:
                    bash_commands.append(command)
            evidence.append(
                {
                    "id": f"{side}-process-{index + 1:04d}",
                    "side": side,
                    "type": "process",
                    "polarity": "neutral",
                    "text": f"第 {index + 1} 个事件调用 {name or '未知工具'}",
                    "trace": {
                        "sessionId": session_id,
                        "eventIndex": index,
                        "eventUuid": str(event.get("uuid") or ""),
                        "tracePath": str(trace_path.resolve()),
                        "quote": json.dumps(tool, ensure_ascii=False)[:1200],
                        "toolName": name,
                    },
                    "artifact": None,
                }
            )
        for error in _tool_result_errors(event):
            tool_errors.append({"eventIndex": index, "error": error})
            evidence.append(
                {
                    "id": f"{side}-process-error-{index + 1:04d}",
                    "side": side,
                    "type": "process",
                    "polarity": "negative",
                    "text": "工具调用返回错误",
                    "evaluationExcluded": _evaluation_excluded_noise(error),
                    "trace": {
                        "sessionId": session_id,
                        "eventIndex": index,
                        "eventUuid": str(event.get("uuid") or ""),
                        "tracePath": str(trace_path.resolve()),
                        "quote": error,
                        "toolName": "",
                    },
                    "artifact": None,
                }
            )
        if event.get("type") == "assistant" and _message_text(event.get("message") or {}):
            final_text = _message_text(event.get("message") or {})
    if final_text:
        final_index = len(events) - 1
        evidence.append(
            {
                "id": f"{side}-process-final",
                "side": side,
                "type": "process",
                "polarity": "neutral",
                "text": "模型最终回复",
                "trace": {
                    "sessionId": session_id,
                    "eventIndex": final_index,
                    "eventUuid": str(events[-1].get("uuid") or ""),
                    "tracePath": str(trace_path.resolve()),
                    "quote": final_text[:4000],
                    "toolName": "",
                },
                "artifact": None,
            }
        )
    summary = {
        "tracePath": str(trace_path.resolve()),
        "traceSha256": sha256_file(trace_path),
        "sessionId": session_id,
        "validation": validation,
        "toolCallCount": sum(tool_names.values()),
        "toolNames": dict(tool_names),
        "readCounts": dict(read_counts),
        "repeatedReads": {key: value for key, value in read_counts.items() if value > 1},
        "bashCallCount": len(bash_commands),
        "bashCommands": bash_commands,
        "toolErrors": tool_errors,
        "finalText": final_text,
    }
    return evidence, summary


def artifact_evidence(side: str, side_state: dict[str, Any], verification: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    evidence = [
        {
            "id": f"{side}-artifact-snapshot",
            "side": side,
            "type": "artifact",
            "polarity": "neutral",
            "text": f"{side} 产物快照 {side_state.get('artifactSnapshot')}",
            "trace": None,
            "artifact": {
                "commit": side_state.get("artifactSnapshot", ""),
                "commitUrl": side_state.get("artifactSnapshotUrl", ""),
                "parent": side_state.get("parentSnapshot", ""),
                "changedFiles": side_state.get("changedFiles") or [],
                "diffStat": side_state.get("diffStat", ""),
                "command": "git diff --stat <initial> <artifact>",
                "exitCode": 0,
                "output": side_state.get("diffStat", ""),
            },
        }
    ]
    for index, check in enumerate(verification.get("checks") or [], 1):
        evidence.append(
            {
                "id": f"{side}-artifact-check-{index:02d}",
                "side": side,
                "type": "artifact",
                "polarity": "negative"
                if check.get("observedFailure") or not check.get("ok")
                else "positive",
                "text": f"真实复核 {check.get('name')}",
                "trace": None,
                "artifact": {
                    "commit": verification.get("artifactSnapshot", ""),
                    "path": "",
                    "line": None,
                    "command": check.get("command", ""),
                    "exitCode": check.get("exitCode"),
                    "output": check.get("outputPreview", ""),
                    "logPath": check.get("logPath", ""),
                    "ok": bool(check.get("ok")),
                    "observedFailure": bool(check.get("observedFailure")),
                    "localScript": bool(check.get("localScript")),
                    "probe": check.get("probe"),
                    "error": check.get("error", ""),
                },
            }
        )
    summary = {
        "artifactSnapshot": side_state.get("artifactSnapshot", ""),
        "artifactSnapshotUrl": side_state.get("artifactSnapshotUrl", ""),
        "parentSnapshot": side_state.get("parentSnapshot", ""),
        "changedFiles": side_state.get("changedFiles") or [],
        "diffStat": side_state.get("diffStat", ""),
        "verification": verification,
    }
    return evidence, summary


def recording_evidence(side: str, side_state: dict[str, Any]) -> dict[str, Any] | None:
    recording = side_state.get("recording") or {}
    if not recording:
        return None
    app_outcome = str(recording.get("appOutcome") or ("failure" if recording.get("observedAppFailure") else "success"))
    browser_result_path = str(recording.get("browserResultPath") or "")
    browser_result: dict[str, Any] = {}
    if browser_result_path:
        try:
            browser_result = read_json(Path(browser_result_path), {}) or {}
        except Exception:
            browser_result = {}
    steps = browser_result.get("steps") or []
    failed_steps = [
        item for item in steps
        if isinstance(item, dict) and str(item.get("status") or "") == "failed"
    ]
    failed_text = "；".join(str(item.get("error") or item.get("name") or "") for item in failed_steps)
    return {
        "id": f"{side}-recording",
        "side": side,
        "type": "artifact",
        "polarity": "negative" if app_outcome == "failure" else "positive",
        "text": (
            f"真实录屏结果：{app_outcome}；命令退出码 {recording.get('commandExitCode')}"
            + (f"；失败步骤：{failed_text}" if failed_text else "")
        ),
        "trace": None,
        "artifact": {
            "commit": str(side_state.get("artifactSnapshot") or ""),
            "path": str(recording.get("videoPath") or ""),
            "line": None,
            "command": "sologsb.py record",
            "exitCode": recording.get("commandExitCode"),
            "output": json.dumps(
                {
                    "appOutcome": app_outcome,
                    "expectedAppFailure": recording.get("expectedAppFailure"),
                    "observedAppFailure": recording.get("observedAppFailure"),
                    "browserResult": browser_result,
                    "terminalLogPath": recording.get("terminalLogPath"),
                    "captureMethod": recording.get("captureMethod"),
                    "windowCaptures": recording.get("windowCaptures"),
                },
                ensure_ascii=False,
            )[:12000],
            "logPath": browser_result_path,
            "videoPath": str(recording.get("videoPath") or ""),
            "captureMethod": recording.get("captureMethod"),
            "windowCaptures": recording.get("windowCaptures") or [],
            "durationSeconds": recording.get("durationSeconds"),
            "recordingMode": str(recording.get("mode") or ""),
            "width": recording.get("width"),
            "height": recording.get("height"),
            "ok": bool(recording.get("ok")),
            "observedFailure": bool(recording.get("observedAppFailure")),
            "error": "",
        },
    }


def _write_side_report(path: Path, side: str, process: dict[str, Any], artifact: dict[str, Any]) -> None:
    files = artifact.get("changedFiles") or []
    checks = artifact.get("verification", {}).get("checks") or []
    lines = [
        f"# {side} 审核结论",
        "",
        f"- SessionID：{process.get('sessionId')}",
        f"- 轨迹：{process.get('tracePath')}",
        f"- 产物快照：{artifact.get('artifactSnapshot')}",
        f"- 父提交：{artifact.get('parentSnapshot')}",
        f"- 工具调用：{process.get('toolCallCount')} 次",
        f"- 重复读取：{json.dumps(process.get('repeatedReads') or {}, ensure_ascii=False)}",
        f"- 工具错误：{len(process.get('toolErrors') or [])} 次",
        f"- 变更文件：{len(files)} 个",
        "",
        "## 真实复核",
        "",
    ]
    for check in checks:
        lines.append(f"- {'通过' if check.get('ok') else '失败'}：{check.get('name')}，命令 `{check.get('command')}`，退出码 {check.get('exitCode')}")
    lines.extend(["", "## Diff", "", "```", str(artifact.get("diffStat") or "无变更"), "```", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _append_reviewed_custom_evidence(task_root: Path, evidence: list[dict[str, Any]]) -> None:
    """Merge reviewed product-level evidence that automated checks cannot express.

    Custom evidence is optional and intentionally separate from generated evidence so
    every audit refresh preserves it. Each item still has to identify a side, layer,
    polarity, readable fact, and a trace or artifact anchor.
    """
    path = task_root / "monitor" / "custom-evidence.json"
    if not path.is_file():
        return
    document = read_json(path, {})
    items = document.get("evidence") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise SologsbError("custom-evidence.json 必须是数组或包含 evidence 数组的对象")
    existing = {str(item.get("id") or "") for item in evidence if isinstance(item, dict)}
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise SologsbError(f"custom evidence {index} 不是对象")
        evidence_id = str(item.get("id") or "").strip()
        side = str(item.get("side") or "").strip()
        layer = str(item.get("type") or "").strip()
        polarity = str(item.get("polarity") or "").strip()
        text = str(item.get("text") or "").strip()
        if not evidence_id or evidence_id in existing:
            raise SologsbError(f"custom evidence {index} 缺少唯一 ID")
        if side not in {"A", "B"} or layer not in {"process", "artifact"}:
            raise SologsbError(f"custom evidence {evidence_id} side/type 无效")
        if polarity not in {"positive", "negative", "neutral"}:
            raise SologsbError(f"custom evidence {evidence_id} polarity 无效")
        if not text:
            raise SologsbError(f"custom evidence {evidence_id} 缺少可读事实")
        if layer == "process" and not isinstance(item.get("trace"), dict):
            raise SologsbError(f"custom process evidence {evidence_id} 缺少 trace 锚点")
        if layer == "artifact" and not isinstance(item.get("artifact"), dict):
            raise SologsbError(f"custom artifact evidence {evidence_id} 缺少 artifact 锚点")
        evidence.append(item)
        existing.add(evidence_id)


def run_audit(task_root: Path) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    if state.get("status") not in {"verified", "gsb_ready", "recorded", "complete"}:
        raise SologsbError("必须完成两侧真实验证才能生成证据索引")
    verification = read_json(task_root / "monitor" / "verification.json", {})
    if not verification.get("ok"):
        raise SologsbError("真实验证未全部通过")
    prompt = Path(str(state["promptPath"])).read_text(encoding="utf-8")
    all_evidence: list[dict[str, Any]] = []
    process_summary: dict[str, Any] = {}
    artifact_summary: dict[str, Any] = {}
    for side in ("A", "B"):
        side_state = dict((state.get("sides") or {}).get(side) or {})
        side_state["recording"] = ((state.get("recordings") or {}).get(side) or {})
        trace_path = Path(str(side_state.get("tracePath") or ""))
        process, process_info = process_evidence(
            side,
            trace_path,
            prompt,
            str(side_state.get("sessionId") or ""),
        )
        artifact, artifact_info = artifact_evidence(side, side_state, verification.get(side.lower()) or {})
        recording = recording_evidence(side, side_state)
        all_evidence.extend(process + artifact + ([recording] if recording else []))
        artifact_info["recording"] = recording
        process_summary[side] = process_info
        artifact_summary[side] = artifact_info
        _write_side_report(
            task_root / "workspace" / "评审文件" / f"审核结论-{side.lower()}.md",
            side,
            process_info,
            artifact_info,
        )
    side_a = ((state.get("sides") or {}).get("A") or {})
    side_b = ((state.get("sides") or {}).get("B") or {})
    version_a = side_a.get("harnessVersion")
    version_b = side_b.get("harnessVersion")
    if version_a != version_b:
        raise SologsbError(f"A/B Harness 版本不一致: {version_a} != {version_b}")
    if side_a.get("imageDigest") != side_b.get("imageDigest"):
        raise SologsbError(
            f"A/B Docker 镜像 digest 不一致: {side_a.get('imageDigest')} != {side_b.get('imageDigest')}"
        )
    if side_a.get("declaredContextWindow") != side_b.get("declaredContextWindow"):
        raise SologsbError("A/B 上下文窗口配置不一致")
    _append_reviewed_custom_evidence(task_root, all_evidence)
    evidence_doc = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "promptSha256": state.get("promptSha256"),
        "evidence": all_evidence,
        "process": process_summary,
        "artifact": artifact_summary,
    }
    write_json(task_root / "monitor" / "evidence.json", evidence_doc)
    audit = {
        "schemaVersion": 1,
        "generatedAt": utc_now(),
        "promptPath": state.get("promptPath"),
        "promptSha256": state.get("promptSha256"),
        "harness": "Claude Code",
        "harnessVersion": version_a,
        "process": process_summary,
        "artifact": artifact_summary,
        "evidenceCount": len(all_evidence),
    }
    write_json(task_root / "monitor" / "audit.json", audit)
    state["status"] = "verified"
    state["evidencePath"] = str((task_root / "monitor" / "evidence.json").resolve())
    save_state(task_root, state)
    return audit
