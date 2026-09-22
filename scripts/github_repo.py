#!/usr/bin/env python3
"""Create the task GitHub repository and the mandatory main/A/B topology."""
from __future__ import annotations

import base64
import os
import random
import re
import shutil
import string
from pathlib import Path
from typing import Any

from common import (
    SologsbError,
    command_exists,
    commit_url,
    github_env,
    load_git_identity,
    read_json,
    run,
    save_state,
    safe_slug,
)


def _random_code(length: int = 4) -> str:
    if length < 3 or length > 6:
        raise SologsbError("仓库唯一后缀长度必须是 3 到 6 位")
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def _project_code(task_root: Path, state: dict[str, Any]) -> str:
    candidates = [state.get("projectCode")]
    source = state.get("source") if isinstance(state.get("source"), dict) else {}
    candidates.append(source.get("projectCode"))
    for path in (task_root / "monitor" / "platform-selection.json", task_root / "monitor" / "source.json"):
        document = read_json(path, {})
        if not isinstance(document, dict):
            continue
        candidates.append(document.get("projectCode"))
        selection = document.get("selection") if isinstance(document.get("selection"), dict) else {}
        candidates.append(selection.get("projectCode"))
    for value in candidates:
        code = safe_slug(str(value or "").strip()).lower()
        if code and code != "task":
            return code
    return ""


def _normalize_repo_prefix(raw_base: str, project_code: str = "") -> str:
    base = safe_slug(raw_base).lower()
    project = safe_slug(project_code).lower() if project_code else ""
    if project and base.lower() != project.lower() and not base.lower().startswith(project.lower() + "-"):
        base = f"{project}-{base}"
    if project:
        return project
    return re.sub(r"-[a-z0-9]{3,6}$", "", base.lower()).strip("-._") or "task"


def _gh_available() -> None:
    if not command_exists("gh"):
        raise SologsbError("缺少 gh CLI")
    proc = run(["gh", "auth", "status"], check=False, env=github_env(require_proxy=True))
    if proc.returncode != 0:
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "gh 未登录")


def _repo_exists(full_name: str) -> bool:
    proc = run(["gh", "repo", "view", full_name], check=False, env=github_env(require_proxy=True))
    return proc.returncode == 0


def _git(
    origin: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
    network: bool = False,
):
    return run(
        ["git", "-C", str(origin), *args],
        check=check,
        env=env if env is not None else github_env(require_proxy=network),
    )


def _remote_heads(origin: Path) -> dict[str, str]:
    proc = _git(origin, "ls-remote", "--heads", "origin", network=True)
    heads: dict[str, str] = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1].removeprefix("refs/heads/")] = parts[0]
    return heads


def _derive_repo_base(origin: Path, state: dict[str, Any], project_code: str = "") -> str:
    explicit = str(state.get("repoBaseName") or "").strip()
    if explicit:
        return _normalize_repo_prefix(explicit, project_code)
    if project_code:
        return safe_slug(project_code)
    package_path = origin / "package.json"
    if package_path.is_file():
        try:
            name = str(read_json(package_path, {}).get("name") or "").split("/")[-1]
            if name:
                return safe_slug(name)
        except Exception:
            pass
    go_mod = origin / "go.mod"
    if go_mod.is_file():
        first = go_mod.read_text(encoding="utf-8", errors="replace").splitlines()[0:1]
        if first and first[0].startswith("module "):
            return safe_slug(first[0].split()[-1].rstrip("/").split("/")[-1])
    cargo = origin / "Cargo.toml"
    if cargo.is_file():
        match = re.search(r"^name\s*=\s*\"([^\"]+)\"", cargo.read_text(encoding="utf-8", errors="replace"), re.M)
        if match:
            return safe_slug(match.group(1))
    return safe_slug(str(state.get("taskName") or origin.parent.parent.name))


def _ensure_origin_commit(origin: Path, message: str) -> str:
    identity = load_git_identity()
    if not (origin / ".git").is_dir():
        run(["git", "init", "-b", "main"], cwd=origin)
    _git(origin, "config", "user.name", identity["name"])
    _git(origin, "config", "user.email", identity["email"])
    head = _git(origin, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode != 0:
        _git(origin, "add", "-A")
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": identity["name"],
                "GIT_AUTHOR_EMAIL": identity["email"],
                "GIT_COMMITTER_NAME": identity["name"],
                "GIT_COMMITTER_EMAIL": identity["email"],
            }
        )
        _git(origin, "commit", "-m", message, env=env)
    branch = _git(origin, "branch", "--show-current").stdout.decode().strip()
    if branch != "main":
        raise SologsbError(f"初始分支必须是 main，当前为 {branch or 'detached'}")
    return _git(origin, "rev-parse", "HEAD").stdout.decode().strip()


def _github_git_env(owner: str, token: str) -> dict[str, str]:
    env = github_env(require_proxy=True)
    auth = base64.b64encode(f"{owner}:{token}".encode()).decode()
    env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {auth}",
        }
    )
    return env


