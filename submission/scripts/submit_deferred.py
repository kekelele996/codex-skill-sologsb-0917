#!/usr/bin/env python3
"""Submit parked ``<date>待提交`` tasks after local midnight at a steady pace.

Only tasks parked before the current local day are drained. The eligible tasks
are spread evenly across the window (default 10 hours): with N tasks the i-th
submission starts at ``window_start + i * hours / N``. Each one goes through
``submit_api.py --execute``, so every audit gate and the daily quota still
apply. The default mode only prints the schedule; ``--execute`` submits.

``待提交状态表.json`` (with a CSV copy) next to the folders is the ledger that
prevents duplicates and misses: candidates are the union of the folder records
and every open ledger row, and a task whose own result file already holds a
submission id is marked submitted instead of being POSTed again.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_quota  # noqa: E402

SUBMIT_SCRIPT = Path(__file__).with_name("submit_api.py")
DEFAULT_WINDOW_HOURS = 10.0
MAX_QC_POLL_SECONDS = 600.0
LOG_NAME = "待提交日志.jsonl"
EXIT_DEFERRED = 3


def default_base() -> Path:
    configured = os.environ.get(daily_quota.DEFERRED_DIR_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()


def platform_count_getter() -> Callable[[], int | None] | None:
    """读平台当天提交量的回调；缺凭据或平台地址时返回 None，不影响本机节奏。"""
    try:
        import submit_api
    except Exception:  # noqa: BLE001 - 只是拿平台额度，导入失败不阻断
        return None
    server = str(getattr(submit_api, "DEFAULT_SERVER", "") or "").strip()
    if not server:
        return None
    service = str(getattr(submit_api, "DEFAULT_KEYCHAIN_SERVICE", "") or "").strip()

    def read() -> int | None:
        cookie, csrf = submit_api.credentials(service)
        return submit_api.platform_today_count(server, cookie, csrf)

    return read


def local_now() -> datetime:
    return datetime.now().astimezone()


def next_midnight(now: datetime) -> datetime:
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _folder_records(base: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for folder in sorted(base.glob(f"*{daily_quota.DEFERRED_DIR_SUFFIX}")):
        if not folder.is_dir():
            continue
        folder_date = folder.name[: -len(daily_quota.DEFERRED_DIR_SUFFIX)]
        for path in sorted(folder.glob("*.json")):
            if path.name == daily_quota.DEFERRED_INDEX_NAME:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("taskRoot"):
                records.append({**record, "deferredDate": folder_date, "recordPath": str(path)})
    return records


def reconcile(base: Path) -> None:
    """Bring the ledger in line with folder records and each task's own result file."""
    records = _folder_records(base)
    with daily_quota.locked_status_table(base) as rows:
        for record in records:
            key = str(record["taskRoot"])
            row = rows.get(key)
            if row is None:
                rows[key] = {"taskRoot": key, "taskName": record.get("taskName") or Path(key).name,
                             "attempts": 0, "status": daily_quota.STATUS_PENDING,
                             "deferredDate": record["deferredDate"], "deferredAt": record.get("deferredLocalTime") or record.get("deferredAt"),
                             "recordPath": record["recordPath"], "message": "从待提交文件夹补登记"}
            elif row.get("status") in daily_quota.OPEN_STATUSES:
                # The newest folder record wins, so a re-parked task moves to its new date.
                if str(record["deferredDate"]) >= str(row.get("deferredDate") or ""):
                    row["deferredDate"] = record["deferredDate"]
                    row["recordPath"] = record["recordPath"]
        for key, row in rows.items():
            if row.get("status") == daily_quota.STATUS_SUBMITTED:
                continue
            result = daily_quota.task_submission_result(Path(key))
            if result:
                # Already POSTed (maybe by hand, or a run that died mid-way): never submit again.
                row.update(status=daily_quota.STATUS_SUBMITTED, submissionId=str(result["submissionId"]),
                           qcStatus=str(result.get("statusValue") or ""),
                           submittedAt=str(result.get("submittedAt") or ""), message="任务目录已有提交结果，不再提交")
            elif row.get("status") == daily_quota.STATUS_SUBMITTING:
                row.update(status=daily_quota.STATUS_FAILED, message="上次提交中断且没有提交结果，重新排队")
    for key, row in daily_quota.load_status_table(base).items():
        if row.get("status") == daily_quota.STATUS_SUBMITTED:
            daily_quota.clear_deferred_in(base, Path(key))


