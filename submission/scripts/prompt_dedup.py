#!/usr/bin/env python3
"""Fetch historical GSB prompts and optionally deduplicate a candidate prompt."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# --- 设备配置注入：必须在下列模块读取环境变量之前执行 ---
for _parent in Path(__file__).resolve().parents:
    if (_parent / "scripts" / "device_config.py").is_file():
        sys.path.insert(0, str(_parent / "scripts"))
        break
import device_config as _device_config  # noqa: E402

_device_config.load_and_apply()

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import (  # noqa: E402
    assess_gsb_reason_dedup,
    assess_prompt_dedup,
    fetch_gsb_prompt_history,
    gsb_history_cache_path,
    load_json,
    load_manual_history_items,
    unresolved_history_ids,
    write_manual_history_items,
)


def task_context(task_root: Path | None) -> tuple[set[str], set[int], Path | None]:
    if task_root is None:
        return set(), set(), None
    task_root = task_root.expanduser().resolve()
    state = load_json(task_root / "monitor" / "state.json")
    sessions = {
        str(((state.get("sides") or {}).get(side) or {}).get("sessionId") or "")
        for side in ("A", "B")
    } - {""}
    result = load_json(task_root / "workspace" / "评审文件" / "pre-submit" / "submission-result.json")
    exclude_ids = {int(result["submissionNo"])} if str(result.get("submissionNo") or "").isdigit() else set()
    return sessions, exclude_ids, task_root



def standalone_output_dir() -> Path:
    """没有 --task-root 时的输出目录。

    绝不能落到当前工作目录：技能目录是多设备共享的只读包，
    在这里留下文件会污染其他设备拿到的副本。
    """
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    return (codex_home / "cache" / "sologsb-0917" / "prompt-dedup").resolve()

def main() -> int:
    parser = argparse.ArgumentParser(description="历史 GSB 提示词/理由抽取与去重")
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--text")
    parser.add_argument("--reason")
    parser.add_argument("--reason-file", type=Path)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--import-history", type=Path, help="导入平台反馈或无权读取的历史 GSB JSON")
    parser.add_argument("--history-out", type=Path)
    parser.add_argument("--review-out", type=Path)
    parser.add_argument("--reason-review-out", type=Path)
    parser.add_argument("--summary-out", type=Path)
    args = parser.parse_args()

    task_root = args.task_root.expanduser().resolve() if args.task_root else None
    sessions, exclude_ids, task_root = task_context(task_root)
    imported_count = 0
    if args.import_history:
        imported_raw = json.loads(args.import_history.expanduser().resolve().read_text(encoding="utf-8"))
        if isinstance(imported_raw, dict):
            imported_values = imported_raw.get("items") if "items" in imported_raw else [imported_raw]
        else:
            imported_values = imported_raw
        if isinstance(imported_values, dict):
            imported_values = [imported_values]
        if not isinstance(imported_values, list):
            raise ValueError("--import-history 必须是对象、对象数组或含 items 的对象")
        existing = load_manual_history_items()
        write_manual_history_items([*existing, *imported_values])
        imported_count = len(imported_values)
    history = fetch_gsb_prompt_history(
        sessions,
        exclude_ids,
        force_refresh=args.refresh_cache,
    )
    history_path = (args.history_out or (task_root / "monitor" / "gsb-prompt-history.json" if task_root else standalone_output_dir() / "gsb-prompt-history.json")).expanduser().resolve()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    candidate = ""
    if args.candidate:
        candidate = args.candidate.expanduser().resolve().read_text(encoding="utf-8")
    elif args.text is not None:
        candidate = args.text
    reason = ""
    if args.reason_file:
        reason_path = args.reason_file.expanduser().resolve()
        reason_raw = reason_path.read_text(encoding="utf-8")
        try:
            reason_value = json.loads(reason_raw)
        except json.JSONDecodeError:
            reason = reason_raw
        else:
            if isinstance(reason_value, dict):
                reason = str(reason_value.get("reason") or reason_value.get("gsb_reason") or "")
            else:
                reason = str(reason_value or "")
    elif args.reason is not None:
        reason = args.reason

    if not candidate.strip() and not reason.strip():
        print(json.dumps({
            "status": "history_ready",
            "historyPath": str(history_path),
            "cachePath": str(history.get("cachePath") or gsb_history_cache_path()),
            "cacheHit": history.get("cacheHit"),
            "cacheFresh": history.get("cacheFresh"),
            "historyCount": history.get("total", 0),
            "serverTotal": history.get("serverTotal", 0),
            "skippedCurrentSubmissions": history.get("skippedCurrentSubmissions", []),
            "manualImported": imported_count,
            "unresolvedHistoryIds": unresolved_history_ids(history),
            "message": "历史 GSB 提示词和理由已缓存，可继续设计候选内容。",
        }, ensure_ascii=False, indent=2))
        return 0

    result = {
        "status": "unique",
        "historyPath": str(history_path),
        "cachePath": str(history.get("cachePath") or gsb_history_cache_path()),
        "manualImported": imported_count,
        "unresolvedHistoryIds": unresolved_history_ids(history),
    }
    exit_code = 0
    if candidate.strip():
        review = assess_prompt_dedup(candidate, history)
        review_path = (args.review_out or (task_root / "monitor" / "prompt-dedup-review.json" if task_root else standalone_output_dir() / "prompt-dedup-review.json")).expanduser().resolve()
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["promptReview"] = {"decision": review.get("decision"), "path": str(review_path), "matches": review.get("matches", [])}
        if review.get("decision") != "UNIQUE":
            result["status"] = "blocked"
            exit_code = 2
    if reason.strip():
        review = assess_gsb_reason_dedup(reason, history)
        review_path = (args.reason_review_out or (task_root / "monitor" / "gsb-reason-dedup-review.json" if task_root else standalone_output_dir() / "gsb-reason-dedup-review.json")).expanduser().resolve()
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_path.write_text(json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["reasonReview"] = {
            "decision": review.get("decision"),
            "path": str(review_path),
            "matches": review.get("matches", []),
            "rewriteInstruction": review.get("rewriteInstruction", ""),
        }
        if review.get("decision") != "UNIQUE":
            result["status"] = "blocked"
            exit_code = 2

    summary_path = (args.summary_out or (task_root / "workspace" / "评审文件" / "GSB文案查重.md" if task_root else standalone_output_dir() / "GSB文案查重.md")).expanduser().resolve()
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# GSB 文案查重",
        "",
        f"- 历史来源：{history.get('source')}",
        f"- 本地缓存：`{history.get('cachePath') or gsb_history_cache_path()}`",
        f"- 缓存命中：{history.get('cacheHit')} / fresh={history.get('cacheFresh')}",
        f"- 历史记录数：{history.get('total', 0)}",
    ]
    unresolved_ids = unresolved_history_ids(history)
    if unresolved_ids:
        lines.append(f"- 未解决历史 ID：{unresolved_ids}（缺少理由文本，提交预检会阻断）")
    lines.append("")
    if candidate.strip():
        prompt_review = result.get("promptReview") or {}
        lines += [f"## 提示词：`{prompt_review.get('decision')}`", ""]
        lines.extend([f"- `#{item.get('id')}` 相似度 {item.get('similarity')}，最长片段 {item.get('longestCommonSubstringLength')} 字" for item in prompt_review.get("matches", [])] or ["- 未发现重复。"])
    if reason.strip():
        reason_review = result.get("reasonReview") or {}
        lines += ["", f"## GSB 理由：`{reason_review.get('decision')}`", ""]
        lines.extend([
            f"- `#{item.get('id')}` 相似度 {item.get('similarityPercent')}%，最长片段 {item.get('longestCommonSubstringLength')} 字，规则 `{item.get('rule')}`"
            for item in reason_review.get("matches", [])
        ] or ["- 未发现 B-5 公共长片段、模板化片段或高相似度理由。"])
        if reason_review.get("rewriteInstruction"):
            lines.append(f"- 重写要求：{reason_review.get('rewriteInstruction')}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result["summaryPath"] = str(summary_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
