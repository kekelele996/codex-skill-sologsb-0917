#!/usr/bin/env python3
"""Shared deterministic helpers for sologsb-0917."""
from __future__ import annotations

import hashlib
import fcntl
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

HOME = Path.home()
SKILL_ROOT = Path(__file__).resolve().parents[1]
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
SOLO_DIR = CODEX_HOME / "skills" / "solo-annotation-loop"
SOLO_SCRIPTS = SOLO_DIR / "scripts"
AUTO_DIR = CODEX_HOME / "skills" / "solo2-auto"
AUTO_RUNNER = AUTO_DIR / "scripts" / "auto_runner.py"
RECORDER_DIR = CODEX_HOME / "skills" / "desktop-demo-recorder"
SCHEMA_FALLBACK = SKILL_ROOT / "references" / "gsb-form-schema.json"
VERSION_FILE = SKILL_ROOT / "VERSION"
STATE_ORDER = [
    "prepared",
    "prompt_ready",
    "candidates_running",
    "semantic_review_required",
    "repo_ready",
    "a_clean",
    "b_clean",
    "verified",
    "gsb_ready",
    "recorded",
    "complete",
]
TASK_TYPES = {"0-1代码生成", "feature迭代", "Bug修复", "代码重构", "工程化", "代码测试"}
ALL_DISPLAY_TASK_TYPES = TASK_TYPES | {"代码理解"}
DIFFICULTIES = {"困难", "地狱"}
SIDES = ("A", "B")
SIDE_LOWER = {"A": "a", "B": "b"}
DEFAULT_RECORDING_LOCK_TIMEOUT = 7200.0
RECORDING_LOCK_ENV = "SOLOGBS_0917_RECORDING_LOCK"


class SologsbError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def skill_version_info() -> dict[str, str]:
    """读取技能根目录的 VERSION 文件，它是全局版本号的唯一来源。"""
    info: dict[str, str] = {}
    try:
        text = VERSION_FILE.read_text(encoding="utf-8")
    except OSError:
        return info
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        info[key.strip()] = value.strip()
    return info


def skill_version() -> str:
    """统一全局版本号，例如 1.0.0。"""
    return skill_version_info().get("version", "unknown")


def skill_release_tag() -> str:
    """与全局版本号对应的发布标签，例如 v1.0.0。"""
    return skill_version_info().get("release_tag", "")

DEFAULT_GITHUB_PROXY_HOST = "127.0.0.1"
DEFAULT_GITHUB_PROXY_PORT = 7897


def _proxy_port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def normalize_github_proxy(value: str, *, default_scheme: str = "http") -> str:
    """Normalize bare ports and host:port values into a proxy URL."""
    text = value.strip()
    if not text:
        return ""
    if "://" not in text:
        if text.isdigit():
            text = f"{DEFAULT_GITHUB_PROXY_HOST}:{text}"
        text = f"{default_scheme}://{text}"
    return text


def github_proxy_url(*, required: bool = False) -> str:
    """Return the Clash Verge mixed-port proxy used for GitHub traffic.

    An explicit ``SOLOSB_GITHUB_PROXY``/``GITHUB_PROXY`` wins. Otherwise the
    local Clash Verge mixed listener at 127.0.0.1:7897 is used. With
    ``required=True``, a missing listener fails closed instead of allowing a
    direct GitHub connection.
    """
    explicit = (
        os.environ.get("SOLOSB_GITHUB_PROXY", "").strip()
        or os.environ.get("GITHUB_PROXY", "").strip()
    )
    if explicit:
        return normalize_github_proxy(explicit)
    if _proxy_port_open(DEFAULT_GITHUB_PROXY_HOST, DEFAULT_GITHUB_PROXY_PORT):
        return f"http://{DEFAULT_GITHUB_PROXY_HOST}:{DEFAULT_GITHUB_PROXY_PORT}"
    if required:
        raise SologsbError(
            "Clash Verge GitHub 代理不可用：请设置 SOLOSB_GITHUB_PROXY，"
            f"或启动混合代理 {DEFAULT_GITHUB_PROXY_HOST}:{DEFAULT_GITHUB_PROXY_PORT}"
        )
    return ""