def _push_topology(
    origin: Path,
    remote_url: str,
    initial_sha: str,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    remotes = _git(origin, "remote").stdout.decode().splitlines()
    if "origin" in remotes:
        _git(origin, "remote", "set-url", "origin", remote_url)
    else:
        _git(origin, "remote", "add", "origin", remote_url)
    _git(origin, "push", "-u", "origin", "main", env=env)
    _git(origin, "checkout", "-B", "A", initial_sha)
    _git(origin, "push", "-u", "origin", "A:A", env=env)
    _git(origin, "checkout", "-B", "B", initial_sha)
    _git(origin, "push", "-u", "origin", "B:B", env=env)
    _git(origin, "checkout", "main")
    heads = _remote_heads(origin)
    if set(heads) != {"main", "A", "B"}:
        raise SologsbError(f"远端分支必须且只能是 main/A/B，当前为 {sorted(heads)}")
    if heads["A"] != initial_sha or heads["B"] != initial_sha:
        raise SologsbError("A/B 起始提交与初始快照不一致")
    return heads


def _clone_branch(remote_url: str, branch: str, destination: Path, expected_sha: str) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "clone", "--branch", branch, "--single-branch", remote_url, str(destination)],
        env=github_env(require_proxy=True),
    )
    sha = _git(destination, "rev-parse", "HEAD").stdout.decode().strip()
    if sha != expected_sha:
        raise SologsbError(f"{branch} 分支起始提交不是初始快照: {sha} != {expected_sha}")


def init_github_repo(
    task_root: Path,
    *,
    repo_name: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    allowed = {
        "candidates_ready", "semantic_review_required", "a_staged", "b_staged",
    }
    if state.get("status") not in allowed:
        raise SologsbError(
            "必须先完成候选竞速并映射 A/B，再创建 GitHub 仓库；"
            "候选完成前禁止上传源码")
    mapping = state.get("candidateMapping") if isinstance(state.get("candidateMapping"), dict) else {}
    for side in ("A", "B"):
        item = mapping.get(side) if isinstance(mapping.get(side), dict) else {}
        if not item.get("candidateId"):
            raise SologsbError(f"候选映射缺少 {side}，禁止创建 GitHub 仓库")
    _gh_available()
    origin = task_root / "source" / "origin"
    if not origin.is_dir() or not any(origin.iterdir()):
        raise SologsbError(f"原始源码目录为空: {origin}")

    project_code = _project_code(task_root, state)
    selection_doc = read_json(task_root / "monitor" / "platform-selection.json", {})
    if selection_doc and not project_code:
        raise SologsbError("平台项目缺少 projectCode，禁止创建缺少项目标识的 GitHub 仓库")
    raw_base = repo_name.strip() or _derive_repo_base(origin, state, project_code)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", raw_base):
        raise SologsbError("仓库名只允许字母、数字、点、下划线和连字符")
    prefix = _normalize_repo_prefix(raw_base, project_code)
    if project_code and not prefix.startswith(project_code):
        raise SologsbError(f"GitHub 仓库名必须以项目标识 {project_code} 开头")
    user_proc = run(["gh", "api", "user", "--jq", ".login"], env=github_env(require_proxy=True))
    owner = user_proc.stdout.decode().strip()
    if not owner:
        raise SologsbError("无法获取 GitHub 用户名")
    full_name = ""
    base = ""
    for _ in range(5):
        base = f"{prefix}-{_random_code()}"
        if project_code and not re.fullmatch(rf"{re.escape(project_code)}-[a-z0-9]{{3,6}}", base):
            raise SologsbError(f"GitHub 仓库名必须以 {project_code} 加 3–6 位唯一后缀组成: {base}")
        candidate = f"{owner}/{base}"
        if not _repo_exists(candidate):
            full_name = candidate
            break
    if not full_name:
        raise SologsbError("连续 5 次生成的 GitHub 仓库名均已存在")

    initial_sha = _ensure_origin_commit(origin, "chore: initial environment snapshot")
    repo_url = f"https://github.com/{full_name}"
    remote_url = repo_url + ".git"
    if dry_run:
        return {
            "owner": owner,
            "repoName": base,
            "repoUrl": repo_url,
            "remoteUrl": remote_url,
            "initialSnapshot": initial_sha,
            "dryRun": True,
        }

    env = github_env(require_proxy=True)
    create = run(
        [
            "gh",
            "repo",
            "create",
            full_name,
            "--public",
            "--description",
            "Pair-wise GSB evaluation snapshot",
        ],
        env=env,
    )
    if create.returncode != 0:
        raise SologsbError(create.stderr.decode("utf-8", errors="replace") or "创建 GitHub 仓库失败")
    token_proc = run(["gh", "auth", "token"], env=github_env(require_proxy=True))
    token = token_proc.stdout.decode().strip()
    if not token:
        raise SologsbError("gh auth token 为空")
    try:
        heads = _push_topology(
            origin,
            remote_url,
            initial_sha,
            env=_github_git_env(owner, token),
        )
    except Exception as exc:
        cleanup = run(
            ["gh", "repo", "delete", full_name, "--yes"],
            check=False,
            env=github_env(require_proxy=True),
        )
        cleanup_note = "已清理空仓库" if cleanup.returncode == 0 else "空仓库清理失败，需要人工删除" + full_name
        raise SologsbError(f"{exc}; {cleanup_note}") from exc

    result = {
        "owner": owner,
        "repoName": base,
        "repoUrl": repo_url,
        "remoteUrl": remote_url,
        "initialSnapshot": initial_sha,
        "initialSnapshotUrl": commit_url(repo_url, initial_sha),
        "branches": heads,
        "createdAt": read_json(task_root / "monitor" / "state.json", {}).get("createdAt"),
    }
    state.update({"status": "repo_ready", **result})
    state["repoCreatedAfterCandidates"] = True
    save_state(task_root, state)
    return result
