#!/usr/bin/env python3
"""Cross-process project selection and ownership locks for sologsb-0917.

The lock root is intentionally shared with solo2-auto.  A project claimed here
must also be skipped by solo2-auto, and vice versa.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from common import SologsbError, read_json, utc_now, write_json

DEFAULT_SELECTION_LOCK_TIMEOUT = 600.0
DEFAULT_PROJECT_CLAIM_TTL_SECONDS = 24 * 60 * 60
CLAIM_ROOT_ENV = "SOLO2_PLATFORM_CLAIM_ROOT"
SELECTION_TIMEOUT_ENV = "SOLOGBS_PLATFORM_SELECTION_TIMEOUT"
CLAIM_TTL_ENV = "SOLOGBS_PROJECT_CLAIM_TTL_SECONDS"
CONTAINER_TIMESTAMP_RE = re.compile(r"-\d{8,}$")
_HOLDER_PROCESSES: dict[str, subprocess.Popen[Any]] = {}


def platform_claim_root() -> Path:
    configured = os.environ.get(CLAIM_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return (codex_home / "solo2-auto" / "locks" / "platform-claims").resolve()


def platform_base_digest(base_url: str) -> str:
    return hashlib.sha256(base_url.rstrip("/").encode("utf-8")).hexdigest()[:16]


def platform_selection_lock_path(base_url: str) -> Path:
    return platform_claim_root() / f".selection-{platform_base_digest(base_url)}.lock"


def _selection_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return float(timeout)
    raw = os.environ.get(SELECTION_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_SELECTION_LOCK_TIMEOUT
    try:
        return float(raw)
    except ValueError as exc:
        raise SologsbError(f"{SELECTION_TIMEOUT_ENV} 不是有效秒数: {raw}") from exc


@contextlib.contextmanager
def platform_selection_lock(
    base_url: str,
    *,
    timeout: float | None = None,
) -> Iterator[Any]:
    """Serialize project selection against solo2-auto and all sologsb sessions."""
    wait_seconds = _selection_timeout(timeout)
    if wait_seconds < 0:
        raise SologsbError("平台项目选择锁等待时间不能为负数")
    lock_path = platform_selection_lock_path(base_url)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise SologsbError(
                    f"等待平台项目选择锁超过 {wait_seconds:g} 秒: {lock_path}"
                )
            time.sleep(0.2)
    try:
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def claimed_project_codes(base_url: str) -> set[str]:
    """Return project codes whose cross-process claim lock is currently held."""
    claim_dir = platform_claim_root() / platform_base_digest(base_url)
    if not claim_dir.is_dir():
        return set()
    codes: set[str] = set()
    for metadata_path in claim_dir.glob("*.json"):
        metadata = read_json(metadata_path, {})
        if not isinstance(metadata, dict):
            continue
        code = str(metadata.get("projectCode") or "").strip()
        lock_path = Path(str(metadata.get("lockPath") or ""))
        if not code or not lock_path.is_file():
            continue
        try:
            with lock_path.open("a+", encoding="utf-8") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except BlockingIOError:
                    codes.add(code.casefold())
        except OSError:
            continue
    return codes


def container_name_project_code(name: str) -> str:
    """Parse `claude-<project-code>-<timestamp>` container names."""
    prefix = "claude-"
    if not name.startswith(prefix):
        return ""
    body = CONTAINER_TIMESTAMP_RE.sub("", name[len(prefix):])
    return body.strip().strip("-")


def task_project_code(task_root: Path) -> str:
    state = read_json(task_root / "monitor" / "state.json", {}) or {}
    source = state.get("source") if isinstance(state.get("source"), dict) else {}
    selection_doc = read_json(task_root / "monitor" / "platform-selection.json", {}) or {}
    selection = selection_doc.get("selection") if isinstance(selection_doc, dict) else {}
    selection = selection if isinstance(selection, dict) else {}
    return str(
        source.get("projectCode")
        or selection.get("projectCode")
        or ""
    ).strip()


def _task_is_active(state: dict[str, Any]) -> bool:
    status = str(state.get("status") or "").strip().casefold()
    if status in {"running", "blocked"}:
        return True
    sides = state.get("sides") if isinstance(state.get("sides"), dict) else {}
    candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
    for record in (*sides.values(), *candidates.values()):
        if not isinstance(record, dict):
            continue
        side_status = str(record.get("status") or "").strip().casefold()
        if side_status in {"running", "blocked", "attempt_invalid"}:
            return True
    return False


def _local_task_snapshot(workdir: Path | None) -> tuple[set[str], dict[str, str]]:
    codes: set[str] = set()
    container_codes: dict[str, str] = {}
    if not workdir or not workdir.is_dir():
        return codes, container_codes
    for state_path in sorted(workdir.glob("*/monitor/state.json")):
        task_root = state_path.parent.parent
        state = read_json(state_path, {}) or {}
        code = task_project_code(task_root)
        if code and _task_is_active(state):
            codes.add(code.casefold())
        sides = state.get("sides") if isinstance(state.get("sides"), dict) else {}
        candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
        for record in (*sides.values(), *candidates.values()):
            if not isinstance(record, dict):
                continue
            container = record.get("container") if isinstance(record.get("container"), dict) else {}
            name = str(container.get("name") or "").strip()
            if name and code:
                container_codes[name] = code
    return codes, container_codes


def running_container_project_codes(
    workdir: Path | None = None,
) -> tuple[set[str], str]:
    """Return project codes that are already executing on this host."""
    codes, container_codes = _local_task_snapshot(workdir)
    local_source = "本地任务状态" if codes else ""
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Label \"sologsb.project-code\"}}"],
            text=True,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return codes, local_source or "本地状态兜底"
    if proc.returncode != 0:
        return codes, local_source or "本地状态兜底"
    docker_codes: set[str] = set()
    for raw in proc.stdout.splitlines():
        name, _, label = raw.partition("\t")
        name = name.strip()
        label = label.strip()
        if label:
            docker_codes.add(label.casefold())
            continue
        code = container_name_project_code(name)
        if not code:
            code = container_codes.get(name, "")
        if code:
            docker_codes.add(code.casefold())
    codes |= docker_codes
    sources = ["docker ps"] if docker_codes else []
    if local_source:
        sources.append(local_source)
    return codes, " + ".join(sources) or "docker ps"


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_ttl(ttl: float | None) -> float:
    if ttl is not None:
        seconds = float(ttl)
    else:
        raw = os.environ.get(CLAIM_TTL_ENV, "").strip()
        try:
            seconds = float(raw) if raw else float(DEFAULT_PROJECT_CLAIM_TTL_SECONDS)
        except ValueError as exc:
            raise SologsbError(f"{CLAIM_TTL_ENV} 不是有效秒数: {raw}") from exc
    if seconds <= 0:
        raise SologsbError("项目占用锁 TTL 必须大于 0")
    return seconds


def start_project_claim(
    task_root: Path,
    base_url: str,
    project_code: str,
    *,
    project_name: str = "",
    ttl: float | None = None,
) -> dict[str, Any]:
    """Acquire the shared project claim and keep it until cleanup or TTL."""
    code = project_code.strip()
    if not code:
        raise SologsbError("项目编号为空，不能创建项目占用锁")
    base_url = base_url.rstrip("/")
    ttl_seconds = _claim_ttl(ttl)
    claim_dir = platform_claim_root() / platform_base_digest(base_url)
    claim_dir.mkdir(parents=True, exist_ok=True)
    code_digest = hashlib.sha256(code.casefold().encode("utf-8")).hexdigest()
    lock_path = claim_dir / f"{code_digest}.lock"
    metadata_path = claim_dir / f"{code_digest}.json"
    handle = lock_path.open("a+", encoding="utf-8")
    holder: subprocess.Popen[Any] | None = None
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as exc:
            existing = read_json(metadata_path, {}) or {}
            owner = str(existing.get("taskRoot") or existing.get("projectDir") or "未知目录")
            holder_pid = str(existing.get("holderPid") or "未知PID")
            raise SologsbError(
                f"项目 {code} 已被其他会话占用: {owner} (PID {holder_pid})"
            ) from exc
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; time.sleep(float(sys.argv[1]))",
                str(ttl_seconds),
                str(lock_path),
            ],
            pass_fds=(handle.fileno(),),
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _HOLDER_PROCESSES[str(lock_path)] = holder
        metadata = {
            "schemaVersion": 1,
            "claimedBy": "sologsb-0917",
            "baseUrl": base_url,
            "projectCode": code,
            "projectName": project_name,
            "taskRoot": str(task_root.resolve()),
            "projectDir": str(task_root.resolve()),
            "lockPath": str(lock_path),
            "holderPid": holder.pid,
            "ttlSeconds": ttl_seconds,
            "acquiredAt": utc_now(),
        }
        write_json(metadata_path, metadata)
        write_json(task_root / "monitor" / "platform-claim.json", metadata)
        return metadata
    except Exception:
        if holder is not None:
            if _process_alive(holder.pid):
                try:
                    os.kill(holder.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            _HOLDER_PROCESSES.pop(str(lock_path), None)
        if acquired:
            metadata_path.unlink(missing_ok=True)
            lock_path.unlink(missing_ok=True)
        raise
    finally:
        handle.close()


def release_project_claim(task_root: Path) -> dict[str, Any]:
    """Release a project claim and terminate its lock-holder process."""
    claim_path = task_root / "monitor" / "platform-claim.json"
    claim = read_json(claim_path, {})
    if not isinstance(claim, dict) or not claim:
        return {"status": "not_found"}
    base_url = str(claim.get("baseUrl") or "").strip()
    if base_url:
        with platform_selection_lock(base_url):
            return _release_project_claim_locked(task_root, claim)
    return _release_project_claim_locked(task_root, claim)


def _release_project_claim_locked(
    task_root: Path,
    claim: dict[str, Any],
) -> dict[str, Any]:
    claim_path = task_root / "monitor" / "platform-claim.json"
    lock_path = Path(str(claim.get("lockPath") or ""))
    holder_pid = int(claim.get("holderPid") or 0)
    root = platform_claim_root().resolve()
    try:
        lock_path.resolve().relative_to(root)
    except ValueError as exc:
        raise SologsbError(f"拒绝释放不属于共享锁目录的项目锁: {lock_path}") from exc
    stopped = False
    holder = _HOLDER_PROCESSES.pop(str(lock_path), None)
    if holder is not None:
        holder_pid = holder.pid or holder_pid
    if holder_pid and lock_path.is_file() and _process_alive(holder_pid):
        proc = subprocess.run(
            ["ps", "-p", str(holder_pid), "-o", "command="],
            text=True,
            capture_output=True,
            check=False,
        )
        command = proc.stdout.strip() if proc.returncode == 0 else ""
        if str(lock_path) not in command:
            raise SologsbError(
                f"项目锁持有进程与元数据不一致，拒绝释放: PID {holder_pid}"
            )
        os.kill(holder_pid, signal.SIGTERM)
        stopped = True
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _process_alive(holder_pid):
            time.sleep(0.1)
        if _process_alive(holder_pid):
            os.kill(holder_pid, signal.SIGKILL)
    if holder is not None:
        holder.wait(timeout=1)
    lock_path.with_suffix(".json").unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    claim_path.unlink(missing_ok=True)
    return {"status": "released", "holderPid": holder_pid, "stoppedHolder": stopped}
