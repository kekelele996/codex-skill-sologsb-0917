#!/usr/bin/env python3
"""Create the human approval file for a change-volume-line-gate exception.

Normal submissions are approved automatically as user liudong after a complete
audit passes. This command is only for the single allowed exception: at least
one side has fewer than 10 non-test business code lines and that is the only
blocking condition. Approval is bound to the payload, delivery sheet, and
change-volume review hashes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCOPE = "change-volume-line-gate"
APPROVER = "liudong"
PHRASE = "批准改动量例外"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"JSON 必须是对象: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="审核并批准 change-volume-line-gate 例外")
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scope", choices=[SCOPE], default=SCOPE)
    parser.add_argument("--approved-by", default=APPROVER)
    args = parser.parse_args()

    if str(args.approved_by).strip().casefold() != APPROVER:
        raise SystemExit(f"拒绝执行：改动量例外只能由 {APPROVER} 批准")
    if not sys.stdin.isatty():
        raise SystemExit("拒绝执行：人工审批必须在真实 TTY 中完成，禁止管道或 --yes 自动确认")

    task_root = args.task_root.expanduser().resolve()
    payload_path = (
        args.payload
        or task_root / "workspace" / "评审文件" / "pre-submit" / "submission-payload.json"
    ).expanduser().resolve()
    output = (
        args.output
        or task_root / "workspace" / "评审文件" / "pre-submit" / "change-volume-line-gate-approval.json"
    ).expanduser().resolve()
    payload = load_json(payload_path)
    if str(payload.get("approvalPolicy") or "") != SCOPE:
        raise SystemExit("当前 payload 不是 change-volume-line-gate 例外，不允许生成例外审批")
    line_gate = payload.get("changeVolumeLineGate") or {}
    if line_gate.get("required") is not True:
        raise SystemExit("payload 未声明改动量门禁需要例外审批")
    if not line_gate.get("failedSides"):
        raise SystemExit("payload 没有列出低于 10 行的失败侧")
    review_path = Path(str(line_gate.get("reviewPath") or "")).expanduser().resolve()
    if not review_path.is_file():
        raise SystemExit(f"改动量复核文件不存在: {review_path}")
    review = load_json(review_path)
    if review.get("onlyBlocker") is not True:
        raise SystemExit("改动量门禁不是当前唯一阻断项，禁止生成例外审批")
    review_sha = str(line_gate.get("reviewSha256") or "")
    if not review_sha or str(review.get("reviewSha256") or "") != review_sha:
        raise SystemExit("改动量复核哈希与 payload 不一致")
    review_payload = {
        key: review.get(key)
        for key in (
            "schemaVersion", "id", "initialSnapshot", "hardMinimumLines", "targetLines",
            "failedSides", "onlyBlocker", "codeChange",
        )
    }
    if sha256_json(review_payload) != review_sha:
        raise SystemExit("改动量复核内容与 reviewSha256 不一致")

    payload_sha = sha256_file(payload_path)
    delivery = payload.get("deliverySheet") or {}
    delivery_path = Path(str(delivery.get("path") or "")).expanduser().resolve()
    if not delivery_path.is_file():
        raise SystemExit(f"交付表不存在: {delivery_path}")
    delivery_sha = sha256_file(delivery_path)
    if delivery_sha != str(delivery.get("sha256") or ""):
        raise SystemExit("交付表已变化，请重新运行 preflight")

    print("即将批准 change-volume-line-gate 例外：")
    print(f"  任务目录: {task_root}")
    print(f"  payload: {payload_path}")
    print(f"  payload SHA-256: {payload_sha}")
    print(f"  交付表: {delivery_path}")
    print(f"  交付表 SHA-256: {delivery_sha}")
    print(f"  改动量复核: {review_path}")
    print(f"  改动量复核 SHA-256: {review_sha}")
    print(f"  低于 10 行的侧: {', '.join(str(item) for item in line_gate.get('failedSides') or [])}")
    print("该审批只允许改动量单项失败；其他任何阻断项都不能使用。")
    answer = input(f"请输入「{PHRASE}」并回车：").strip()
    if answer != PHRASE:
        raise SystemExit("人工确认文本不匹配，未生成审批文件")

    approval = {
        "schemaVersion": 2,
        "manualConfirmed": True,
        "approvalKind": "manual",
        "scope": SCOPE,
        "approvedBy": APPROVER,
        "approvedAt": utc_now(),
        "phrase": PHRASE,
        "taskRoot": str(task_root),
        "payloadPath": str(payload_path),
        "payloadSha256": payload_sha,
        "deliverySheetPath": str(delivery_path),
        "deliverySheetSha256": delivery_sha,
        "changeVolumeReviewPath": str(review_path),
        "changeVolumeReviewSha256": review_sha,
        "failedSides": list(line_gate.get("failedSides") or []),
        "harnessVersion": str(payload.get("harnessVersion") or ""),
        "uploads": {
            label: {"path": str(info.get("path") or ""), "sha256": str(info.get("sha256") or "")}
            for label, info in (payload.get("uploads") or {}).items()
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"改动量例外审批已写入: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
