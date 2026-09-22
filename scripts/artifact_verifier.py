#!/usr/bin/env python3
"""Real artifact verification in isolated clones."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

from common import (
    SologsbError,
    github_env,
    read_json,
    run,
    save_state,
    sha256_text,
    utc_now,
    write_json,
)


def suggest_plan(repo: Path) -> dict[str, list[dict[str, Any]]]:
    common: list[dict[str, Any]] = []
    if (repo / "go.mod").is_file():
        common.extend(
            [
                {"name": "go-build", "command": "go build ./...", "timeout": 600, "expectedExit": 0},
                {"name": "go-test", "command": "go test ./...", "timeout": 600, "expectedExit": 0},
            ]
        )
    if (repo / "package.json").is_file():
        package = read_json(repo / "package.json", {})
        scripts = package.get("scripts") or {}
        if (repo / "pnpm-lock.yaml").is_file():
            # 兼容 pnpm 11 的无 TTY 模块重建和依赖构建脚本批准。首个安装失败不会丢日志，
            # fallback 批准后必须再次安装；最终安装仍失败时该检查会以非零退出。
            common.append({
                "name": "pnpm-install",
                "command": "CI=true pnpm install --frozen-lockfile || "
                           "(pnpm approve-builds --all && CI=true pnpm install --frozen-lockfile)",
                "timeout": 900,
                "expectedExit": 0,
            })
            if "build" in scripts:
                common.append({"name": "pnpm-build", "command": "pnpm build", "timeout": 900, "expectedExit": 0})
            if "test" in scripts:
                common.append({"name": "pnpm-test", "command": "pnpm test", "timeout": 900, "expectedExit": 0})
        else:
            if (repo / "package-lock.json").is_file():
                common.append({"name": "npm-install", "command": "CI=true npm ci", "timeout": 900, "expectedExit": 0})
            if "build" in scripts:
                common.append({"name": "npm-build", "command": "npm run build", "timeout": 900, "expectedExit": 0})
            if "test" in scripts:
                common.append({"name": "npm-test", "command": "npm test -- --runInBand", "timeout": 900, "expectedExit": 0})
    if (repo / "pyproject.toml").is_file() or (repo / "pytest.ini").is_file():
        common.append({"name": "pytest", "command": "python3 -m pytest -q", "timeout": 900, "expectedExit": 0})
    if (repo / "Cargo.toml").is_file():
        common.extend(
            [
                {"name": "cargo-check", "command": "cargo check", "timeout": 900, "expectedExit": 0},
                {"name": "cargo-test", "command": "cargo test", "timeout": 900, "expectedExit": 0},
            ]
        )
    if (repo / "pom.xml").is_file():
        common.append({"name": "maven-test", "command": "mvn test", "timeout": 1200, "expectedExit": 0})
    if (repo / "build.gradle").is_file() or (repo / "build.gradle.kts").is_file():
        common.append({"name": "gradle-test", "command": "./gradlew test", "timeout": 1200, "expectedExit": 0})
    return {"a": common, "b": common}


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=8)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def _ready(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def execute_check(check: dict[str, Any], *, cwd: Path, log_dir: Path, index: int) -> dict[str, Any]:
    name = str(check.get("name") or f"check-{index}")
    command = str(check.get("command") or "").strip()
    if not command:
        raise SologsbError(f"验证项 {name} 缺少 command")
    timeout = float(check.get("timeout") or 900)
    expected_exit = int(check.get("expectedExit", 0))
    expected_contains = [str(value) for value in check.get("expectedContains") or []]
    log_path = log_dir / f"{index:02d}-{name}.log"
    started = time.monotonic()
    error = ""
    exit_code = -1
    output = ""
    background = bool(check.get("background"))
    if background:
        env = os.environ.copy()
        proc = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        ready_url = str(check.get("readyUrl") or "")
        if ready_url and not _ready(ready_url, timeout):
            error = f"服务未在 {timeout:.0f} 秒内就绪: {ready_url}"
        else:
            hold = float(check.get("holdSeconds") or 3)
            time.sleep(hold)
        try:
            out, _ = proc.communicate(timeout=3)
            output = out.decode("utf-8", errors="replace") if out else ""
            exit_code = proc.returncode if proc.returncode is not None else -1
        except subprocess.TimeoutExpired:
            output = ""
            exit_code = 0
        finally:
            _stop_process(proc)
        if not error and exit_code != 0 and not check.get("allowExitAfterReady"):
            error = f"后台命令异常退出: {exit_code}"
    else:
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
            output = proc.stdout.decode("utf-8", errors="replace")
            exit_code = proc.returncode
            if exit_code != expected_exit:
                error = f"退出码 {exit_code}，期望 {expected_exit}"
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", errors="replace")
            error = f"执行超过 {timeout:.0f} 秒"
            exit_code = 124
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(output, encoding="utf-8")
    missing = [needle for needle in expected_contains if needle not in output]
    if missing and not error:
        error = f"输出缺少预期内容: {missing}"
    return {
        "name": name,
        "command": command,
        "cwd": str(cwd),
        "exitCode": exit_code,
        "expectedExit": expected_exit,
        "durationSeconds": round(time.monotonic() - started, 3),
        "outputSha256": sha256_text(output),
        "outputPreview": output[-4000:],
        "logPath": str(log_path.resolve()),
        "ok": not error,
        "observedFailure": bool(check.get("observedFailure")),
        "error": error,
    }


def verify_side(
    task_root: Path,
    side: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    side = side.upper()
    state = read_json(task_root / "monitor" / "state.json", {})
    side_state = (state.get("sides") or {}).get(side) or {}
    if side_state.get("status") != "clean":
        raise SologsbError(f"{side} 尚未干净完成")
    if not checks:
        raise SologsbError(f"{side} 没有验证命令，禁止标记 verified")
    verify_dir = task_root / "monitor" / "verify" / side.lower()
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    run(
        [
            "git",
            "clone",
            "--branch",
            side,
            "--single-branch",
            str(state["remoteUrl"]),
            str(verify_dir),
        ],
        env=github_env(require_proxy=True),
    )
    sha = run(["git", "-C", str(verify_dir), "rev-parse", "HEAD"]).stdout.decode().strip()
    if sha != str(side_state.get("artifactSnapshot") or ""):
        raise SologsbError(f"{side} 验证 clone 的 HEAD 与产物快照不一致")
    log_dir = task_root / "monitor" / "verify-logs" / side.lower()
    log_dir.mkdir(parents=True, exist_ok=True)
    results = [
        execute_check(check, cwd=verify_dir, log_dir=log_dir, index=index)
        for index, check in enumerate(checks, 1)
    ]
    return {
        "side": side,
        "artifactSnapshot": sha,
        "checks": results,
        "ok": all(item["ok"] for item in results),
        "verifiedAt": utc_now(),
    }


def run_verification(task_root: Path, plan_path: Path | None = None) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    if (state.get("sides") or {}).get("B", {}).get("status") != "clean":
        raise SologsbError("A/B 都必须先干净完成")
    plan_path = plan_path or (task_root / "monitor" / "verification-plan.json")
    if not plan_path.is_file():
        suggestion = suggest_plan(task_root / "source" / "origin")
        write_json(task_root / "monitor" / "verification-plan.suggested.json", suggestion)
        raise SologsbError(
            "缺少 verification-plan.json。请按项目真实入口填写 A/B 命令；"
            f"已生成建议: {task_root / 'monitor' / 'verification-plan.suggested.json'}"
        )
    plan = read_json(plan_path, {})
    result = {
        "a": verify_side(task_root, "A", list(plan.get("a") or [])),
        "b": verify_side(task_root, "B", list(plan.get("b") or [])),
        "planPath": str(plan_path.resolve()),
    }
    result["ok"] = bool(result["a"]["ok"] and result["b"]["ok"])
    write_json(task_root / "monitor" / "verification.json", result)
    if result["ok"]:
        state["status"] = "verified"
        save_state(task_root, state)
    return result
