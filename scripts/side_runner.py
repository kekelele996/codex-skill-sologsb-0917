#!/usr/bin/env python3
"""Run one strict first-round side in an isolated Docker workspace."""
from __future__ import annotations

import concurrent.futures
import fcntl
import importlib.util
import json
import os
import queue
import random
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import device_config
from common import (
    AUTO_RUNNER,
    SIDES,
    SologsbError,
    SIDE_LOWER,
    atomic_copy,
    command_exists,
    commit_url,
    is_lockfile,
    load_git_identity,
    mutate_state,
    paired_lockfiles,
    read_json,
    run,
    safe_slug,
    save_state,
    sha256_file,
    utc_now,
    write_json,
)
from github_repo import _ensure_origin_commit, _github_git_env
from project_claims import task_project_code
from semantic_review import build_packet, ensure_packets, validate_review
from trace_validator import validate_single_round

DEFAULT_IMAGE = os.environ.get(
    "SOLOSB_DOCKER_IMAGE",
    "adminfather/benzhi-claude-code2:20260919",
)
DEFAULT_MODEL = os.environ.get("SOLOSB_MODEL", "auto_model/urm")
DEFAULT_ANTHROPIC_BASE_URL = ""
DECLARED_CONTEXT_WINDOW = int(os.environ.get("SOLOSB_CONTEXT_WINDOW", "1000000"))
MAX_ATTEMPTS = 6
DEFAULT_CANDIDATE_COUNT = 2
CANDIDATE_PREFIX = "candidate-"
CLAUDE_MAX_RETRIES = str(os.environ.get("SOLOSB_CLAUDE_MAX_RETRIES", "10")).strip() or "10"
CONTAINER_LIMIT_PATH = Path(os.environ.get(
    "SOLOSB_CONTAINER_LIMIT_PATH",
    str(Path.home() / ".codex" / "sologsb-0917" / "container-limit.json"),
))
DEFAULT_MAX_CONTAINERS = 4
ABSOLUTE_MAX_CONTAINERS = 6
CONTAINER_QUEUE_POLL_SECONDS = 5.0
CONTAINER_SETTINGS_REFRESH_SECONDS = 180.0


def anthropic_base_url() -> str:
    """Return the runtime Anthropic-compatible relay base URL."""
    value = (
        os.environ.get("SOLOSB_ANTHROPIC_BASE_URL", "").strip()
        or DEFAULT_ANTHROPIC_BASE_URL
    ).rstrip("/")
    if not value:
        raise SologsbError(
            "缺少 LLM 中转站地址：请在设备配置里设置 claude.baseUrl，"
            "或运行 scripts/configure.py wizard"
        )
    if not value.startswith(("http://", "https://")):
        raise SologsbError(
            "SOLOSB_ANTHROPIC_BASE_URL 必须以 http:// 或 https:// 开头"
        )
    return value


def _emit_live(side: str, message: str) -> None:
    """Print one compact, secret-free execution event to the foreground."""
    text = " ".join(str(message).split())
    if len(text) > 1200:
        text = text[:1197] + "..."
    try:
        print(f"[{side}] {text}", flush=True)
    except (BrokenPipeError, OSError):
        pass


def _consume_live_events(path: Path, offset: int, side: str) -> int:
    """Read complete JSONL events appended so far and render useful progress."""
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    break
                offset = stream.tell()
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                try:
                    _render_live_event(event, side)
                except Exception as exc:
                    _emit_live(side, f"实时事件渲染已跳过: {type(exc).__name__}")
    except OSError:
        return offset
    return offset


def _tool_summary(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "Tool")
    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        return str(payload.get("command") or "")
    if name in {"Read", "Write", "Edit"}:
        return str(payload.get("file_path") or payload.get("path") or "")
    if name in {"Glob", "Grep"}:
        return " ".join(
            str(payload.get(key) or "")
            for key in ("pattern", "path")
            if payload.get(key)
        )
    return json.dumps(payload, ensure_ascii=False)[:500] if payload else ""


def _api_retry_events(path: Path) -> list[dict[str, Any]]:
    """Read Claude auto-reconnect events without treating them as new attempts."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") == "system" and str(event.get("subtype") or "") == "api_retry":
            events.append(event)
    return events


def _api_transport_error(path: Path) -> str:
    """Return only a final transport failure, not a successful auto-reconnect."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for raw in lines:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        subtype = str(event.get("subtype") or "")
        if event_type == "system" and subtype == "api_error":
            status = event.get("error_status") or event.get("status") or ""
            detail = event.get("error") or event.get("message") or subtype
            return f"API/网络错误: {detail}" + (f" status={status}" if status else "")
        if event_type in {"error", "api_error"}:
            return "API/网络错误: " + str(event.get("error") or event.get("message") or event_type)
        if event_type == "result" and event.get("is_error") is True:
            return "模型执行错误: " + str(event.get("result") or event.get("error") or "is_error=true")
    return ""


