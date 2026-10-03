#!/usr/bin/env python3
"""恢复被监控台关闭或列入停止名单、但 A/B 候选其实已经完成的 0917 任务。

背景（2026-10-02 实测）：`run` 的进程如果不是常驻会话，会话结束时会被回收；
监控台的“长时间无进展”守卫会据此把任务标成 failed，并在启动新任务前关闭旧任务、
把旧任务写入停止名单。此时 A/B 候选往往已经 staged，把状态改回
`semantic_review_required` 并解除停止名单就能继续发布，不必重跑候选。
本模块把这段恢复动作收敛成一条命令，并留下审计记录。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from common import SologsbError, read_json, save_state, utc_now, write_json
from project_claims import submission_record

# 这些状态本身就允许继续发布，不需要纠正
RESUMABLE_STATUSES = {
    "semantic_review_required",
    "repo_ready",
    "a_clean",
    "b_clean",
    "verified",
    "gsb_ready",
    "recorded",
}

SIDE_DONE_STATUSES = {"staged", "clean"}


def default_queue_path() -> Path:
    """监控台队列文件；找不到就跳过提示，不影响恢复动作。"""
    override = str(os.environ.get("SOLOSB_MONITOR_QUEUE_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return (
        Path.home()
        / "repositories"
        / "gitlab"
        / "评审项目"
        / "project"
        / "sg-auto"
        / ".state"
        / "queue.json"
    )


def remove_stop_marker(stop_path: Path, task_name: str) -> dict[str, Any]:
    """解除本任务的停止名单；其它任务的条目原样保留。"""
    task_name = str(task_name or "").strip()
    result: dict[str, Any] = {"path": str(stop_path), "removed": False, "backup": ""}
    if not task_name or not stop_path.is_file():
        return result
    try:
        data = json.loads(stop_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SologsbError(f"停止名单不可解析: {stop_path}: {exc}") from exc
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list) or task_name not in [str(item) for item in tasks]:
        return result
    backup = stop_path.with_suffix(stop_path.suffix + f".bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(stop_path, backup)
    result["backup"] = str(backup)
    if isinstance(data, dict):
        data["tasks"] = [item for item in tasks if str(item) != task_name]
        guard = data.get("guard")
        if isinstance(guard, dict):
            guard.pop(task_name, None)
        payload: Any = data
    else:
        payload = [item for item in tasks if str(item) != task_name]
    temp = stop_path.with_suffix(stop_path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, stop_path)
    result["removed"] = True
    return result


def monitor_queue_notice(task_root: Path, project_code: str, queue_path: Path | None = None) -> dict[str, Any]:
    """记录监控台里同项目仍待办的条目。

    监控台默认允许同项目重跑：任务被关闭后它会把项目重新排队。技能不去改
    别的应用的队列文件，只把现状写进任务目录，供操作员在监控台页面删除。
    """
    target = queue_path or default_queue_path()
    notice: dict[str, Any] = {
        "checkedAt": utc_now(),
        "queuePath": str(target),
        "projectCode": project_code,
        "pendingItems": [],
        "found": False,
    }
    if project_code and target.is_file():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for item in (data.get("items") if isinstance(data, dict) else []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("projectCode") or "").casefold() != str(project_code).casefold():
                continue
            if str(item.get("status") or "") != "pending":
                continue
            notice["pendingItems"].append(
                {
                    "id": str(item.get("id") or ""),
                    "taskName": str(item.get("taskName") or ""),
                    "addedAt": str(item.get("addedAt") or ""),
                }
            )
    notice["found"] = bool(notice["pendingItems"])
    write_json(task_root / "monitor" / "monitor-queue-notice.json", notice)
    return notice


def resume_task(task_root: Path, *, stop_path: Path, reason: str = "") -> dict[str, Any]:
    """解除停止名单并把被监控台关闭的任务恢复到可继续发布的状态。"""
    task_root = task_root.expanduser().resolve()
    state_path = task_root / "monitor" / "state.json"
    if not state_path.is_file():
        raise SologsbError(f"任务状态不存在: {state_path}")
    state = read_json(state_path, {})
    if not isinstance(state, dict) or not state:
        raise SologsbError(f"任务状态不可解析: {state_path}")
    task_name = str(state.get("taskName") or task_root.name)

    record = submission_record(task_root)
    if record:
        raise SologsbError(
            f"任务已有提交记录（{record.get('submissionId')} / {record.get('status')}），"
            "不需要恢复；返修请按平台流程处理"
        )

    sides = state.get("sides") if isinstance(state.get("sides"), dict) else {}
    mapping = state.get("candidateMapping") if isinstance(state.get("candidateMapping"), dict) else {}
    staged = [side for side in ("A", "B") if str((sides.get(side) or {}).get("status") or "") in SIDE_DONE_STATUSES]
    missing = [side for side in ("A", "B") if not (mapping.get(side) or {}).get("candidateId")]
    if len(staged) != 2 or missing:
        raise SologsbError(
            "A/B 尚未都完成结构校验，不能恢复发布："
            f"已完成 {staged or '无'}，缺少映射 {missing or '无'}；"
            "候选缺失时请按工作流重新 run 对应一侧"
        )

    previous_status = str(state.get("status") or "")
    stop_result = remove_stop_marker(stop_path, task_name)
    reopened: dict[str, Any] = {}
    if previous_status not in RESUMABLE_STATUSES:
        reopened = {
            "status": previous_status,
            "closedBy": str(state.get("closedBy") or ""),
            "closedReason": str(state.get("closedReason") or ""),
            "closedAt": str(state.get("closedAt") or ""),
            "reopenedAt": utc_now(),
            "reopenedReason": reason or "监控台关闭或停止名单阻断后恢复，候选均已 staged",
        }
        state["reopenedFrom"] = reopened
        for key in ("closedBy", "closedReason", "closedAt"):
            state.pop(key, None)
        state["status"] = "semantic_review_required"
        state["updatedAt"] = utc_now()
        save_state(task_root, state)

    project_code = str((state.get("source") or {}).get("projectCode") or "").strip()
    notice = monitor_queue_notice(task_root, project_code)
    result = {
        "ok": True,
        "taskRoot": str(task_root),
        "taskName": task_name,
        "previousStatus": previous_status,
        "status": str(state.get("status") or ""),
        "reopened": reopened,
        "stopList": stop_result,
        "monitorQueueNotice": notice,
        "nextSteps": [
            f"github-init --task-root {task_root}（仓库未创建时）",
            f"semantic --task-root {task_root} --side both",
            f"publish --task-root {task_root} --semantic-a .../a.review.json --semantic-b .../b.review.json",
        ],
    }
    if notice.get("found"):
        result["warning"] = (
            "监控台队列里同项目还有待办，提交成功后请在监控台页面删除，"
            "否则同一项目会被再跑一遍；明细见 monitor/monitor-queue-notice.json"
        )
    write_json(task_root / "monitor" / "resume.json", result)
    return result
