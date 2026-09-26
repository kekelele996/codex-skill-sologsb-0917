#!/usr/bin/env python3
"""定期把本机 sologsb 任务状态推送到 Bark。

用法：
  status_push.py show                    打印本机状态摘要（不推送）
  status_push.py push                    推送一次
  status_push.py install [--interval N]  按 N 分钟（默认读设备配置 notify.intervalMinutes）定期推送
  status_push.py uninstall               取消定期推送

所有数据每次现取，不写死任何设备信息：
- SOLO2 账号、平台地址、Bark 地址来自设备配置（~/.codex/sologsb/config.json）；
- 容器用量 / 上限 / 排队候选来自 side_runner 的容器名额账本（与技能限流同一口径）；
- 运行中任务来自跨进程项目占用锁；
- 平台提交来自 SOLO2 GSB 列表接口，平台不可达时回落到本机 GSB 历史缓存。
Bark 地址用 `configure.py set notify.barkUrl=https://api.day.app/<key>` 配置。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_config as dc  # noqa: E402

LAUNCHD_LABEL = "com.sologsb-0917.status-push"
CRON_MARKER = "# sologsb-0917 status-push"
LOG_PATH = Path.home() / ".codex" / "sologsb-0917" / "status-push.log"
SEND_ATTEMPTS = 4
MAX_TASK_LINES = 6


def _device_name() -> str:
    if platform.system() == "Darwin":
        try:
            proc = subprocess.run(["scutil", "--get", "ComputerName"],
                                  capture_output=True, text=True, timeout=5)
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".")[0]


def _local_date(value: str) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)[:10]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d")


def _hhmm(value: str) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%H:%M")


def container_section() -> tuple[list[str], dict[str, int]]:
    """容器名额（与 side_runner 限流同一账本）；返回文字行和每个项目的容器数。"""
    import side_runner

    limiter = side_runner._ContainerLimiter()
    status = limiter.status()
    if status.get("ok"):
        counted = set(status.get("runningNames") or [])
        per_project = Counter(
            label.casefold()
            for name, label in limiter._running_containers(limiter._count_all_containers())
            if name in counted and label
        )
    if not status.get("ok"):
        return [f"容器：读取失败（{status.get('error', '')[:60]}），上限 {status.get('limit')}"], {}
    line = f"容器 {status['used']}/{status['limit']}（运行 {status['runningContainers']}"
    if status.get("reservedSlots"):
        line += f" · 预占 {status['reservedSlots']}"
    line += f"）· 候选排队 {status.get('queuedCandidates', 0)}"
    return [line], dict(per_project)


def task_section(per_project: dict[str, int]) -> list[str]:
    """运行中任务 = 本机仍持有项目占用锁的项目。"""
    import project_claims

    base = os.environ.get("SOLO_MANAGER_BASE_URL", "").strip()
    if not base:
        return ["任务：设备配置缺少 manager.baseUrl"]
    # 正在占容器（候选赛阶段）的排前面
    codes = sorted(project_claims.claimed_project_codes(base), key=lambda c: (-per_project.get(c, 0), c))
    lines = [f"运行中任务 {len(codes)}（占容器 {sum(1 for c in codes if per_project.get(c))}）"]
    for code in codes[:MAX_TASK_LINES]:
        count = per_project.get(code, 0)
        lines.append(f"▶ {code}" + (f" · {count} 容器" if count else ""))
    if len(codes) > MAX_TASK_LINES:
        lines.append(f"… 另有 {len(codes) - MAX_TASK_LINES} 个")
    return lines


def _summarize_submissions(items: list[dict[str, Any]], total: int | None) -> list[str]:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    today_count = sum(1 for item in items if _local_date(item.get("submittedAt") or "") == today)
    statuses = Counter(str(item.get("status") or "未知") for item in items)
    lines = [f"今日提交 {today_count} · 总提交 {total if total is not None else len(items)}"]
    if statuses:
        lines.append("状态 " + " · ".join(f"{k} {v}" for k, v in statuses.most_common(4)))
    return lines


def platform_section() -> list[str]:
    """SOLO2 GSB 提交；平台不可达时用技能本机的 GSB 历史缓存兜底。"""
    import gsb_tools

    preflight = gsb_tools._load_submission_preflight()
    params = {"user_id": "0", "leader_id": "0", "page": "1", "page_size": "200"}
    try:
        payload = preflight._readonly_json("/api/v1/gsb/submissions?" + urllib.parse.urlencode(params),
                                           timeout=30)
        items = [
            {
                "status": str(item.get("status_label") or item.get("status") or ""),
                "submittedAt": str(item.get("submitted_at") or item.get("created_at") or ""),
            }
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        total = (payload.get("meta") or {}).get("total")
        return _summarize_submissions(items, int(total) if str(total or "").isdigit() else None)
    except Exception as exc:  # noqa: BLE001 - 平台故障不能影响本机状态推送
        reason = f"{type(exc).__name__}: {exc}"[:50]
    try:
        cache = json.loads(preflight.gsb_history_cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = None
    if not isinstance(cache, dict) or not cache.get("items"):
        return [f"平台不可达（{reason}），无本机缓存"]
    lines = _summarize_submissions(cache.get("items") or [], cache.get("serverTotal"))
    lines[0] += f"（平台不可达，缓存 {_hhmm(cache.get('fetchedAt') or '')}）"
    return lines


def build_status() -> tuple[str, str]:
    dc.load_and_apply()
    cfg = dc.load()
    account = dc.resolve_from(cfg, "solo2.username") or "未配置账号"
    try:
        sections = platform_section()
    except Exception as exc:  # noqa: BLE001
        sections = [f"平台：读取失败 {type(exc).__name__}: {exc}"[:80]]
    try:
        container_lines, per_project = container_section()
    except Exception as exc:  # noqa: BLE001
        container_lines, per_project = [f"容器：读取失败 {type(exc).__name__}: {exc}"[:80]], {}
    try:
        sections.extend(task_section(per_project))
    except Exception as exc:  # noqa: BLE001
        sections.append(f"任务：读取失败 {type(exc).__name__}: {exc}"[:80])
    sections.extend(container_lines)
    title = f"📊 {account}@{_device_name()} {datetime.now().strftime('%H:%M')}"
    return title, "\n".join(sections)


def bark_send(title: str, body: str) -> None:
    cfg = dc.load()
    url = dc.resolve_from(cfg, "notify.barkUrl").rstrip("/")
    if not url:
        raise dc.ConfigError("缺少 notify.barkUrl：运行 configure.py set notify.barkUrl=https://api.day.app/<key>")
    data = urllib.parse.urlencode({
        "title": title,
        "body": body,
        "group": dc.resolve_from(cfg, "notify.group") or "sologsb",
        "ttl": "86400",
    }).encode("utf-8")
    last = ""
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as response:
                reply = json.loads(response.read().decode("utf-8") or "{}")
            if reply.get("code") == 200:
                return
            last = str(reply)[:120]
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < SEND_ATTEMPTS:
            time.sleep(attempt * 15)
    raise RuntimeError(f"Bark 推送失败：{last}")


def _interval_minutes(value: int | None) -> int:
    raw = value if value is not None else dc.resolve_from(dc.load(), "notify.intervalMinutes")
    try:
        return max(5, int(raw))
    except (TypeError, ValueError):
        return 30


def _job_path() -> str:
    """定时任务里的 PATH：带上本机 docker 所在目录，launchd/cron 默认 PATH 找不到它。"""
    dirs = [str(Path(sys.executable).parent)]
    docker = shutil.which("docker")
    if docker:
        dirs.append(str(Path(docker).resolve().parent))
        dirs.append(str(Path(docker).parent))
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return ":".join(dict.fromkeys(dirs))


def _launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def install(interval: int) -> str:
    script = str(Path(__file__).resolve())
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Darwin":
        plist = _launchd_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
        plist.write_bytes(plistlib.dumps({
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": [sys.executable, script, "push"],
            "StartInterval": interval * 60,
            "RunAtLoad": True,
            "EnvironmentVariables": {"PATH": _job_path()},
            "StandardOutPath": str(LOG_PATH),
            "StandardErrorPath": str(LOG_PATH),
        }))
        proc = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap 失败：{proc.stderr.strip()}")
        return f"已安装 launchd 任务 {LAUNCHD_LABEL}，每 {interval} 分钟推送一次（日志 {LOG_PATH}）"
    if not shutil.which("crontab"):
        raise RuntimeError("本机既不是 macOS 也没有 crontab，无法安装定期推送")
    line = (f"*/{interval} * * * * PATH={_job_path()} {sys.executable} {script} push "
            f">>{LOG_PATH} 2>&1 {CRON_MARKER}") if interval < 60 else (
            f"0 */{max(1, interval // 60)} * * * PATH={_job_path()} {sys.executable} {script} push "
            f">>{LOG_PATH} 2>&1 {CRON_MARKER}")
    _write_crontab(line)
    return f"已写入 crontab，每 {interval} 分钟推送一次（日志 {LOG_PATH}）"


def _write_crontab(line: str | None) -> None:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    kept = [row for row in (current.stdout.splitlines() if current.returncode == 0 else [])
            if CRON_MARKER not in row]
    if line:
        kept.append(line)
    subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)


def uninstall() -> str:
    if platform.system() == "Darwin":
        plist = _launchd_plist()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        return f"已移除 launchd 任务 {LAUNCHD_LABEL}"
    if shutil.which("crontab"):
        _write_crontab(None)
    return "已移除 crontab 定期推送"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机 sologsb 任务状态 Bark 推送")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", help="打印本机状态摘要")
    sub.add_parser("push", help="推送一次")
    inst = sub.add_parser("install", help="安装定期推送")
    inst.add_argument("--interval", type=int, default=None, help="分钟，默认 notify.intervalMinutes 或 30")
    sub.add_parser("uninstall", help="取消定期推送")
    args = parser.parse_args(argv)

    if args.command in {"show", "push"}:
        title, body = build_status()
        stamp = datetime.now().strftime("%F %T")
        if args.command == "show":
            print(f"{title}\n{body}")
            return 0
        try:
            bark_send(title, body)
        except Exception as exc:  # noqa: BLE001
            print(f"{stamp} 推送失败：{exc}", file=sys.stderr)
            return 1
        print(f"{stamp} 已推送：{title}")
        return 0
    try:
        message = install(_interval_minutes(args.interval)) if args.command == "install" else uninstall()
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