def _result_summary(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(part for part in parts if part)
    return str(content or "")


def _render_live_event(event: dict[str, Any], side: str) -> None:
    event_type = str(event.get("type") or "")
    if event_type == "system":
        if str(event.get("subtype") or "") == "init":
            model = str(event.get("model") or "")
            tools = event.get("tools") if isinstance(event.get("tools"), list) else []
            _emit_live(side, f"会话已启动 model={model} tools={len(tools)}")
        return
    if event_type == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "thinking":
                _emit_live(side, "模型正在思考")
            elif block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    _emit_live(side, f"模型输出 {text}")
            elif block_type == "tool_use":
                detail = _tool_summary(block)
                _emit_live(side, f"调用工具 {block.get('name') or 'Tool'} {detail}".rstrip())
        return
    if event_type == "user":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or str(block.get("type") or "") != "tool_result":
                continue
            summary = _result_summary(block.get("content"))
            if summary:
                _emit_live(side, f"工具结果 {summary}")
        return
    if event_type == "result":
        _emit_live(
            side,
            "模型执行结束 "
            + " ".join(
                f"{key}={event.get(key)}"
                for key in ("subtype", "stop_reason", "num_turns", "duration_ms")
                if event.get(key) is not None
            ),
        )


def _load_auto_runner():
    if not AUTO_RUNNER.is_file():
        raise SologsbError(f"缺少运行适配器: {AUTO_RUNNER}")
    spec = importlib.util.spec_from_file_location("sologsb_auto_runner", AUTO_RUNNER)
    if spec is None or spec.loader is None:
        raise SologsbError("无法加载 solo2-auto 运行适配器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _claude_secret() -> str:
    env_key = os.environ.get("SOLOSB_CLAUDE_KEY", "").strip()
    if env_key:
        return env_key
    module = _load_auto_runner()
    return str(module.claude_keychain_secret())


def _claude_command(session_id: str) -> str:
    args = [
        "claude",
        "--safe-mode",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--settings",
        '{"autoMemoryEnabled":false}',
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--tools",
        "Bash,Read,Write,Edit,Glob,Grep,TodoWrite",
        "--dangerously-skip-permissions",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--session-id",
        session_id,
    ]
    return "unset apikey APIKEY ANTHROPIC_API_KEY NODE_OPTIONS NODE_PATH; exec " + shlex.join(args)


def _git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None):
    return run(["git", "-C", str(repo), *args], check=check, env=env)


def candidate_id(index: int) -> str:
    if index < 1:
        raise SologsbError("候选编号必须从 1 开始")
    return f"{CANDIDATE_PREFIX}{index}"


def candidate_ids(count: int) -> tuple[str, ...]:
    if count < 2:
        raise SologsbError("至少需要 2 个候选才能竞逐 A/B")
    if count > 8:
        raise SologsbError("候选数最多为 8，避免无界并发占用主机资源")
    return tuple(candidate_id(index) for index in range(1, count + 1))


def _candidate_workspace(task_root: Path, candidate: str) -> Path:
    if not re.fullmatch(r"candidate-[1-9][0-9]*", candidate):
        raise SologsbError(f"非法候选目录名: {candidate}")
    return task_root / "source" / "candidates" / candidate


def _candidate_runtime_root(task_root: Path, candidate: str) -> Path:
    if not re.fullmatch(r"candidate-[1-9][0-9]*", candidate):
        raise SologsbError(f"非法候选目录名: {candidate}")
    return task_root / "monitor" / "runtime" / "candidates" / candidate


def _candidate_trace_root(task_root: Path, candidate: str) -> Path:
    if not re.fullmatch(r"candidate-[1-9][0-9]*", candidate):
        raise SologsbError(f"非法候选目录名: {candidate}")
    return task_root / "workspace" / "轨迹文件" / "candidates" / candidate


def _side_workspace(task_root: Path, side_state: dict[str, Any]) -> Path:
    configured = str(side_state.get("workspacePath") or "")
    if configured:
        return Path(configured)
    candidate = str(side_state.get("candidateId") or "")
    if candidate:
        return _candidate_workspace(task_root, candidate)
    side = str(side_state.get("side") or "").upper()
    if side in SIDE_LOWER:
        return task_root / "source" / SIDE_LOWER[side]
    raise SologsbError("侧状态缺少候选工作区映射")


def _clone_candidate(task_root: Path, state: dict[str, Any], candidate: str) -> Path:
    destination = _candidate_workspace(task_root, candidate)
    origin = task_root / "source" / "origin"
    if not (origin / ".git").is_dir():
        raise SologsbError(f"原始源码尚未建立本地 Git 基线，不能拉取候选: {origin}")
    initial = str(state.get("initialSnapshot") or "")
    if not initial:
        raise SologsbError("状态缺少 initialSnapshot")
    if destination.is_dir() and (destination / ".git").is_dir():
        head = _git(destination, "rev-parse", "HEAD", check=False).stdout.decode().strip()
        status = _git(destination, "status", "--porcelain", check=False).stdout.decode().strip()
        if head == initial and not status:
            return destination
    if destination.exists():
        mounted = _workspace_mounted_by_running_container(destination)
        if mounted:
            raise SologsbError(
                f"{candidate} 工作区仍被运行中的容器 {mounted} 挂载，拒绝删除重建"
            )
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--no-hardlinks", str(origin), str(destination)])
    head = _git(destination, "rev-parse", "HEAD").stdout.decode().strip()
    if head != initial:
        raise SologsbError(f"{candidate} 起始提交不是初始快照")
    status = _git(destination, "status", "--porcelain").stdout.decode().strip()
    if status:
        raise SologsbError(f"{candidate} 新建工作区不干净")
    return destination


def _docker_exists(name: str) -> bool:
    proc = run(["docker", "container", "inspect", name], check=False)
    return proc.returncode == 0


def _docker_running(name: str) -> bool:
    proc = run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return proc.returncode == 0 and proc.stdout.decode().strip() == "true"


def _remove_container(name: str) -> None:
    if name and _docker_exists(name):
        run(["docker", "rm", "-f", name], check=False)


class CandidateCancelled(SologsbError):
    """The race no longer needs this candidate; not a failed attempt."""


# Wait between failed attempts so a rate-limited key or a hiccuping Docker
# daemon does not burn all attempts within seconds.
ATTEMPT_BACKOFF_ENV = "SOLOGBS_ATTEMPT_BACKOFF_SECONDS"
ATTEMPT_BACKOFF_MAX_SECONDS = 300.0


def _attempt_backoff(attempt: int) -> float:
    try:
        base = float(os.environ.get(ATTEMPT_BACKOFF_ENV, "30") or 30)
    except ValueError:
        base = 30.0
    if base <= 0:
        return 0.0
    return min(ATTEMPT_BACKOFF_MAX_SECONDS, base * (2 ** max(0, attempt - 1)))


class _ContainerReservation:
    def __init__(self, path: Path | None):
        self.path = path

    def release(self) -> None:
        if self.path is None:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class _ContainerLimiter:
    """Global cross-process Docker slot limiter for non-test sologsb containers."""

    def __init__(self, config_path: Path = CONTAINER_LIMIT_PATH):
        self.config_path = config_path
        self.root = config_path.parent / "container-slots"
        self.reservations = self.root / "reservations"
        self.lock_path = self.root / "limit.lock"

    @staticmethod
    def _positive_int(value: Any, default: int = DEFAULT_MAX_CONTAINERS) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, parsed)

    def _configured_limit(self, data: dict[str, Any]) -> int:
        """按监控台托管值、设备配置、环境变量、兼容配置、代码默认值的顺序解析上限。

        监控台（sologsb-monitor）写入的 ``container-limit.json`` 带 ``managedBy``
        标记时优先生效，这样容器上限只需在监控台设置一处，技能与调度器读同一个值。
        设备配置优先于环境变量是为了让运行中的任务在排队期间重新读取同一份配置；
        否则进程启动时注入的 ``SOLOSB_MAX_CONTAINERS`` 会一直遮住后续修改。
        """
        managed_value = str(data.get("maxContainers") or "").strip() if data.get("managedBy") else ""
        if managed_value:
            return self._positive_int(managed_value)
        device_value = ""
        try:
            device_data = device_config.load()
            device_value = str(
                device_config.get_path(device_data, "claude.maxContainers", "") or ""
            ).strip()
        except (device_config.ConfigError, OSError):
            # 设备配置缺失或暂时不可读时继续使用环境变量或兼容配置。
            device_value = ""

        env_value = str(os.environ.get("SOLOSB_MAX_CONTAINERS") or "").strip()
        legacy_value = str(data.get("maxContainers") or "").strip()
        return self._positive_int(device_value or env_value or legacy_value or DEFAULT_MAX_CONTAINERS)

    def _settings(self) -> tuple[int, set[str], float]:
        data = read_json(self.config_path, {})
        if not isinstance(data, dict):
            data = {}
        limit = min(ABSOLUTE_MAX_CONTAINERS, self._configured_limit(data))
        excluded = {
            str(value).strip().casefold()
            for value in (data.get("excludedProjectCodes") or [])
            if str(value).strip()
        }
        env_excluded = os.environ.get("SOLOSB_EXCLUDED_PROJECT_CODES", "")
        if env_excluded:
            excluded = {value.strip().casefold() for value in env_excluded.split(",") if value.strip()}
        try:
            wait_seconds = float(os.environ.get("SOLOSB_CONTAINER_WAIT_SECONDS") or data.get("waitSeconds") or 14400)
        except (TypeError, ValueError):
            wait_seconds = 14400
        return max(1, limit), excluded, max(1.0, wait_seconds)

    @staticmethod
    def _container_is_excluded(name: str, project_code: str, excluded: set[str]) -> bool:
        code = str(project_code or "").strip().casefold()
        if code and code in excluded:
            return True
        container = str(name or "").casefold()
        if container.startswith("sologsb-"):
            return any(container.startswith(f"sologsb-{item}-") for item in excluded)
        # 其他工具起的容器没有 sologsb- 前缀，按名字前缀匹配排除项。
        return any(container == item or container.startswith(f"{item}-") for item in excluded)

    def _count_all_containers(self) -> bool:
        """监控台托管时可要求把本机所有运行中的容器都计入名额。"""
        data = read_json(self.config_path, {})
        return isinstance(data, dict) and bool(data.get("managedBy")) and bool(data.get("countAllContainers"))

    @staticmethod
    def _reap_orphan_containers() -> list[str]:
        """Remove candidate containers whose runner process is gone.

        The container only idles (``sleep infinity``) while its runner drives
        ``docker exec``; once the runner is SIGKILLed or the host rebooted, the
        container can never produce a trace again but still holds a slot.
        Containers without the run-pid label (older skill versions) are left
        alone.
        """
        proc = run(
            ["docker", "ps", "--filter", "label=sologsb-0917=true", "--format",
             '{{.Names}}\t{{.Label "sologsb.run-pid"}}'],
            check=False,
            timeout=15,
        )
        if proc.returncode != 0:
            return []
        removed: list[str] = []
        for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
            name, _, pid = raw.partition("\t")
            name, pid = name.strip(), pid.strip()
            if not name or not pid.isdigit() or int(pid) == os.getpid():
                continue
            if _ContainerLimiter._pid_alive(pid):
                continue
            rm = run(["docker", "rm", "-f", name], check=False, timeout=60)
            if rm.returncode == 0:
                removed.append(name)
                _emit_live("container", f"回收执行进程已退出的孤儿容器 {name}（PID {pid}）")
        return removed

    @staticmethod
    def _running_containers(count_all: bool = False) -> list[tuple[str, str]]:
        """列出正在运行的候选任务容器。

        默认只看本题型自己创建的候选容器：带 ``sologsb-0917=true`` 标签，或者名字符合
        ``sologsb-<任务>-candidate-<N>-...`` 的历史容器。数据库、验证 clone、监控台
        辅助容器等 ``sologsb-`` 前缀容器都不算任务容器，不占用并发名额。

        ``count_all`` 为真时（监控台 ``countAllContainers``），本机任何运行中的容器
        都占名额，与其他工具共用同一个整机上限。
        """
        proc = run(
            ["docker", "ps", "--format",
             '{{.Names}}\t{{.Label "sologsb.project-code"}}\t{{.Label "sologsb-0917"}}'],
            check=False,
            timeout=15,
        )
        if proc.returncode != 0:
            raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "无法读取 Docker 容器列表")
        records: list[tuple[str, str]] = []
        for raw in proc.stdout.decode("utf-8", errors="replace").splitlines():
            parts = raw.split("\t")
            name = parts[0].strip() if parts else ""
            label = parts[1].strip() if len(parts) > 1 else ""
            marker = parts[2].strip() if len(parts) > 2 else ""
            if not name:
                continue
            if not count_all:
                if not name.startswith("sologsb-"):
                    continue
                if marker != "true" and not CANDIDATE_CONTAINER_RE.match(name):
                    continue
            records.append((name, label))
        return records

    @staticmethod
    def _pid_alive(pid: Any) -> bool:
        try:
            value = int(pid)
            if value <= 0:
                return False
            os.kill(value, 0)
            return True
        except (TypeError, ValueError, OSError):
            return False

    def _classify_markers(
        self,
        running_names: set[str],
        excluded: set[str] | None = None,
    ) -> tuple[set[str], list[Path]]:
        """Split reservation markers into live reservations and dead ones.

        Shared by :meth:`acquire` and :meth:`status` so the two can never
        disagree about what a marker means. Callers inside ``acquire`` pass
        the same settings snapshot used for admission.
        """
        if excluded is None:
            _limit, excluded, _wait = self._settings()
        reservations: set[str] = set()
        dead: list[Path] = []
        for marker in list(self.reservations.glob("*.json")):
            data = read_json(marker, {})
            if not isinstance(data, dict):
                dead.append(marker)
                continue
            name = str(data.get("container") or "").strip()
            marker_code = str(data.get("projectCode") or "").strip()
            if not name or self._container_is_excluded(name, marker_code, excluded):
                continue
            if name in running_names:
                # The container exists, so this is no longer a reservation.
                continue
            if self._pid_alive(data.get("pid")):
                reservations.add(name)
                continue
            dead.append(marker)
        return reservations, dead

    def status(self) -> dict[str, Any]:
        """Read-only view of the slot ledger.

        The scheduler monitor reads the same reservation files to show
        "occupied / available" without taking a lock, so this never mutates
        state: dead markers are reported, not removed.
        """
        limit, excluded, _wait = self._settings()
        try:
            running = self._running_containers(self._count_all_containers())
        except SologsbError as exc:
            return {"ok": False, "error": str(exc), "limit": limit}
        running_names = {
            name for name, label in running
            if not self._container_is_excluded(name, label, excluded)
        }
        self.reservations.mkdir(parents=True, exist_ok=True)
        reservations, dead = self._classify_markers(running_names, excluded)
        # 运行中的容器与仍存活的预占位都占用名额，避免并发 docker run 突破上限。
        active_names = running_names | reservations
        used = len(active_names)
        return {
            "ok": True,
            "limit": limit,
            "excludedProjectCodes": sorted(excluded),
            "runningContainers": len(running_names),
            "reservedSlots": len(reservations),
            "advisoryReservedSlots": len(reservations),
            "used": used,
            "available": max(0, limit - used),
            "reservations": sorted(reservations),
            "runningNames": sorted(running_names),
            "deadMarkers": [str(path) for path in dead],
        }

    def acquire(
        self,
        project_code: str,
        container_name: str,
        stop_event: Any = None,
    ) -> _ContainerReservation:
        self.reservations.mkdir(parents=True, exist_ok=True)
        started_at = time.monotonic()
        next_settings_refresh = 0.0
        limit = DEFAULT_MAX_CONTAINERS
        excluded: set[str] = set()
        wait_seconds = 14400.0

        while True:
            now = time.monotonic()
            if now >= next_settings_refresh:
                limit, excluded, wait_seconds = self._settings()
                next_settings_refresh = now + max(0.0, CONTAINER_SETTINGS_REFRESH_SECONDS)

            if self._container_is_excluded(container_name, project_code, excluded):
                return _ContainerReservation(None)

            if stop_event is not None and stop_event.is_set():
                raise CandidateCancelled("已有两个候选先完成，放弃排队中的容器名额")
            elapsed = now - started_at
            if elapsed >= wait_seconds:
                raise SologsbError(
                    f"等待容器名额超时：非测试项目最多同时运行 {limit} 个容器，"
                    f"已等待 {int(wait_seconds)} 秒"
                )

            with self.lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    self._reap_orphan_containers()
                    running = self._running_containers(self._count_all_containers())
                    running_names = {
                        name for name, label in running
                        if not self._container_is_excluded(name, label, excluded)
                    }
                    reservations, dead = self._classify_markers(running_names, excluded)
                    for marker in dead:
                        try:
                            marker.unlink()
                        except OSError:
                            pass
                    # 必须在同一把独占锁内完成“统计运行中容器 + 统计存活预占位 + 写预占位”。
                    # 预约也占用名额，所以并发调用不会在容器尚未出现时同时越过上限。
                    active_names = running_names | reservations
                    if len(active_names) < limit:
                        marker_path = self.reservations / f"{uuid.uuid4().hex}.json"
                        write_json(marker_path, {
                            "container": container_name,
                            "projectCode": project_code,
                            "pid": os.getpid(),
                            "createdAt": utc_now(),
                        })
                        return _ContainerReservation(marker_path)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            _emit_live(project_code or "container", f"容器名额已满，排队等待（上限 {limit}）")
            remaining = max(0.0, wait_seconds - (time.monotonic() - started_at))
            time.sleep(min(CONTAINER_QUEUE_POLL_SECONDS, remaining))