def due_rows(base: Path, today: str) -> list[dict[str, Any]]:
    rows = [row for row in daily_quota.load_status_table(base).values()
            if row.get("status") in daily_quota.OPEN_STATUSES and str(row.get("deferredDate") or "") < today]
    rows.sort(key=lambda row: (str(row.get("deferredDate") or ""), str(row.get("deferredAt") or ""),
                               str(row.get("taskName") or "")))
    return rows


def build_schedule(items: list[dict[str, Any]], start: datetime, hours: float) -> list[dict[str, Any]]:
    if not items:
        return []
    interval = hours * 3600.0 / len(items)
    return [{**item, "at": start + timedelta(seconds=i * interval), "intervalSeconds": interval}
            for i, item in enumerate(items)]


def sleep_until(moment: datetime, now: Callable[[], datetime], sleep: Callable[[float], None]) -> None:
    # Short naps against the wall clock so a laptop sleep does not stretch the pace.
    while True:
        remaining = (moment - now()).total_seconds()
        if remaining <= 0:
            return
        sleep(min(remaining, 30.0))


def submit_one(item: dict[str, Any], poll_timeout: float) -> int:
    command = [sys.executable, str(SUBMIT_SCRIPT), "--task-root", str(item["taskRoot"]),
               "--execute", "--poll-timeout", f"{poll_timeout:g}"]
    return subprocess.run(command, check=False).returncode