def github_env(extra: dict[str, str] | None = None, *, require_proxy: bool = False) -> dict[str, str]:
    """Build an environment with the Clash Verge proxy applied to GitHub traffic."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    proxy = github_proxy_url(required=require_proxy)
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        no_proxy = env.get("NO_PROXY", "")
        required = ["127.0.0.1", "localhost", "::1"]
        merged = [item for item in no_proxy.split(",") if item.strip()]
        for item in required:
            if item not in merged:
                merged.append(item)
        env["NO_PROXY"] = ",".join(merged)
        env["no_proxy"] = env["NO_PROXY"]
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env



def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "task"


def safe_local_name(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", value.strip()).strip(" .-")
    return cleaned or "task"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copy2(src, temp_name)
        os.replace(temp_name, dst)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        stdout = proc.stdout.decode("utf-8", errors="replace")
        raise SologsbError(f"命令失败({proc.returncode}): {' '.join(cmd)}\n{stderr or stdout}")
    return proc


def run_text(cmd: list[str], **kwargs: Any) -> tuple[int, str, str]:
    kwargs.pop("check", None)
    proc = run(cmd, check=False, **kwargs)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def parse_last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index in range(len(text)):
        if text[index] != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise SologsbError("命令输出中没有 JSON 对象")


def task_root_from_arg(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    if not (root / "monitor" / "state.json").is_file():
        raise SologsbError(f"不是有效任务目录: {root}")
    return root


@contextlib.contextmanager
def task_state_lock(task_root: Path, timeout: float = 60.0):
    lock_path = task_root / "monitor" / ".state.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise SologsbError(f"等待任务状态锁超时: {lock_path}")
            time.sleep(0.1)
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def recording_lock_path() -> Path:
    """Return the host-global recording mutex path.

    The lock deliberately lives outside every task root so concurrent tasks cannot
    bypass it simply by using different work directories.
    """
    configured = os.environ.get(RECORDING_LOCK_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return CODEX_HOME / "run" / "sologsb-0917" / "recording.lock"


def recording_project_identity(task_root: Path) -> dict[str, str]:
    """Build stable metadata for the project that owns the recording lock."""
    state = read_json(task_root / "monitor" / "state.json", {}) or {}
    source = state.get("source") if isinstance(state.get("source"), dict) else {}
    selection_doc = read_json(task_root / "monitor" / "platform-selection.json", {}) or {}
    selection = selection_doc.get("selection") if isinstance(selection_doc, dict) else {}
    selection = selection if isinstance(selection, dict) else {}
    source = source if isinstance(source, dict) else {}
    project_code = str(source.get("projectCode") or selection.get("projectCode") or "").strip()
    project_name = str(source.get("projectName") or selection.get("projectName") or "").strip()
    project_id = str(source.get("projectId") or "").strip()
    source_identity = str(
        project_code
        or project_id
        or source.get("sourcePath")
        or source.get("packagePath")
        or state.get("repoUrl")
        or task_root
    ).strip()
    label_parts = [part for part in (project_code, project_name) if part]
    return {
        "key": sha256_text(source_identity.casefold())[:24],
        "code": project_code,
        "name": project_name,
        "label": " / ".join(label_parts) or project_name or project_code or task_root.name,
        "taskRoot": str(task_root.resolve()),
    }


def _lock_holder_from_handle(handle) -> dict[str, Any]:
    try:
        handle.seek(0)
        raw = handle.read().strip()
        value = json.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@contextlib.contextmanager
def global_recording_lock(
    task_root: Path,
    *,
    side: str,
    timeout: float = DEFAULT_RECORDING_LOCK_TIMEOUT,
):
    """Serialize real recording work across every task on this host.

    The mutex is global, while ownership metadata is project-scoped. The kernel
    releases the lock automatically if the recording process exits or crashes.
    """
    if timeout is not None and timeout < 0:
        raise SologsbError("录制锁等待时间不能为负数")
    lock_path = recording_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    project = recording_project_identity(task_root)
    handle = lock_path.open("a+", encoding="utf-8")
    started = time.monotonic()
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= timeout:
                holder = _lock_holder_from_handle(handle)
                owner = str(holder.get("projectLabel") or holder.get("projectKey") or "未知项目")
                holder_side = str(holder.get("side") or "未知侧")
                holder_pid = str(holder.get("pid") or "未知PID")
                acquired_at = str(holder.get("acquiredAt") or "未知时间")
                handle.close()
                raise SologsbError(
                    "等待全局录制锁超时"
                    f"（{timeout:g}s）：{owner} 正在录制 {holder_side} 侧"
                    f"（PID {holder_pid}，获取于 {acquired_at}）；锁文件 {lock_path}"
                )
            time.sleep(min(0.5, max(0.05, (timeout - elapsed) if timeout is not None else 0.5)))

    wait_seconds = time.monotonic() - started
    metadata: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "acquired",
        "lockPath": str(lock_path),
        "projectKey": project["key"],
        "projectLabel": project["label"],
        "projectCode": project["code"],
        "projectName": project["name"],
        "taskRoot": project["taskRoot"],
        "side": side.upper(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquiredAt": utc_now(),
        "waitSeconds": round(wait_seconds, 3),
    }
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield metadata
    finally:
        try:
            released = {
                **metadata,
                "status": "released",
                "releasedAt": utc_now(),
            }
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(released, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def mutate_state(task_root: Path, mutator) -> dict[str, Any]:
    with task_state_lock(task_root):
        state = load_state(task_root)
        result = mutator(state)
        if isinstance(result, dict):
            state = result
        save_state(task_root, state)
        return state


def load_state(task_root: Path) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    if not isinstance(state, dict):
        raise SologsbError("任务状态损坏")
    return state


def save_state(task_root: Path, state: dict[str, Any]) -> None:
    state["updatedAt"] = utc_now()
    write_json(task_root / "monitor" / "state.json", state)


def set_status(task_root: Path, status: str, **extra: Any) -> dict[str, Any]:
    state = load_state(task_root)
    state["status"] = status
    state.update(extra)
    save_state(task_root, state)
    return state


def require_status(task_root: Path, accepted: Iterable[str]) -> dict[str, Any]:
    state = load_state(task_root)
    if state.get("status") not in set(accepted):
        raise SologsbError(
            f"当前状态为 {state.get('status')}，要求状态之一: {', '.join(sorted(accepted))}"
        )
    return state


def ensure_task_dirs(task_root: Path) -> None:
    paths = [
        task_root / "source" / "origin",
        # Candidate worktrees are intentionally never renamed after a race.
        task_root / "source" / "candidates",
        task_root / "source" / "a",
        task_root / "source" / "b",
        task_root / "monitor" / "runtime" / "candidates",
        task_root / "monitor" / "runtime" / "a",
        task_root / "monitor" / "runtime" / "b",
        task_root / "workspace" / "评审文件",
        task_root / "workspace" / "轨迹文件" / "candidates",
        task_root / "workspace" / "轨迹文件" / "a" / "rejected",
        task_root / "workspace" / "轨迹文件" / "b" / "rejected",
        task_root / "workspace" / "视频信息" / "a" / "脚本",
        task_root / "workspace" / "视频信息" / "a" / "视频",
        task_root / "workspace" / "视频信息" / "b" / "脚本",
        task_root / "workspace" / "视频信息" / "b" / "视频",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def copy_tree(src: Path, dst: Path, *, overwrite: bool = False) -> int:
    if not src.is_dir():
        raise SologsbError(f"源码目录不存在: {src}")
    if dst.exists() and any(dst.iterdir()) and not overwrite:
        raise SologsbError(f"目标目录不是空的: {dst}")
    if overwrite and dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    ignored_names = {".git", ".DS_Store", "node_modules", "__pycache__", ".venv", "venv"}
    for source in src.rglob("*"):
        if any(part in ignored_names for part in source.relative_to(src).parts):
            continue
        target = dst / source.relative_to(src)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
    return count


def extract_zip_safe(zip_path: Path, target: Path) -> None:
    import zipfile

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise SologsbError(f"ZIP 包含非法路径: {name}")
            total += info.file_size
            if total > 2 * 1024 * 1024 * 1024:
                raise SologsbError("ZIP 解压后大小超过 2GB")
            destination = (target / name).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise SologsbError(f"ZIP 路径越界: {name}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)


def find_zip_source_root(extracted: Path) -> Path:
    candidates = [extracted / "source", extracted]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if (candidate / ".git").exists() or any(candidate.iterdir()):
            return candidate
    raise SologsbError("任务包中没有可用源码")


def run_with_retry(cmd: list[str], attempts: int = 3, delay: float = 0.4) -> subprocess.CompletedProcess[bytes]:
    last: subprocess.CompletedProcess[bytes] | None = None
    for index in range(attempts):
        last = run(cmd, check=False)
        if last.returncode == 0:
            return last
        if index + 1 < attempts:
            time.sleep(delay)
    assert last is not None
    raise SologsbError(last.stderr.decode("utf-8", errors="replace") or "命令失败")


def git_output(repo: Path, *args: str, check: bool = True) -> str:
    proc = run(["git", "-C", str(repo), *args], check=check)
    return proc.stdout.decode("utf-8", errors="replace").strip()


def ensure_git_repo(repo: Path) -> None:
    if not (repo / ".git").is_dir():
        raise SologsbError(f"不是 Git 仓库: {repo}")


def commit_url(repo_url: str, sha: str) -> str:
    return repo_url.rstrip("/") + "/commit/" + sha


def normalize_repo_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value.endswith(".git"):
        value = value[:-4]
    return value


def load_git_identity() -> dict[str, str]:
    """解析提交产物要用的 Git 作者身份。

    技能包里不内置任何个人姓名或邮箱：先读全局 git 配置，再读环境变量，
    两者都没有就报错提示补齐。
    """
    name_proc = run(["git", "config", "--global", "user.name"], check=False)
    email_proc = run(["git", "config", "--global", "user.email"], check=False)
    name = name_proc.stdout.decode("utf-8", errors="replace").strip()
    email = email_proc.stdout.decode("utf-8", errors="replace").strip()
    name = name or os.environ.get("SOLOSB_GIT_AUTHOR_NAME", "").strip()
    email = email or os.environ.get("SOLOSB_GIT_AUTHOR_EMAIL", "").strip()
    if not name or not email:
        raise SologsbError(
            "缺少 Git 提交身份：请设置 git config --global user.name / user.email，"
            "或设置 SOLOSB_GIT_AUTHOR_NAME / SOLOSB_GIT_AUTHOR_EMAIL"
        )
    return {"name": name, "email": email}


def text_non_whitespace_len(value: str) -> int:
    return len(re.sub(r"\s+", "", value))


# 锁文件与依赖清单的配对：模型改了清单，同目录（或其子目录下同类清单）的锁文件要一起发布，
# 否则从 GitHub 干净检出后 `npm ci`、`go build` 等会因锁文件缺失或不同步失败，错算到模型头上。
LOCKFILE_MANIFESTS: dict[str, tuple[str, ...]] = {
    "package-lock.json": ("package.json",),
    "npm-shrinkwrap.json": ("package.json",),
    "pnpm-lock.yaml": ("package.json", "pnpm-workspace.yaml"),
    "yarn.lock": ("package.json",),
    "bun.lockb": ("package.json",),
    "bun.lock": ("package.json",),
    "go.sum": ("go.mod",),
    "go.work.sum": ("go.work", "go.mod"),
    "Cargo.lock": ("Cargo.toml",),
    "poetry.lock": ("pyproject.toml",),
    "uv.lock": ("pyproject.toml",),
    "pdm.lock": ("pyproject.toml",),
    "Pipfile.lock": ("Pipfile",),
    "composer.lock": ("composer.json",),
    "Gemfile.lock": ("Gemfile",),
}
LOCKFILE_NAMES = frozenset(LOCKFILE_MANIFESTS)
# go.sum 只记录校验值，不决定依赖版本（版本由 go.mod 决定）。初始快照的 go.sum 不全时，
# 模型补上的校验记录不发布，干净检出后 go build 会报 missing go.sum entry，所以改了就发布。
ALWAYS_PUBLISHED_LOCKFILES = frozenset({"go.sum", "go.work.sum"})


def is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name in LOCKFILE_NAMES


def paired_lockfiles(changed: list[str] | set[str]) -> set[str]:
    """Return the lockfiles in ``changed`` whose manifest also changed.

    清单与锁文件在同一目录，或锁文件在工作区根目录、清单在其子目录（pnpm/yarn/Cargo workspace）时视为配对。
    go.sum、go.work.sum 改了就发布，不要求 go.mod 同时改。
    """
    paths = [PurePosixPath(str(item)) for item in changed]
    manifests = [path for path in paths if path.name not in LOCKFILE_NAMES]
    allowed: set[str] = set()
    for lock in paths:
        if lock.name in ALWAYS_PUBLISHED_LOCKFILES:
            allowed.add(str(lock))
            continue
        names = LOCKFILE_MANIFESTS.get(lock.name)
        if not names:
            continue
        root = lock.parent
        for manifest in manifests:
            if manifest.name not in names:
                continue
            if manifest.parent == root or root in manifest.parents or str(root) == ".":
                allowed.add(str(lock))
                break
    return allowed