CANDIDATE_CONTAINER_RE = re.compile(r"^sologsb-.+-candidate-\d+-")

_CONTAINER_LIMITER = _ContainerLimiter()


def _ensure_image(image: str) -> str:
    if not command_exists("docker"):
        raise SologsbError("缺少 Docker")
    inspect = run(["docker", "image", "inspect", image], check=False)
    if inspect.returncode != 0:
        pull = run(["docker", "pull", image], check=False, timeout=900)
        if pull.returncode != 0:
            raise SologsbError(pull.stderr.decode("utf-8", errors="replace") or "Docker 镜像拉取失败")
    digest = run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], check=False)
    if digest.returncode != 0 or not digest.stdout.strip():
        raise SologsbError("无法获取 Docker 镜像 digest")
    return digest.stdout.decode().strip()


def _start_container(
    *,
    task_root: Path,
    side: str,
    attempt_dir: Path,
    workspace: Path,
    secret: str,
    base_url: str,
    stop_event: Any = None,
) -> dict[str, Any]:
    claude_home = attempt_dir / "claude-home"
    (claude_home / "projects").mkdir(parents=True, exist_ok=True)
    (attempt_dir / "logs").mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(workspace, 0o777)
        os.chmod(claude_home, 0o777)
    except OSError:
        pass
    container = f"sologsb-{safe_slug(task_root.name)}-{side.lower()}-{int(time.time())}-{random.randrange(0x1000):03x}"
    setup = (
        "set -euo pipefail; "
        'test "$HOME" = /home/node; '
        'test "$(pwd)" = /workspace; '
        "mkdir -p /home/node/.claude/projects; "
        'test -z "$(find /home/node/.claude/projects -mindepth 1 -print -quit)" || { echo "transcript directory must be empty" >&2; exit 2; }; '
        "exec sleep infinity"
    )
    env = os.environ.copy()
    env["apikey"] = secret
    project_code = task_project_code(task_root)
    cmd = [
        "docker", "run", "-d", "--init", "--restart=no",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--name", container,
        "--label", "sologsb-0917=true",
        "--label", f"sologsb.run-pid={os.getpid()}",
    ]
    if project_code:
        cmd += ["--label", f"sologsb.project-code={project_code}"]
    cmd += [
        "--mount", f"type=bind,src={workspace},dst=/workspace",
        "--mount", f"type=bind,src={claude_home},dst=/home/node/.claude",
        "-e", "apikey",
        "-e", f"ANTHROPIC_BASE_URL={base_url}",
        "--entrypoint", "/bin/bash", DEFAULT_IMAGE,
        "-lc", setup,
    ]
    slot = _CONTAINER_LIMITER.acquire(project_code, container, stop_event)
    if stop_event is not None and stop_event.is_set():
        slot.release()
        raise CandidateCancelled("已有两个候选先完成，不再启动新容器")
    try:
        proc = run(cmd, env=env, check=False, timeout=180)
        if proc.returncode != 0:
            raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "容器启动失败")
        if not _docker_running(container):
            raise SologsbError(f"容器未运行: {container}")
    except Exception:
        slot.release()
        raise
    return {
        "name": container,
        "image": DEFAULT_IMAGE,
        "workspace": str(workspace),
        "claudeHome": str(claude_home),
        "attemptDir": str(attempt_dir),
        "baseUrl": base_url,
        "_slot": slot,
    }


