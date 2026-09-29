#!/usr/bin/env python3
"""Device-wide daily SOLO2 submission quota.

The flag file is shared by every task on this device. Once the number of new
submissions for the local day reaches the limit, ``limitReached`` turns true
and every later submission is parked in the ``待提交`` folder instead of being
POSTed, so it can be submitted after local midnight when the counter resets.
"""
from __future__ import annotations

import fcntl
import json
import os
import shlex
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

DAILY_LIMIT_ENV = "SOLOGSB_DAILY_SUBMIT_LIMIT"
FLAG_PATH_ENV = "SOLOGSB_DAILY_QUOTA_PATH"
DEFERRED_DIR_ENV = "SOLOGSB_DEFERRED_DIR"
DEFAULT_DAILY_LIMIT = 100
DEFERRED_DIR_SUFFIX = "待提交"
DEFERRED_INDEX_NAME = "清单.json"


def daily_limit() -> int:
    raw = os.environ.get(DAILY_LIMIT_ENV, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_DAILY_LIMIT


def flag_path() -> Path:
    configured = os.environ.get(FLAG_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    return (codex_home / "cache" / "sologsb-0917" / "daily-submit-quota.json").resolve()


def local_today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def local_date(value: str) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d")


def next_local_midnight() -> str:
    now = datetime.now().astimezone()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fresh_state(today: str, limit: int) -> dict[str, Any]:
    return {"schemaVersion": 1, "date": today, "count": 0, "limit": limit,
            "limitReached": False, "submissionIds": []}


@contextmanager
def _locked_state() -> Iterator[dict[str, Any]]:
    """Yield the current day's state under an exclusive lock and save it back."""
    path = flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        today, limit = local_today(), daily_limit()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}
        if not isinstance(state, dict) or state.get("date") != today:
            # A new local day resets the counter and clears the flag at 00:00.
            state = _fresh_state(today, limit)
        state["limit"] = limit
        yield state
        state["count"] = max(0, int(state.get("count") or 0))
        state["limitReached"] = state["count"] >= limit
        state["updatedAt"] = _utc_now()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def read_state() -> dict[str, Any]:
    with _locked_state() as state:
        return dict(state)


def reserve(platform_count: Callable[[], int | None] | None = None) -> dict[str, Any]:
    """Reserve one submission slot for today.

    Returns ``{"reserved": bool, ...state}``. The platform count is queried on
    every reservation attempt, including when an older flag says the limit was
    reached. This lets a stale overcount be corrected by the platform overview.
    While the limit is not currently reached, the larger of the platform and
    local values is kept so an in-flight local reservation is not discarded.
    """
    with _locked_state() as state:
        if platform_count is not None:
            try:
                remote = platform_count()
            except Exception:  # noqa: BLE001 - platform outage must not block on its own
                remote = None
            if remote is not None:
                remote = max(0, int(remote))
                local = max(0, int(state.get("count") or 0))
                state["platformCount"] = remote
                # A stale true flag may come from an older list-based counter.
                # If the overview says there is room again, trust the platform
                # value and clear the stale flag before reserving this slot.
                if state.get("limitReached") and remote < int(state["limit"]):
                    state["count"] = remote
                    state["limitReached"] = False
                else:
                    state["count"] = max(local, remote)
        count = int(state.get("count") or 0)
        if state.get("limitReached") or count >= int(state["limit"]):
            state["limitReached"] = True
            return {"reserved": False, **state}
        state["count"] = count + 1
        return {"reserved": True, **state}


def release() -> None:
    """Give back a reserved slot when the POST did not create a submission."""
    with _locked_state() as state:
        state["count"] = int(state.get("count") or 0) - 1


def record_submission(submission_id: str) -> None:
    with _locked_state() as state:
        ids = [str(x) for x in state.get("submissionIds") or []]
        if submission_id and submission_id not in ids:
            ids.append(submission_id)
        state["submissionIds"] = ids


def deferred_base(task_root: Path) -> Path:
    """Dated ``待提交`` folders sit next to the task roots, one level above each."""
    configured = os.environ.get(DEFERRED_DIR_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else task_root.parent


def deferred_dir(task_root: Path, date: str) -> Path:
    """e.g. ``<task root parent>/2026-09-28待提交``, dated by the day the record was created."""
    return deferred_base(task_root) / f"{date}{DEFERRED_DIR_SUFFIX}"


def _record_path(folder: Path, task_root: Path) -> Path:
    return folder / f"{task_root.name}.json"


def _local_now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _rewrite_index(folder: Path) -> Path:
    records = []
    for path in sorted(folder.glob("*.json")):
        if path.name == DEFERRED_INDEX_NAME:
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    records.sort(key=lambda item: str(item.get("deferredAt") or ""))
    index = {"schemaVersion": 1, "updatedAt": _utc_now(), "total": len(records), "items": records}
    index_path = folder / DEFERRED_INDEX_NAME
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index_path


# --- 状态表：每个暂存过的任务一行，防止重复提交和漏提交 ---
STATUS_TABLE_NAME = "待提交状态表.json"
STATUS_CSV_NAME = "待提交状态表.csv"
STATUS_PENDING = "pending"
STATUS_SUBMITTING = "submitting"
STATUS_SUBMITTED = "submitted"
STATUS_FAILED = "failed"
STATUS_LABELS = {
    STATUS_PENDING: "待提交",
    STATUS_SUBMITTING: "提交中",
    STATUS_SUBMITTED: "已提交",
    STATUS_FAILED: "提交失败待重试",
}
OPEN_STATUSES = {STATUS_PENDING, STATUS_SUBMITTING, STATUS_FAILED}
CSV_COLUMNS = [
    ("taskName", "任务名"), ("deferredDate", "暂存日期"), ("statusLabel", "状态"),
    ("submissionId", "提交ID"), ("qcStatus", "质检状态"), ("attempts", "尝试次数"),
    ("lastExitCode", "最近退出码"), ("lastAttemptAt", "最近尝试时间"), ("submittedAt", "提交时间"),
    ("message", "说明"), ("taskRoot", "任务路径"),
]


def status_table_path(base: Path) -> Path:
    return base / STATUS_TABLE_NAME


def _write_status_csv(base: Path, rows: dict[str, dict[str, Any]]) -> None:
    import csv

    ordered = sorted(rows.values(), key=lambda row: (str(row.get("deferredDate") or ""), str(row.get("taskName") or "")))
    with open(base / STATUS_CSV_NAME, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([label for _, label in CSV_COLUMNS])
        for row in ordered:
            writer.writerow([row.get(key, "") if row.get(key) is not None else "" for key, _ in CSV_COLUMNS])


@contextmanager
def locked_status_table(base: Path) -> Iterator[dict[str, dict[str, Any]]]:
    """Yield ``{taskRoot: row}`` under an exclusive lock and save it (JSON + CSV) back."""
    base.mkdir(parents=True, exist_ok=True)
    path = status_table_path(base)
    with open(base / ".待提交状态表.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            table = {}
        rows: dict[str, dict[str, Any]] = table.get("items") if isinstance(table.get("items"), dict) else {}
        yield rows
        for row in rows.values():
            row["statusLabel"] = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
        counts: dict[str, int] = {}
        for row in rows.values():
            counts[row["statusLabel"]] = counts.get(row["statusLabel"], 0) + 1
        payload = {"schemaVersion": 1, "updatedAt": _local_now_text(), "counts": counts, "items": rows}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        _write_status_csv(base, rows)


def load_status_table(base: Path) -> dict[str, dict[str, Any]]:
    try:
        table = json.loads(status_table_path(base).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items = table.get("items")
    return items if isinstance(items, dict) else {}


def update_status(base: Path, task_root: Path, **fields: Any) -> dict[str, Any]:
    key = str(task_root)
    with locked_status_table(base) as rows:
        row = rows.setdefault(key, {"taskRoot": key, "taskName": task_root.name, "attempts": 0})
        row.update(fields)
        row["updatedAt"] = _local_now_text()
        return dict(row)


def task_submission_result(task_root: Path) -> dict[str, Any]:
    """The task's own submit_api result; a submissionId there means it was POSTed."""
    path = task_root / "workspace" / "评审文件" / "pre-submit" / "submission-api-result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) and str(value.get("submissionId") or "").strip() else {}


def defer(task_root: Path, payload_path: Path, payload: dict[str, Any], state: dict[str, Any],
          command: list[str], reason: str = "当日额度已满，已暂存") -> dict[str, Any]:
    """Park one task in ``<today>待提交`` and register it as pending in the status table."""
    # A task re-parked on a later full day keeps only its newest record.
    clear_deferred(task_root)
    # The folder is dated by when this record is really created, not by the quota day.
    today = local_today()
    folder = deferred_dir(task_root, today)
    folder.mkdir(parents=True, exist_ok=True)
    record = {
        "schemaVersion": 1,
        "taskRoot": str(task_root),
        "taskName": task_root.name,
        "payloadPath": str(payload_path),
        "uploads": {label: str((info or {}).get("path") or "")
                    for label, info in (payload.get("uploads") or {}).items()},
        "deferredAt": _utc_now(),
        "deferredLocalTime": _local_now_text(),
        "deferredDate": today,
        "quotaDate": state.get("date"),
        "quotaCount": state.get("count"),
        "quotaLimit": state.get("limit"),
        "deferReason": reason,
        "submitAfter": next_local_midnight(),
        "command": shlex.join(command),
    }
    record_path = _record_path(folder, task_root)
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path = _rewrite_index(folder)
    update_status(deferred_base(task_root), task_root, status=STATUS_PENDING, deferredDate=today,
                  deferredAt=record["deferredLocalTime"], recordPath=str(record_path),
                  message=reason)
    return {"recordPath": str(record_path), "indexPath": str(index_path), **record}


def clear_deferred(task_root: Path) -> None:
    """Drop the task from every dated ``待提交`` folder once it has really been submitted."""
    clear_deferred_in(deferred_base(task_root), task_root)


def clear_deferred_in(base: Path, task_root: Path) -> None:
    if not base.is_dir():
        return
    for folder in base.glob(f"*{DEFERRED_DIR_SUFFIX}"):
        record_path = _record_path(folder, task_root)
        if folder.is_dir() and record_path.is_file():
            record_path.unlink()
            _rewrite_index(folder)


def note_submitted(task_root: Path, submission_id: str, qc_status: str) -> None:
    """Mark a parked task as submitted; tasks never parked get no row."""
    base = deferred_base(task_root)
    if str(task_root) not in load_status_table(base):
        return
    update_status(base, task_root, status=STATUS_SUBMITTED, submissionId=submission_id,
                  qcStatus=qc_status, submittedAt=_local_now_text(), message="已提交")


def main() -> int:
    state = read_state()
    print(json.dumps({"flagPath": str(flag_path()), **state}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
