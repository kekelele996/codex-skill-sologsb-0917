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


def validate_candidate(
    candidate_path: Path,
    review_path: Path,
    *,
    task_type: str,
    difficulty: str,
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
    errors: list[str] = []
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
    }


def install_prompt(
    task_root: Path,
    *,
    candidate_path: Path,
    review_path: Path,
    task_type: str,
    difficulty: str,
    history_path: Path | None = None,
    allow_over_170: bool = False,
) -> dict[str, Any]:
    result = validate_candidate(
        candidate_path,
        review_path,
        task_type=task_type,
        difficulty=difficulty,
        history_path=history_path,
        allow_over_170=allow_over_170,
    )
    review_dir = task_root / "monitor" / "prompt"
    review_dir.mkdir(parents=True, exist_ok=True)
    write_json(review_dir / "validation.json", result)
    atomic_copy(review_path, review_dir / "ra-renhua-review.json")
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
        }
    )
    save_state(task_root, state)
    return {**result, "promptPath": str(destination)}