def _runtime_info(container: str, secret: str, base_url: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["ANTHROPIC_AUTH_TOKEN"] = secret
    command = 'claude --version; printf "MODEL=%s CONTEXT=%s BASE=%s" "$ANTHROPIC_MODEL" "$CLAUDE_CODE_MAX_CONTEXT_TOKENS" "$ANTHROPIC_BASE_URL"'
    proc = run(
        ["docker", "exec", "-e", "ANTHROPIC_AUTH_TOKEN", "-e", f"ANTHROPIC_BASE_URL={base_url}", container, "bash", "-lc", command],
        env=env,
        check=False,
        timeout=60,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not output:
        raise SologsbError(f"无法获取 Claude Code 运行信息: {output}")
    version = output.splitlines()[0].strip()
    match = __import__("re").search(r"MODEL=(\S+) CONTEXT=(\d+) BASE=(\S+)", output)
    model = match.group(1) if match else ""
    context = int(match.group(2)) if match else 0
    runtime_base_url = match.group(3) if match else ""
    if model != DEFAULT_MODEL:
        raise SologsbError(f"容器模型 {model} 不等于 {DEFAULT_MODEL}")
    if context != DECLARED_CONTEXT_WINDOW:
        raise SologsbError(f"容器上下文窗口 {context} 不等于 {DECLARED_CONTEXT_WINDOW}")
    if runtime_base_url.rstrip("/") != base_url.rstrip("/"):
        raise SologsbError(
            f"容器 Base URL {runtime_base_url} 不等于运行配置 {base_url}"
        )
    return {
        "version": version,
        "model": model,
        "contextWindow": context,
        "baseUrl": runtime_base_url.rstrip("/"),
    }


def _find_trace(attempt_dir: Path, session_id: str) -> Path | None:
    root = attempt_dir / "claude-home"
    exact = root / "projects" / "-workspace" / f"{session_id}.jsonl"
    if exact.is_file():
        return exact
    matches = sorted(root.rglob(f"{session_id}.jsonl"))
    return matches[0] if matches else None


def _save_rejected(
    *,
    task_root: Path,
    candidate: str,
    attempt: int,
    trace: Path | None,
    stdout_path: Path,
    stderr_path: Path,
    reason: str,
    mapped_side: str = "",
) -> None:
    destinations = [
        _candidate_trace_root(task_root, candidate) / "rejected" / f"attempt-{attempt:02d}"
    ]
    if mapped_side in SIDE_LOWER:
        destinations.append(
            task_root / "workspace" / "轨迹文件" / mapped_side.lower() / "rejected" / f"attempt-{attempt:02d}"
        )
    for rejected in destinations:
        rejected.mkdir(parents=True, exist_ok=True)
        if trace and trace.is_file():
            atomic_copy(trace, rejected / trace.name)
        if stdout_path.is_file():
            atomic_copy(stdout_path, rejected / stdout_path.name)
        if stderr_path.is_file():
            atomic_copy(stderr_path, rejected / stderr_path.name)
        write_json(
            rejected / "rejection.json",
            {
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "reason": reason,
                "recordedAt": utc_now(),
            },
        )


def _diff_snapshot(repo: Path, initial_sha: str) -> dict[str, Any]:
    diff_stat = _git(repo, "diff", "--stat", initial_sha).stdout.decode("utf-8", errors="replace").strip()
    changed = [
        line for line in _git(repo, "diff", "--name-only", initial_sha).stdout.decode().splitlines()
        if line.strip()
    ]
    untracked = [
        line[3:].strip()
        for line in _git(repo, "status", "--porcelain").stdout.decode("utf-8", errors="replace").splitlines()
        if line.startswith("?? ")
    ]
    return {
        "diffStat": diff_stat,
        "changedFiles": list(dict.fromkeys(changed + untracked)),
        "workingTreeStatus": _git(repo, "status", "--short").stdout.decode("utf-8", errors="replace").strip(),
    }


GENERATED_PATH_EXCLUDES = (
    "node_modules/", "dist/", "build/", "coverage/", ".next/", ".nuxt/", ".vite/",
    "target/", "vendor/", "__pycache__/", ".venv/", "venv/", ".cache/",
)
# 锁文件不写进 .git/info/exclude：模型改了依赖清单时要随清单一起发布，
# 没改清单时才在 stage 后撤回（见 _unstage_generated_paths）。
BUSINESS_SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".graphql", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".less", ".m", ".mm", ".mjs", ".cjs",
    ".php", ".proto", ".py", ".rb", ".rs", ".scala", ".scss", ".sh", ".sql",
    ".svelte", ".swift", ".ts", ".tsx", ".vue",
}


def _install_generated_path_excludes(repo: Path) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
    marker = "# sologsb-generated-artifacts"
    if marker in existing:
        # 旧版本把锁文件也写进了排除清单；去掉这些行，锁文件改由 stage 后按配对规则处理。
        kept = [line for line in existing.splitlines() if not is_lockfile(line.strip())]
        text = "\n".join(kept) + "\n"
        if text != existing:
            exclude.write_text(text, encoding="utf-8")
        return
    text = existing.rstrip() + "\n\n" + marker + "\n" + "\n".join(GENERATED_PATH_EXCLUDES) + "\n"
    exclude.write_text(text.lstrip("\n"), encoding="utf-8")


def _is_generated_or_lock_path(path: str) -> bool:
    parts = [part for part in Path(path).parts if part not in {"", "."}]
    generated_dirs = {item.rstrip("/") for item in GENERATED_PATH_EXCLUDES}
    return any(part in generated_dirs for part in parts) or is_lockfile(path)


def _unstage_generated_paths(repo: Path, initial_sha: str) -> list[str]:
    """撤回生成物；锁文件只在对应依赖清单没改时撤回，改了就随清单一起发布。"""
    proc = _git(repo, "diff", "--cached", "--name-only", initial_sha, check=False)
    paths = [line.strip() for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    keep = paired_lockfiles(paths)
    generated = [path for path in paths if _is_generated_or_lock_path(path) and path not in keep]
    for offset in range(0, len(generated), 80):
        chunk = generated[offset : offset + 80]
        _git(repo, "reset", "-q", initial_sha, "--", *chunk, check=False)
    return generated


def _is_business_source(path: str) -> bool:
    parts = [part.lower() for part in Path(path).parts]
    name = parts[-1] if parts else ""
    if Path(path).suffix.lower() not in BUSINESS_SOURCE_EXTENSIONS:
        return False
    if any(part in {"test", "tests", "__tests__", "testdata", "spec"} for part in parts):
        return False
    return not (".test." in name or ".spec." in name or name.endswith("_test.go") or name.startswith("test_"))


def _staged_business_change_lines(repo: Path, initial_sha: str) -> tuple[int, list[str]]:
    files_proc = _git(repo, "diff", "--cached", "--name-only", initial_sha, check=False)
    files = [line.strip() for line in files_proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    business = [path for path in files if _is_business_source(path)]
    if not business:
        return 0, []
    total = 0
    for offset in range(0, len(business), 80):
        chunk = business[offset : offset + 80]
        proc = _git(repo, "diff", "--cached", "--numstat", initial_sha, "--", *chunk, check=False)
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
                continue
            total += max(int(parts[0]), int(parts[1]))
    return total, business


def _committed_business_change_lines(repo: Path, initial_sha: str, head: str) -> tuple[int, list[str]]:
    files_proc = _git(repo, "diff", "--name-only", initial_sha, head, check=False)
    files = [line.strip() for line in files_proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    business = [path for path in files if _is_business_source(path)]
    if not business:
        return 0, []
    total = 0
    for offset in range(0, len(business), 80):
        chunk = business[offset : offset + 80]
        proc = _git(repo, "diff", "--numstat", initial_sha, head, "--", *chunk, check=False)
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or parts[0] == "-" or parts[1] == "-":
                continue
            total += max(int(parts[0]), int(parts[1]))
    return total, business


def _line_gate_record(
    *,
    side: str,
    initial_sha: str,
    artifact_sha: str,
    code_lines: int,
    business_files: list[str],
) -> dict[str, Any]:
    passed = code_lines >= 10
    return {
        "id": "change-volume-line-gate",
        "side": side,
        "initialSnapshot": initial_sha,
        "artifactSnapshot": artifact_sha,
        "hardMinimumLines": 10,
        "businessCodeLines": code_lines,
        "businessCodeFiles": business_files,
        "hardOk": passed,
        "status": "passed" if passed else "failed",
        "blockingStage": "submit_preflight",
        "checkedAt": utc_now(),
    }


def _commit_local(repo: Path, side: str, initial_sha: str) -> dict[str, Any]:
    identity = load_git_identity()
    _git(repo, "config", "user.name", identity["name"])
    _git(repo, "config", "user.email", identity["email"])
    current = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    if current != initial_sha:
        parent_current = _git(repo, "rev-parse", "HEAD^").stdout.decode().strip()
        if parent_current != initial_sha:
            raise SologsbError(
                f"{side} 本地已有不可复用的产物提交历史: HEAD={current} parent={parent_current}"
            )
        code_lines, business_files = _committed_business_change_lines(repo, initial_sha, current)
        line_gate = _line_gate_record(
            side=side,
            initial_sha=initial_sha,
            artifact_sha=current,
            code_lines=code_lines,
            business_files=business_files,
        )
        return {
            "artifactSnapshot": current,
            "parentSnapshot": parent_current,
            "lineGate": line_gate,
            **_diff_snapshot(repo, initial_sha),
        }
    _install_generated_path_excludes(repo)
    _git(repo, "add", "-A")
    _unstage_generated_paths(repo, initial_sha)
    code_lines, business_files = _staged_business_change_lines(repo, initial_sha)
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": identity["name"],
            "GIT_AUTHOR_EMAIL": identity["email"],
            "GIT_COMMITTER_NAME": identity["name"],
            "GIT_COMMITTER_EMAIL": identity["email"],
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    _git(repo, "commit", "--allow-empty", "-m", f"fix: model {side} first-round result", env=env)
    artifact_sha = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    parent = _git(repo, "rev-parse", "HEAD^").stdout.decode().strip()
    if parent != initial_sha:
        raise SologsbError(f"{side} 产物父提交不是初始快照: {parent} != {initial_sha}")
    line_gate = _line_gate_record(
        side=side,
        initial_sha=initial_sha,
        artifact_sha=artifact_sha,
        code_lines=code_lines,
        business_files=business_files,
    )
    return {
        "artifactSnapshot": artifact_sha,
        "parentSnapshot": parent,
        "businessCodeLines": code_lines,
        "businessCodeFiles": business_files,
        "lineGate": line_gate,
        **_diff_snapshot(repo, initial_sha),
    }


def _atomic_publish(task_root: Path, state: dict[str, Any], sides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    initial_sha = str(state["initialSnapshot"])
    commits: dict[str, dict[str, Any]] = {}
    for side in SIDES:
        existing = sides.get(side) or {}
        repo = _side_workspace(task_root, {**existing, "side": side})
        if not repo.is_dir():
            raise SologsbError(
                f"{side} 工作区不存在，不能发布；candidate="
                f"{existing.get('candidateId') or '未记录'} path={repo}"
            )
        if existing.get("artifactSnapshot") and existing.get("parentSnapshot") == initial_sha:
            code_lines, business_files = _committed_business_change_lines(
                repo, initial_sha, str(existing["artifactSnapshot"])
            )
            commits[side] = {
                **existing,
                "lineGate": existing.get("lineGate") or _line_gate_record(
                    side=side,
                    initial_sha=initial_sha,
                    artifact_sha=str(existing["artifactSnapshot"]),
                    code_lines=code_lines,
                    business_files=business_files,
                ),
            }
        else:
            commits[side] = _commit_local(repo, side, initial_sha)
        sides[side].update(commits[side])

    origin = task_root / "source" / "origin"
    for side in SIDES:
        repo = _side_workspace(task_root, {**sides[side], "side": side})
        _git(origin, "fetch", str(repo), f"HEAD:refs/remotes/sologsb-local/{side}")

    token_proc = run(["gh", "auth", "token"])
    token = token_proc.stdout.decode().strip()
    owner = str(state.get("owner") or "")
    if not token or not owner:
        raise SologsbError("缺少 GitHub token 或 owner，无法发布 A/B")
    env = _github_git_env(owner, token)
    sha_a = str(commits["A"]["artifactSnapshot"])
    sha_b = str(commits["B"]["artifactSnapshot"])
    push = _git(
        origin,
        "push",
        "--atomic",
        "origin",
        f"{sha_a}:refs/heads/A",
        f"{sha_b}:refs/heads/B",
        check=False,
        env=env,
    )
    if push.returncode != 0:
        raise SologsbError(
            "A/B 原子推送失败，远端分支未发布: "
            + push.stderr.decode("utf-8", errors="replace")
        )
    remote = _git(origin, "ls-remote", "--heads", "origin").stdout.decode().splitlines()
    remote_heads = {
        line.split("refs/heads/", 1)[1]: line.split()[0]
        for line in remote if "refs/heads/" in line
    }
    if remote_heads.get("A") != sha_a or remote_heads.get("B") != sha_b:
        raise SologsbError(f"A/B 原子推送后远端校验失败: {remote_heads}")
    return {"A": commits["A"], "B": commits["B"], "remoteHeads": remote_heads}

def _candidate_attempt_dirs(task_root: Path, candidate: str) -> list[Path]:
    root = _candidate_runtime_root(task_root, candidate)
    if not root.is_dir():
        return []
    return sorted(
        (item for item in root.glob("attempt-*") if item.is_dir()),
        key=lambda item: int(item.name.split("-")[-1]) if item.name.split("-")[-1].isdigit() else 0,
    )


def _find_candidate_trace(attempt_dir: Path, session_id: str) -> Path | None:
    root = attempt_dir / "claude-home"
    exact = root / "projects" / "-workspace" / f"{session_id}.jsonl"
    if exact.is_file():
        return exact
    matches = sorted(root.rglob(f"{session_id}.jsonl"))
    return matches[0] if matches else None


def _stop_process_group(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_candidate_attempt(
    *,
    task_root: Path,
    state: dict[str, Any],
    candidate: str,
    attempt: int,
    timeout: float,
    live: bool,
    mapped_side: str = "",
    stop_event: Any = None,
) -> dict[str, Any]:
    initial_sha = str(state["initialSnapshot"])
    repo = _clone_candidate(task_root, state, candidate)
    attempt_dir = _candidate_runtime_root(task_root, candidate) / f"attempt-{attempt:02d}"
    if attempt_dir.exists():
        shutil.rmtree(attempt_dir)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = attempt_dir / "stdout.jsonl"
    stderr_path = attempt_dir / "stderr.log"
    secret = _claude_secret()
    base_url = anthropic_base_url()
    image_digest = _ensure_image(DEFAULT_IMAGE)
    container = ""
    container_slot: _ContainerReservation | None = None
    try:
        container_info = _start_container(
            task_root=task_root,
            side=candidate,
            attempt_dir=attempt_dir,
            workspace=repo,
            secret=secret,
            base_url=base_url,
            stop_event=stop_event,
        )
        container_slot = container_info.pop("_slot", None)
        container = container_info["name"]
        session_id = str(uuid.uuid4())
        prompt_path = Path(str(state["promptPath"]))
        prompt = prompt_path.read_text(encoding="utf-8")
        if sha256_file(prompt_path) != str(state["promptSha256"]):
            raise SologsbError("提示词文件已变化，拒绝启动")
        runtime_info = _runtime_info(container, secret, base_url)
        harness_version = runtime_info["version"]
        command = _claude_command(session_id)
        env = os.environ.copy()
        env["ANTHROPIC_AUTH_TOKEN"] = secret
        env["ANTHROPIC_MODEL"] = DEFAULT_MODEL
        env["ANTHROPIC_BASE_URL"] = base_url
        env["CLAUDE_CODE_MAX_RETRIES"] = CLAUDE_MAX_RETRIES
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(DECLARED_CONTEXT_WINDOW)
        docker_cmd = [
            "docker", "exec", "-i", "-w", "/workspace",
            "-e", "ANTHROPIC_AUTH_TOKEN", "-e", "ANTHROPIC_MODEL",
            "-e", f"ANTHROPIC_BASE_URL={base_url}",
            "-e", "CLAUDE_CODE_MAX_RETRIES", "-e", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
            container, "/bin/bash", "-lc", command,
        ]
        metadata = {
            "candidateId": candidate,
            "mappedSide": mapped_side,
            "attempt": attempt,
            "sessionId": session_id,
            "container": container_info,
            "harness": "Claude Code",
            "harnessVersion": harness_version,
            "model": runtime_info["model"],
            "imageDigest": image_digest,
            "declaredContextWindow": runtime_info["contextWindow"],
            "baseUrl": runtime_info["baseUrl"],
            "status": "running",
            "startedAt": utc_now(),
        }
        write_json(attempt_dir / "attempt.json", metadata)
        error = ""
        exit_code = -1
        trace: Path | None = None
        live_offset = 0
        auto_reconnect_count = 0
        stop_requested = False
        with prompt_path.open("rb") as stdin, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            proc = subprocess.Popen(
                docker_cmd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=True,
            )
            started = time.monotonic()
            first_assistant_deadline = started + float(os.environ.get("SOLOSB_FIRST_RESPONSE_TIMEOUT", "300"))
            idle_timeout = float(os.environ.get("SOLOSB_IDLE_TIMEOUT", "300"))
            assistant_seen = False
            last_size = -1
            last_change = started
            if live:
                _emit_live(
                    candidate,
                    f"开始首轮 attempt={attempt} session={session_id} "
                    f"prompt={str(state.get('promptSha256') or '')[:12]} container={container}",
                )
            try:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        stop_requested = True
                        error = "候选竞速已完成，未进入前两名；执行被主动终止"
                        break
                    code = proc.poll()
                    if code is not None:
                        exit_code = code
                        break
                    if time.monotonic() >= started + timeout:
                        error = f"执行超过 {timeout:.0f} 秒"
                        raise subprocess.TimeoutExpired(docker_cmd, timeout)
                    try:
                        current_size = stdout_path.stat().st_size
                        # Once seen it stays seen; rescanning a growing trace
                        # every poll only burns IO on long attempts.
                        if not assistant_seen and current_size != last_size:
                            with stdout_path.open("rb") as stream:
                                assistant_seen = any(
                                    b'"type":"assistant"' in line or b'"type": "assistant"' in line
                                    for line in stream
                                )
                    except OSError:
                        current_size = -1
                    if current_size != last_size:
                        last_size = current_size
                        last_change = time.monotonic()
                    if live and current_size > live_offset:
                        live_offset = _consume_live_events(stdout_path, live_offset, candidate)
                    if not assistant_seen and time.monotonic() >= first_assistant_deadline:
                        error = "超过首响应时限，仍未产生 assistant 事件"
                        raise subprocess.TimeoutExpired(docker_cmd, first_assistant_deadline - started)
                    if time.monotonic() - last_change >= idle_timeout:
                        error = f"超过无响应时限 {idle_timeout:.0f} 秒"
                        raise subprocess.TimeoutExpired(docker_cmd, idle_timeout)
                    time.sleep(min(2.0, max(0.2, (started + timeout - time.monotonic()))))
            except subprocess.TimeoutExpired:
                if not error:
                    error = f"执行超过 {timeout:.0f} 秒"
                _stop_process_group(proc)
                exit_code = 124
            if stop_requested:
                _stop_process_group(proc)
                exit_code = 130
            if live:
                _consume_live_events(stdout_path, live_offset, candidate)
        trace = _find_candidate_trace(attempt_dir, session_id)
        auto_reconnect_count = len(_api_retry_events(stdout_path))
        transport_error = _api_transport_error(stdout_path)
        if transport_error:
            error = error or transport_error
        if exit_code != 0 and not stop_requested:
            error = error or f"Claude 进程退出码 {exit_code}"
        if stop_requested:
            validation = None
        else:
            if trace is None:
                error = error or "未找到 SessionID 对应的原生 JSONL"
            validation = None
            if trace is not None:
                validation = validate_single_round(trace, expected_prompt=prompt, expected_session_id=session_id)
                if not validation["ok"]:
                    error = error or "; ".join(validation["errors"])
        if error:
            _save_rejected(
                task_root=task_root,
                candidate=candidate,
                mapped_side=mapped_side,
                attempt=attempt,
                trace=trace,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                reason=error,
            )
            return {
                **metadata,
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "status": "cancelled" if stop_requested else "attempt_invalid",
                "exitCode": exit_code,
                "error": error,
                "autoReconnectCount": auto_reconnect_count,
                "tracePath": str(trace) if trace else "",
                "validation": validation,
                "finishedAt": utc_now(),
            }

        candidate_trace = _candidate_trace_root(task_root, candidate) / trace.name
        atomic_copy(trace, candidate_trace)
        active_trace = candidate_trace
        if mapped_side in SIDE_LOWER:
            active_trace = task_root / "workspace" / "轨迹文件" / mapped_side.lower() / trace.name
            atomic_copy(trace, active_trace)
        diff = _diff_snapshot(repo, initial_sha)
        return {
            **metadata,
            "candidateId": candidate,
            "mappedSide": mapped_side,
            **diff,
            "status": "staged",
            "exitCode": exit_code,
            "autoReconnectCount": auto_reconnect_count,
            "candidateTracePath": str(candidate_trace),
            "tracePath": str(active_trace),
            "traceSha256": sha256_file(active_trace),
            "validation": validation,
            "stagedAt": utc_now(),
            "finishedAt": utc_now(),
            "raceFinishedMonotonic": time.monotonic(),
        }
    finally:
        _remove_container(container)
        if container_slot is not None:
            container_slot.release()


def _pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def _candidate_lock_is_held(task_root: Path, candidate: str) -> bool:
    """只读探测候选任务锁是否已被其他执行器持有。

    ``run_candidates`` 必须在改动任何状态之前调用它：重复启动的第二个执行器
    过去会先把 ``state.json`` 清空、再重建候选工作区，然后才在取锁时失败，
    结果把正在跑的候选现场破坏掉。
    """
    lock_path = task_root / "monitor" / f"run-{candidate}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return False


def _workspace_mounted_by_running_container(workspace: Path) -> str:
    """返回正在挂载该工作区的容器名，没有则返回空串。"""
    target = str(workspace.resolve())
    proc = run(["docker", "ps", "--format", "{{.Names}}"], check=False, timeout=15)
    if proc.returncode != 0:
        return ""
    for name in proc.stdout.decode("utf-8", errors="replace").split():
        inspect = run(
            ["docker", "inspect", "-f", "{{range .Mounts}}{{.Source}}|{{end}}", name],
            check=False,
            timeout=15,
        )
        if inspect.returncode != 0:
            continue
        sources = inspect.stdout.decode("utf-8", errors="replace").strip().split("|")
        if any(str(Path(item).resolve()) == target for item in sources if item.strip()):
            return name
    return ""


@contextmanager
def _candidate_run_lock(task_root: Path, candidate: str):
    lock_path = task_root / "monitor" / f"run-{candidate}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SologsbError(f"{candidate} 已有执行器持有任务锁，拒绝重复启动") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _archive_stale_candidate(
    task_root: Path,
    candidate: str,
    record: dict[str, Any],
    reason: str,
) -> None:
    attempt = int(record.get("attempt") or 1)
    attempt_dir = _candidate_runtime_root(task_root, candidate) / f"attempt-{attempt:02d}"
    stdout_path = attempt_dir / "stdout.jsonl"
    stderr_path = attempt_dir / "stderr.log"
    session_id = str(record.get("sessionId") or "")
    trace = _find_candidate_trace(attempt_dir, session_id) if session_id else None
    _save_rejected(
        task_root=task_root,
        candidate=candidate,
        mapped_side=str(record.get("mappedSide") or ""),
        attempt=attempt,
        trace=trace,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        reason=reason,
    )
    _remove_container(str((record.get("container") or {}).get("name") or ""))
    source_path = _candidate_workspace(task_root, candidate)
    if source_path.exists():
        shutil.rmtree(source_path)


def _record_candidate_state(task_root: Path, candidate: str, record: dict[str, Any]) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        candidates = state.setdefault("candidates", {})
        candidates[candidate] = record
        statuses = {
            str((item or {}).get("status") or "")
            for item in candidates.values()
            if isinstance(item, dict)
        }
        if "running" in statuses:
            state["status"] = "candidates_running"
        elif state.get("status") not in {"semantic_review_required", "repo_ready", "attempt_invalid", "blocked"}:
            state["status"] = "candidates_ready"
        return state

    return mutate_state(task_root, mutate)


def _side_record_from_candidate(
    *,
    task_root: Path,
    side: str,
    candidate: str,
    result: dict[str, Any],
    completion_order: int,
) -> dict[str, Any]:
    record = dict(result)
    workspace = _candidate_workspace(task_root, candidate)
    record.update(
        {
            "side": side,
            "candidateId": candidate,
            "candidateFolder": str(workspace.relative_to(task_root)),
            "workspacePath": str(workspace),
            "completionOrder": completion_order,
            "status": "staged",
        }
    )
    return record


def _run_candidate_locked(
    task_root: Path,
    candidate: str,
    *,
    timeout: float = 7200,
    live: bool = True,
    force: bool = False,
    attempts: int = MAX_ATTEMPTS,
    mapped_side: str = "",
    stop_event: Any = None,
) -> dict[str, Any]:
    if attempts < 1:
        raise SologsbError("attempts 必须至少为 1")
    state = read_json(task_root / "monitor" / "state.json", {})
    allowed = {
        "prepared", "prompt_ready", "candidates_running", "candidates_ready",
        "semantic_review_required", "repo_ready", "running", "a_staged", "b_staged",
        "attempt_invalid", "blocked",
    }
    if state.get("status") not in allowed:
        raise SologsbError(f"当前状态 {state.get('status')} 不允许运行 {candidate}")
    existing = (state.get("candidates") or {}).get(candidate) or {}
    if existing.get("status") == "running" and not force:
        old_pid = existing.get("runPid")
        if old_pid and int(old_pid) != os.getpid() and _pid_alive(old_pid):
            raise SologsbError(f"{candidate} 已有运行中的执行进程 PID={old_pid}，拒绝重复启动")
        _archive_stale_candidate(
            task_root,
            candidate,
            existing,
            "检测到上一次运行进程已经中断；保留现场后销毁并重跑。",
        )
        existing = {"status": "invalidated", "invalidatedAt": utc_now()}
        _record_candidate_state(task_root, candidate, existing)
    if existing.get("status") == "staged" and not force:
        return existing
    if force:
        _remove_container(str((existing.get("container") or {}).get("name") or ""))
        workspace = _candidate_workspace(task_root, candidate)
        if workspace.exists():
            shutil.rmtree(workspace)
        runtime = _candidate_runtime_root(task_root, candidate)
        if runtime.exists():
            shutil.rmtree(runtime)
        existing = {"status": "invalidated", "invalidatedAt": utc_now()}
        _record_candidate_state(task_root, candidate, existing)

    last_error = ""
    for attempt in range(1, attempts + 1):
        if stop_event is not None and stop_event.is_set():
            canceled = {
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "attempt": attempt - 1,
                "status": "cancelled",
                "error": "已有两个候选先完成，未启动新的重试",
                "finishedAt": utc_now(),
            }
            _record_candidate_state(task_root, candidate, canceled)
            return canceled
        _record_candidate_state(
            task_root,
            candidate,
            {
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "status": "running",
                "attempt": attempt,
                "startedAt": utc_now(),
                "runPid": os.getpid(),
            },
        )
        current = read_json(task_root / "monitor" / "state.json", {})
        try:
            result = _run_candidate_attempt(
                task_root=task_root,
                state=current,
                candidate=candidate,
                attempt=attempt,
                timeout=timeout,
                live=live,
                mapped_side=mapped_side,
                stop_event=stop_event,
            )
        except CandidateCancelled as exc:
            result = {
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "attempt": attempt,
                "status": "cancelled",
                "error": str(exc),
                "finishedAt": utc_now(),
            }
        except Exception as exc:
            last_error = str(exc)
            result = {
                "candidateId": candidate,
                "mappedSide": mapped_side,
                "attempt": attempt,
                "status": "attempt_invalid",
                "error": last_error,
            }
        if result.get("status") == "staged":
            _record_candidate_state(task_root, candidate, result)
            write_json(_candidate_runtime_root(task_root, candidate) / "result.json", result)
            if mapped_side in SIDE_LOWER:
                side_record = _side_record_from_candidate(
                    task_root=task_root,
                    side=mapped_side,
                    candidate=candidate,
                    result=result,
                    completion_order=int(result.get("completionOrder") or 0),
                )
                _record_side_state(task_root, mapped_side, side_record)
                write_json(
                    task_root / "monitor" / "runtime" / mapped_side.lower() / "result.json",
                    side_record,
                )
                build_packet(task_root, mapped_side)
            return result
        if result.get("status") == "cancelled":
            _record_candidate_state(task_root, candidate, result)
            return result
        last_error = str(result.get("error") or last_error)
        _remove_container(str((result.get("container") or {}).get("name") or ""))
        workspace = _candidate_workspace(task_root, candidate)
        if workspace.exists():
            shutil.rmtree(workspace)
        _record_candidate_state(task_root, candidate, result)
        if attempt < attempts:
            delay = _attempt_backoff(attempt)
            if stop_event is not None:
                stop_event.wait(delay)
            elif delay:
                time.sleep(delay)

    blocked = {
        "candidateId": candidate,
        "mappedSide": mapped_side,
        "attempt": attempts,
        "status": "blocked",
        "error": f"{candidate} 连续 {attempts} 次运行均不干净: {last_error}",
        "finishedAt": utc_now(),
    }
    _record_candidate_state(task_root, candidate, blocked)
    raise SologsbError(blocked["error"])


def run_candidate(
    task_root: Path,
    candidate: str,
    *,
    timeout: float = 7200,
    live: bool = True,
    force: bool = False,
    attempts: int = MAX_ATTEMPTS,
    mapped_side: str = "",
    stop_event: Any = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"candidate-[1-9][0-9]*", candidate):
        raise SologsbError(f"非法候选目录名: {candidate}")
    mapped_side = mapped_side.upper() if mapped_side else ""
    if mapped_side and mapped_side not in SIDE_LOWER:
        raise SologsbError("mapped_side 只能是 A 或 B")
    with _candidate_run_lock(task_root, candidate):
        return _run_candidate_locked(
            task_root,
            candidate,
            timeout=timeout,
            live=live,
            force=force,
            attempts=attempts,
            mapped_side=mapped_side,
            stop_event=stop_event,
        )


def _record_side_state(task_root: Path, side: str, record: dict[str, Any]) -> dict[str, Any]:
    def mutate(state: dict[str, Any]) -> dict[str, Any]:
        sides = state.setdefault("sides", {})
        sides[side] = record
        statuses = {name: (sides.get(name) or {}).get("status") for name in SIDES}
        if any(value == "blocked" for value in statuses.values()):
            state["status"] = "blocked"
        elif statuses.get("A") in {"clean", "staged"} and statuses.get("B") in {"clean", "staged"}:
            state["status"] = "semantic_review_required"
        elif statuses.get("A") == "staged" and not statuses.get("B"):
            state["status"] = "a_staged"
        elif statuses.get("B") == "staged" and not statuses.get("A"):
            state["status"] = "b_staged"
        elif "running" in statuses.values():
            state["status"] = "running"
        return state

    return mutate_state(task_root, mutate)


def _run_side_locked(
    task_root: Path,
    side: str,
    *,
    timeout: float = 7200,
    live: bool = True,
    force: bool = False,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    side = side.upper()
    if side not in SIDE_LOWER:
        raise SologsbError("--side 只能是 A 或 B")
    state = read_json(task_root / "monitor" / "state.json", {})
    allowed = {
        "repo_ready", "running", "a_staged", "b_staged", "semantic_review_required",
        "attempt_invalid", "blocked",
    }
    if state.get("status") not in allowed:
        raise SologsbError(
            f"当前状态 {state.get('status')} 不允许续跑 {side}；"
            "首轮竞速请先运行 run --side both"
        )
    side_state = (state.get("sides") or {}).get(side) or {}
    candidate = str(side_state.get("candidateId") or (state.get("candidateMapping") or {}).get(side, {}).get("candidateId") or "")
    if not candidate:
        raise SologsbError(f"{side} 尚未绑定候选，不能续跑")
    if side_state.get("status") in {"staged", "clean"} and not force:
        return side_state
    running_side = {
        **side_state,
        "side": side,
        "candidateId": candidate,
        "status": "running",
        "startedAt": utc_now(),
        "runPid": os.getpid(),
        "error": "",
    }
    _record_side_state(task_root, side, running_side)
    try:
        result = _run_candidate_locked(
            task_root,
            candidate,
            timeout=timeout,
            live=live,
            force=force,
            attempts=attempts,
            mapped_side=side,
        )
    except Exception as exc:
        current = read_json(task_root / "monitor" / "state.json", {})
        candidate_state = ((current.get("candidates") or {}).get(candidate) or {})
        failed_side = {
            **running_side,
            **candidate_state,
            "side": side,
            "candidateId": candidate,
            "status": str(candidate_state.get("status") or "blocked"),
            "error": str(exc),
            "finishedAt": utc_now(),
        }
        _record_side_state(task_root, side, failed_side)
        raise
    if result.get("status") == "staged":
        side_record = _side_record_from_candidate(
            task_root=task_root,
            side=side,
            candidate=candidate,
            result=result,
            completion_order=int(side_state.get("completionOrder") or result.get("completionOrder") or 0),
        )
        _record_side_state(task_root, side, side_record)
        return side_record
    return result


def run_side(
    task_root: Path,
    side: str,
    *,
    timeout: float = 7200,
    live: bool = True,
    force: bool = False,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    side = side.upper()
    with _side_run_lock(task_root, side):
        return _run_side_locked(
            task_root,
            side,
            timeout=timeout,
            live=live,
            force=force,
            attempts=attempts,
        )


@contextmanager
def _side_run_lock(task_root: Path, side: str):
    lock_path = task_root / "monitor" / f"run-{side.lower()}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SologsbError(f"{side} 侧已有执行器持有任务锁，拒绝重复启动") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _race_candidate_worker(
    task_root: Path,
    candidate: str,
    *,
    timeout: float,
    live: bool,
    attempts: int,
    stop_event: threading.Event,
    completed: queue.Queue,
) -> None:
    try:
        result = run_candidate(
            task_root,
            candidate,
            timeout=timeout,
            live=live,
            attempts=attempts,
            stop_event=stop_event,
        )
        if result.get("status") != "staged":
            _record_candidate_state(task_root, candidate, result)
        finished_at = float(result.get("raceFinishedMonotonic") or time.monotonic())
        completed.put((candidate, result, finished_at))
    except Exception as exc:
        result = {
            "candidateId": candidate,
            "status": "blocked",
            "error": str(exc),
            "finishedAt": utc_now(),
        }
        _record_candidate_state(task_root, candidate, result)
        completed.put((candidate, result, time.monotonic()))


def run_candidates(
    task_root: Path,
    *,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
    timeout: float = 7200,
    live: bool = False,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    ids = candidate_ids(candidate_count)
    if attempts < 1:
        raise SologsbError("attempts 必须至少为 1")
    held = [candidate for candidate in ids if _candidate_lock_is_held(task_root, candidate)]
    if held:
        raise SologsbError(
            "已有执行器正在运行 " + "、".join(held) + "，拒绝重复启动；"
            "重复启动会清空任务状态并重建候选工作区，因此这里在任何改动之前直接退出。"
        )
    state = read_json(task_root / "monitor" / "state.json", {})
    if state.get("status") not in {"prompt_ready", "candidates_running", "blocked", "attempt_invalid"}:
        raise SologsbError(
            f"当前状态 {state.get('status')} 不允许启动候选竞速；"
            "必须在 GitHub 上传前运行"
        )
    prompt_path = Path(str(state.get("promptPath") or ""))
    if not prompt_path.is_file() or sha256_file(prompt_path) != str(state.get("promptSha256") or ""):
        raise SologsbError("提示词尚未通过固定门禁或哈希已变化")
    origin = task_root / "source" / "origin"
    if not origin.is_dir() or not any(origin.iterdir()):
        raise SologsbError(f"原始源码目录为空: {origin}")
    initial_sha = _ensure_origin_commit(origin, "chore: initial environment snapshot")
    state = read_json(task_root / "monitor" / "state.json", {})
    for record in (state.get("candidates") or {}).values():
        if isinstance(record, dict):
            _remove_container(str((record.get("container") or {}).get("name") or ""))
    state.update(
        {
            "status": "candidates_running",
            "initialSnapshot": initial_sha,
            "candidateCount": candidate_count,
            "candidateIds": list(ids),
            "candidates": {},
            "candidateMapping": {},
            "sides": {},
            "candidateRaceStartedAt": utc_now(),
        }
    )
    save_state(task_root, state)
    # Satisfy the fixed order strictly: materialize every isolated candidate
    # workspace first, then start any model container.
    try:
        for candidate in ids:
            _clone_candidate(task_root, state, candidate)
        _ensure_image(DEFAULT_IMAGE)
    except Exception as exc:
        failed = read_json(task_root / "monitor" / "state.json", {})
        failed["status"] = "blocked"
        failed["candidateRaceError"] = f"候选预拉取或镜像准备失败: {exc}"
        save_state(task_root, failed)
        raise

    stop_event = threading.Event()
    completed: queue.Queue[tuple[str, dict[str, Any], float]] = queue.Queue()
    errors: dict[str, str] = {}
    winners: list[tuple[float, str, dict[str, Any]]] = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=candidate_count, thread_name_prefix="sologsb-candidate")
    futures: dict[str, concurrent.futures.Future[Any]] = {}
    try:
        for candidate in ids:
            futures[candidate] = pool.submit(
                _race_candidate_worker,
                task_root,
                candidate,
                timeout=timeout,
                live=live,
                attempts=attempts,
                stop_event=stop_event,
                completed=completed,
            )
        remaining = set(ids)
        # Each worker only reports after all of its attempts, and may first
        # queue for a container slot, so the race budget is the worst case of
        # one worker, not a single attempt's timeout.
        _limit, _excluded, slot_wait = _CONTAINER_LIMITER._settings()
        race_budget = attempts * (float(timeout) + ATTEMPT_BACKOFF_MAX_SECONDS) + slot_wait
        race_deadline = time.monotonic() + race_budget
        while remaining and len(winners) < 2:
            try:
                candidate, result, finished_at = completed.get(
                    timeout=max(1.0, race_deadline - time.monotonic())
                )
            except queue.Empty as exc:
                raise SologsbError(f"候选竞速等待结果超时（总预算 {int(race_budget)} 秒）") from exc
            remaining.discard(candidate)
            if result.get("status") == "staged":
                winners.append((finished_at, candidate, result))
                if len(winners) == 2:
                    stop_event.set()
            else:
                errors[candidate] = str(result.get("error") or result.get("status") or "候选失败")
        if len(winners) < 2:
            stop_event.set()
            for future in futures.values():
                future.cancel()
            pool.shutdown(wait=True)
            failed = read_json(task_root / "monitor" / "state.json", {})
            failed["status"] = "blocked"
            failed["candidateRaceError"] = (
                f"候选竞速必须至少产生 2 个干净结果，当前仅 {len(winners)} 个"
            )
            failed["candidateErrors"] = errors
            save_state(task_root, failed)
            raise SologsbError(failed["candidateRaceError"] + (f": {errors}" if errors else ""))
    finally:
        # Any exceptional exit must stop sibling candidates too; never wait for
        # another model to finish after this orchestrator has failed.
        stop_event.set()
        for future in futures.values():
            future.cancel()
        pool.shutdown(wait=True)

    winners.sort(key=lambda item: (item[0], item[1]))
    state = read_json(task_root / "monitor" / "state.json", {})
    candidates = state.setdefault("candidates", {})
    sides: dict[str, dict[str, Any]] = {}
    mapping: dict[str, dict[str, Any]] = {}
    for order, (finished_at, candidate, result) in enumerate(winners[:2], 1):
        side = "A" if order == 1 else "B"
        side_record = _side_record_from_candidate(
            task_root=task_root,
            side=side,
            candidate=candidate,
            result=result,
            completion_order=order,
        )
        trace = Path(str(result.get("candidateTracePath") or result.get("tracePath") or ""))
        if not trace.is_file():
            raise SologsbError(f"{candidate} 完成但候选轨迹不存在: {trace}")
        side_trace = task_root / "workspace" / "轨迹文件" / side.lower() / trace.name
        atomic_copy(trace, side_trace)
        side_record.update(
            {
                "tracePath": str(side_trace),
                "traceSha256": sha256_file(side_trace),
                "completionOrder": order,
                "finishedAt": utc_now(),
            }
        )
        sides[side] = side_record
        mapping[side] = {
            "candidateId": candidate,
            "candidateFolder": side_record["candidateFolder"],
            "workspacePath": side_record["workspacePath"],
            "completionOrder": order,
            "finishedAt": side_record["finishedAt"],
        }
        candidate_record = dict(candidates.get(candidate) or result)
        candidate_record.update(
            {
                "status": "staged",
                "mappedSide": side,
                "completionOrder": order,
                "finishedAt": mapping[side]["finishedAt"],
            }
        )
        candidates[candidate] = candidate_record
        write_json(_candidate_runtime_root(task_root, candidate) / "result.json", candidate_record)
        write_json(task_root / "monitor" / "runtime" / side.lower() / "result.json", side_record)

    state["candidates"] = candidates
    state["sides"] = sides
    state["candidateMapping"] = mapping
    state["candidateWinners"] = [mapping["A"]["candidateId"], mapping["B"]["candidateId"]]
    state["candidateRaceFinishedAt"] = utc_now()
    state["status"] = "semantic_review_required"
    state["semanticPackets"] = {}
    save_state(task_root, state)
    packets = ensure_packets(task_root)
    state = read_json(task_root / "monitor" / "state.json", {})
    state["semanticPackets"] = packets
    save_state(task_root, state)
    return {
        "status": "semantic_review_required",
        "executionMode": "candidate-race-live" if live else "candidate-race-headless",
        "candidateCount": candidate_count,
        "candidateMapping": mapping,
        "sides": sides,
        "packets": packets,
        "cancelledCandidates": [
            candidate
            for candidate, record in candidates.items()
            if str((record or {}).get("status") or "") == "cancelled"
        ],
        "errors": errors,
    }


def run_both(
    task_root: Path,
    *,
    timeout: float = 7200,
    live: bool = False,
    candidate_count: int = DEFAULT_CANDIDATE_COUNT,
    attempts: int = MAX_ATTEMPTS,
) -> dict[str, Any]:
    return run_candidates(
        task_root,
        candidate_count=candidate_count,
        timeout=timeout,
        live=live,
        attempts=attempts,
    )

def publish_sides(
    task_root: Path,
    *,
    semantic_a: Path,
    semantic_b: Path,
) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    sides = state.get("sides") or {}
    if (sides.get("A") or {}).get("status") not in {"staged", "clean"} or (sides.get("B") or {}).get("status") not in {"staged", "clean"}:
        raise SologsbError("A/B 都必须完成结构校验后才能发布")
    validate_review(task_root, "A", semantic_a.expanduser().resolve())
    validate_review(task_root, "B", semantic_b.expanduser().resolve())
    published = _atomic_publish(task_root, state, sides)
    for side in SIDES:
        record = sides[side]
        record.update(published[side])
        record["status"] = "clean"
        record["artifactSnapshotUrl"] = commit_url(str(state["repoUrl"]), record["artifactSnapshot"])
        record["publishedAt"] = utc_now()
        write_json(task_root / "monitor" / "runtime" / side.lower() / "result.json", record)
    state["sides"] = sides
    state["status"] = "ab_clean"
    state["publishedAt"] = utc_now()
    state["remoteHeads"] = published["remoteHeads"]
    line_gate = {
        "id": "change-volume-line-gate",
        "ok": all(bool((sides.get(side) or {}).get("lineGate", {}).get("hardOk")) for side in SIDES),
        "failedSides": [
            side for side in SIDES
            if not bool((sides.get(side) or {}).get("lineGate", {}).get("hardOk"))
        ],
        "finalBlockingStage": "submit_preflight",
        "checkedAt": utc_now(),
        "sides": {side: (sides.get(side) or {}).get("lineGate") or {} for side in SIDES},
    }
    state["changeVolumeLineGate"] = line_gate
    write_json(task_root / "monitor" / "change-volume-line-gate.json", line_gate)
    save_state(task_root, state)
    return {
        "status": "ab_clean",
        "A": sides["A"],
        "B": sides["B"],
        "remoteHeads": published["remoteHeads"],
        "changeVolumeLineGate": line_gate,
    }