def append_log(base: Path, entry: dict[str, Any]) -> None:
    with open(base / LOG_NAME, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_item(base: Path, item: dict[str, Any], poll_timeout: float,
             runner: Callable[[dict[str, Any], float], int], now: Callable[[], datetime]) -> dict[str, Any]:
    task_root = Path(item["taskRoot"])
    row = daily_quota.load_status_table(base).get(str(task_root)) or {}
    if row.get("status") == daily_quota.STATUS_SUBMITTED or daily_quota.task_submission_result(task_root):
        reconcile(base)
        return {"outcome": "skipped_already_submitted", "exitCode": None}
    daily_quota.update_status(base, task_root, status=daily_quota.STATUS_SUBMITTING,
                              attempts=int(row.get("attempts") or 0) + 1,
                              lastAttemptAt=now().isoformat(timespec="seconds"), message="提交中")
    code = runner(item, poll_timeout)
    # Judge by the task's own result file, not by the exit code alone.
    result = daily_quota.task_submission_result(task_root)
    if result:
        daily_quota.clear_deferred_in(base, task_root)
        daily_quota.update_status(base, task_root, status=daily_quota.STATUS_SUBMITTED, lastExitCode=code,
                                  submissionId=str(result["submissionId"]),
                                  qcStatus=str(result.get("statusValue") or ""),
                                  submittedAt=str(result.get("submittedAt") or now().isoformat(timespec="seconds")),
                                  message="已提交")
        outcome = "submitted"
    elif code == EXIT_DEFERRED:
        # submit_api already re-parked it under today's date and set the row back to pending.
        daily_quota.update_status(base, task_root, lastExitCode=code)
        outcome = "deferred_daily_limit"
    else:
        daily_quota.update_status(base, task_root, status=daily_quota.STATUS_FAILED, lastExitCode=code,
                                  message=f"submit_api 退出码 {code}，未产生提交记录，下次重试")
        outcome = "failed"
    return {"outcome": outcome, "exitCode": code}


def drain(
    base: Path,
    hours: float,
    *,
    execute: bool,
    now: Callable[[], datetime] = local_now,
    sleep: Callable[[float], None] = time.sleep,
    runner: Callable[[dict[str, Any], float], int] = submit_one,
    platform_count: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    reconcile(base)
    current = now()
    today = current.strftime("%Y-%m-%d")
    if due_rows(base, today):
        start = current
    else:
        # Nothing is due yet: today's parked tasks become due at midnight.
        start = next_midnight(current)
        today = start.strftime("%Y-%m-%d")
        if execute:
            print(f"等待到 {start.isoformat()} 后开始提交", file=sys.stderr, flush=True)
            sleep_until(start, now, sleep)
            reconcile(base)

    items = due_rows(base, today)
    state = daily_quota.read_state() if execute else {"count": 0, "limit": daily_quota.daily_limit()}
    if execute and platform_count is not None:
        # 当天量以平台总览接口为准，别的设备提交的记录也算在同一份额度里。
        state = daily_quota.merge_platform_count(platform_count)
    remaining = max(0, int(state["limit"]) - int(state.get("count") or 0))
    planned, left_over = items[:remaining], items[remaining:]
    schedule = build_schedule(planned, start, hours)
    poll_timeout = min(MAX_QC_POLL_SECONDS, schedule[0]["intervalSeconds"] / 2) if schedule else 0.0
    summary: dict[str, Any] = {
        "status": "dry_run" if not execute else "done",
        "base": str(base),
        "statusTable": str(daily_quota.status_table_path(base)),
        "windowStart": start.isoformat(),
        "windowHours": hours,
        "intervalSeconds": schedule[0]["intervalSeconds"] if schedule else None,
        "qcPollTimeoutSeconds": poll_timeout,
        "planned": [{"taskName": item.get("taskName"), "deferredDate": item.get("deferredDate"),
                     "at": item["at"].isoformat()} for item in schedule],
        "leftForNextDay": [item.get("taskName") for item in left_over],
        "results": [],
    }
    if not execute:
        return summary

    for item in schedule:
        sleep_until(item["at"], now, sleep)
        started = now()
        outcome = run_item(base, item, poll_timeout, runner, now)
        entry = {"taskName": item.get("taskName"), "taskRoot": item["taskRoot"],
                 "scheduledAt": item["at"].isoformat(), "startedAt": started.isoformat(),
                 "finishedAt": now().isoformat(), **outcome}
        summary["results"].append(entry)
        append_log(base, entry)
        if outcome["outcome"] == "deferred_daily_limit":
            # The platform already holds a full day: stop, the rest waits for tomorrow.
            summary["status"] = "stopped_daily_limit"
            break
    summary["counts"] = {label: sum(1 for row in daily_quota.load_status_table(base).values()
                                    if row.get("statusLabel") == label)
                         for label in daily_quota.STATUS_LABELS.values()}
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="0 点后按匀速节奏提交 <日期>待提交 文件夹里的任务")
    parser.add_argument("--base", type=Path, default=None,
                        help="待提交文件夹所在目录，即任务根目录的上一级；默认当前目录")
    parser.add_argument("--hours", type=float, default=DEFAULT_WINDOW_HOURS, help="在多少小时内匀速提交完，默认 10")
    parser.add_argument("--execute", action="store_true", help="真正提交；默认只打印时间表")
    args = parser.parse_args()
    if args.hours <= 0:
        raise RuntimeError("--hours 必须大于 0")
    base = (args.base.expanduser().resolve() if args.base else default_base())
    base.mkdir(parents=True, exist_ok=True)
    # submit_api must park and log into the same base as this run's status table.
    os.environ[daily_quota.DEFERRED_DIR_ENV] = str(base)
    lock_path = base / ".submit-deferred.lock"
    with open(lock_path, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(f"已有 submit-deferred 在运行: {lock_path}") from None
        summary = drain(base, args.hours, execute=args.execute,
                        platform_count=platform_count_getter() if args.execute else None)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = [r for r in summary["results"] if r["outcome"] == "failed"]
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
