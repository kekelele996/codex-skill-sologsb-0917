#!/usr/bin/env python3
"""sologsb-0917 一键本机凭据配置。

用法：
  configure.py init [--from-current]   新增配置（--from-current 从本机钥匙串/gh 导入现有凭据）
  configure.py set KEY=VALUE ...       新增或覆盖单项
  configure.py unset KEY               删除单项
  configure.py login                   用账号密码登录，把交换到的 token/cookie 写回配置
  configure.py show                    查看配置（密钥脱敏）
  configure.py verify                  联网验证四类凭据是否真的可用
  configure.py path                    打印配置文件路径

配置文件默认 ~/.codex/sologsb/config.json，权限 0600，明文保存。
可用 --config PATH 或环境变量 SOLOSB_CONFIG 指定其他位置。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_config as dc  # noqa: E402

TIMEOUT_MANAGER = 15
TIMEOUT_SOLO2 = 20
TIMEOUT_GITHUB = 30


class Unreachable(RuntimeError):
    """目标地址连不上（连接被拒绝 / 网络不可达 / 超时）。"""

    def __init__(self, url: str, exc: Exception):
        reason = getattr(exc, "strerror", None) or f"{type(exc).__name__}: {exc}"
        super().__init__(f"无法连接 {url}（{reason}）")
        self.url = url
        self.exc = exc


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    warnings: list[str] = field(default_factory=list)
    updates: dict[str, str] = field(default_factory=dict)
    retryable: bool = False  # 网关临时故障：重跑即可，不要重填密钥


# ---------------------------------------------------------------- 基础工具

def http_json(method: str, url: str, *, headers: dict[str, str] | None = None,
              body: dict | None = None, timeout: float = 20) -> tuple[int, dict, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
            return response.status, payload, dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:400]}
        return exc.code, payload, dict(exc.headers)


def safe_http_json(method: str, url: str, **kwargs) -> tuple[int, dict, dict]:
    """http_json 的包装：把连接类错误转成 Unreachable，避免裸 traceback。"""
    try:
        return http_json(method, url, **kwargs)
    except Exception as exc:
        raise Unreachable(url, exc) from exc


def curl_json(url: str, *, token: str = "", proxy: str = "", method: str = "GET",
              extra_headers: list[str] | None = None, timeout: float = 30) -> tuple[int, str, dict]:
    cmd = ["curl", "-sS", "--suppress-connect-headers", "-m", str(int(timeout)),
           "-X", method, "-D", "-", "-o", "-", url]
    if proxy:
        cmd += ["-x", proxy]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    for header in extra_headers or []:
        cmd += ["-H", header]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    stdout = proc.stdout
    head, _, body = stdout.partition("\r\n\r\n")
    if not body:
        head, _, body = stdout.partition("\n\n")
    status = 0
    for line in head.splitlines():
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
    headers = {}
    for line in head.splitlines()[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    if proc.returncode != 0 and not status:
        return 0, (proc.stderr.strip() or "curl 执行失败"), headers
    return status, body, headers


def keychain_read(service: str, account: str) -> str:
    if sys.platform != "darwin":
        return ""
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True, check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def device_identity() -> dict[str, str]:
    """复用 solo-annotation-loop 的设备身份（Claude Key 所在的钥匙串条目）。"""
    script = dc.Path.home() / ".codex" / "skills" / "solo-annotation-loop" / "scripts" / "device-identity.py"
    if not script.is_file():
        return {}
    proc = subprocess.run([sys.executable, str(script), "--format", "json"],
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- 各项验证

def verify_claude(cfg: dict) -> Result:
    api_key = dc.resolve_from(cfg, "claude.apiKey")
    base_url = (dc.resolve_from(cfg, "claude.baseUrl") or "").rstrip("/")
    model = dc.resolve_from(cfg, "claude.model") or "auto_model/urm"
    if not api_key:
        return Result("claude", False, "缺少 claude.apiKey")
    if not base_url:
        return Result("claude", False, "缺少 claude.baseUrl")
    body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
    headers = {"anthropic-version": "2023-06-01", "Authorization": f"Bearer {api_key}"}

    attempts = 2
    for attempt in range(1, attempts + 1):
        try:
            status, payload, _ = http_json("POST", f"{base_url}/v1/messages", headers=headers,
                                           body=body, timeout=120)
        except Exception as exc:
            if attempt < attempts:
                print(f"    （网关未响应：{type(exc).__name__}，{5 * attempt} 秒后重试）", flush=True)
                time.sleep(5 * attempt)
                continue
            return Result("claude", False,
                          f"网关无响应（{type(exc).__name__}），未能确认密钥状态", retryable=True,
                          warnings=["这通常是网关繁忙或网络抖动，不是密钥问题；稍后重跑 verify 即可"])
        if status == 200:
            return Result("claude", True, f"接口返回 200，模型 {model} 可调用")
        if status == 401:
            return Result("claude", False, "网关返回 401，密钥无效")
        if status == 403:
            return Result("claude", False, "网关返回 403，密钥被拒绝或权限不足")
        if status == 429:
            return Result("claude", False, "网关返回 429，并发已满", retryable=True,
                          warnings=["429 是准入失败，不代表密钥错误；等网关空闲后重跑 verify"])
        if status in (500, 502, 503, 504):
            if attempt < attempts:
                print(f"    （网关返回 {status}，{5 * attempt} 秒后重试）", flush=True)
                time.sleep(5 * attempt)
                continue
            return Result("claude", False,
                          f"网关连续返回 {status}（网关侧故障），密钥未被拒绝", retryable=True,
                          warnings=["网关临时故障，稍后重跑 verify；不要因此重填密钥"])
        detail = str(payload).replace("\n", " ")[:120]
        return Result("claude", False, f"网关返回 {status}：{detail}")

def verify_manager(cfg: dict) -> Result:
    base_url = (dc.resolve_from(cfg, "manager.baseUrl") or "").rstrip("/")
    username = dc.resolve_from(cfg, "manager.username")
    password = dc.resolve_from(cfg, "manager.password")
    token = dc.resolve_from(cfg, "manager.token")
    if not base_url:
        return Result("manager", False, "缺少 manager.baseUrl")

    updates: dict[str, str] = {}
    login_note = ""
    if username and password:
        try:
            status, payload, _ = safe_http_json(
                "POST", f"{base_url}/api/v1/auth/login",
                body={"username": username, "password": password}, timeout=TIMEOUT_MANAGER)
        except Unreachable as exc:
            return Result("manager", False, str(exc),
                          warnings=["请确认 manager.baseUrl 正确且该地址在本机可达"])
        if status == 200:
            new_token = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
            if not new_token:
                return Result("manager", False, "登录成功但响应里没有 accessToken")
            token = new_token
            updates["manager.token"] = new_token
            login_note = "账号密码登录成功"
        else:
            detail = payload.get("detail") or payload.get("message") or str(payload)[:160]
            return Result("manager", False, f"账号密码登录失败：HTTP {status} {detail}")
    if not token:
        return Result("manager", False, "缺少 manager 凭据（username/password 或 token）")

    try:
        status, payload, _ = safe_http_json("GET", f"{base_url}/api/v1/auth/me",
                                            headers={"Authorization": "Bearer " + token},
                                            timeout=TIMEOUT_MANAGER)
    except Unreachable as exc:
        return Result("manager", False, str(exc),
                      warnings=["请确认 manager.baseUrl 正确且该地址在本机可达"])
    if status != 200:
        return Result("manager", False, f"token 校验失败：HTTP {status} {str(payload)[:160]}")
    who = payload.get("username") or payload.get("name") or (payload.get("user") or {}).get("username") or "?"
    prefix = f"{login_note}；" if login_note else ""
    return Result("manager", True, f"{prefix}身份 {who} 校验通过", updates=updates)


def verify_solo2(cfg: dict) -> Result:
    base_url = (dc.resolve_from(cfg, "solo2.baseUrl") or "").rstrip("/")
    username = dc.resolve_from(cfg, "solo2.username")
    password = dc.resolve_from(cfg, "solo2.password")
    cookie = dc.resolve_from(cfg, "solo2.cookie")
    csrf = dc.resolve_from(cfg, "solo2.csrf")
    if not base_url:
        return Result("solo2", False, "缺少 solo2.baseUrl")

    updates: dict[str, str] = {}
    login_note = ""
    if username and password:
        try:
            status, payload, headers = safe_http_json(
                "POST", f"{base_url}/api/v1/auth/login",
                headers={"Origin": base_url}, body={"username": username, "password": password},
                timeout=TIMEOUT_SOLO2)
        except Unreachable as exc:
            return Result("solo2", False, str(exc),
                          warnings=["请确认 solo2.baseUrl 正确且本机网络可达"])
        if status != 200:
            detail = payload.get("detail") or payload.get("message") or str(payload)[:160]
            return Result("solo2", False, f"账号密码登录失败：HTTP {status} {detail}")
        raw_cookies = headers.get("Set-Cookie") or headers.get("set-cookie") or ""
        if isinstance(raw_cookies, str):
            raw_cookies = [raw_cookies]
        pairs, csrf_value = [], ""
        for item in raw_cookies:
            for chunk in str(item).split(","):
                piece = chunk.split(";")[0].strip()
                if "=" not in piece:
                    continue
                name, _, value = piece.partition("=")
                pairs.append(f"{name.strip()}={value.strip()}")
                if name.strip() == "solo_qa_csrf":
                    csrf_value = value.strip()
        if not pairs:
            return Result("solo2", False, "登录成功但响应没有 Set-Cookie，无法保存会话")
        cookie = "; ".join(pairs)
        csrf = csrf_value or csrf
        updates["solo2.cookie"] = cookie
        if csrf:
            updates["solo2.csrf"] = csrf
        login_note = "账号密码登录成功"
    if not cookie:
        return Result("solo2", False, "缺少 solo2 凭据（username/password 或 cookie）")

    headers = {"Cookie": cookie}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    try:
        status, payload, _ = safe_http_json("GET", f"{base_url}/api/v1/auth/me", headers=headers,
                                            timeout=TIMEOUT_SOLO2)
    except Unreachable as exc:
        return Result("solo2", False, str(exc),
                      warnings=["请确认 solo2.baseUrl 正确且本机网络可达"])
    if status != 200:
        return Result("solo2", False, f"会话校验失败：HTTP {status} {str(payload)[:160]}")
    who = payload.get("username") or (payload.get("user") or {}).get("username") or "?"
    role = payload.get("role") or (payload.get("user") or {}).get("role") or ""
    prefix = f"{login_note}；" if login_note else ""
    return Result("solo2", True, f"{prefix}身份 {who}{'/' + role if role else ''} 校验通过", updates=updates)


def verify_github(cfg: dict) -> Result:
    token = dc.resolve_from(cfg, "github.token")
    if not token:
        return Result("github", False, "缺少 github.token")
    proxy_http = dc.resolve_from(cfg, "github.proxyHttp") or ""
    proxy_socks = dc.resolve_from(cfg, "github.proxySocks") or ""
    candidates = []
    if proxy_http:
        candidates.append(f"http://{proxy_http}" if "://" not in proxy_http else proxy_http)
    if proxy_socks:
        candidates.append(f"socks5h://{proxy_socks}" if "://" not in proxy_socks else proxy_socks)
    candidates.append("")  # 最后兜底直连，仅用于给出明确错误

    last = ""
    saw_transient = False
    for proxy in candidates:
        status, body, headers = curl_json("https://api.github.com/user", token=token,
                                          proxy=proxy, timeout=TIMEOUT_GITHUB)
        label = proxy or "直连"
        if status == 200:
            try:
                login = json.loads(body).get("login", "?")
            except json.JSONDecodeError:
                login = "?"
            scopes = headers.get("x-oauth-scopes", "")
            warn = []
            if scopes and "repo" not in scopes:
                warn.append(f"token 缺少 repo 权限（现有 scope: {scopes}）")
            return Result("github", True, f"经 {label} 校验通过，账号 {login}",
                          warnings=warn, updates={"github.username": login})
        if status == 401:
            return Result("github", False, f"经 {label} 返回 401，token 无效或已撤销")
        if status == 0 or status >= 500:
            saw_transient = True
        last = f"{label} -> HTTP {status or '连接失败'} {body[:120]}"
    return Result("github", False, f"GitHub 校验失败：{last}", retryable=saw_transient)


VERIFIERS = {
    "claude": verify_claude,
    "manager": verify_manager,
    "solo2": verify_solo2,
    "github": verify_github,
}


# ---------------------------------------------------------------- 子命令

def cmd_init(args, path: Path) -> int:
    cfg = dc.load(path)
    changed = []
    interactive = not (getattr(args, "non_interactive", False) or not sys.stdin.isatty())

    def ask(dotted: str, label: str, *, secret: bool = False, current: str = "") -> None:
        existing = dc._text(dc.get_path(cfg, dotted, ""))
        if existing and not getattr(args, "all", False):
            return  # 默认只补缺失项，改已有值请用 set 或 --all
        hint = ""
        if existing:
            hint = f"（当前 {dc.mask(existing)}，直接回车保留）"
        else:
            current_hint = dc._text(current)
            if current_hint:
                hint = f"（本机检测到 {dc.mask(current_hint) if secret else current_hint}，直接回车采用）"
        if not interactive:
            value = existing or dc._text(current)
        else:
            prompt = f"{label}{hint}: "
            try:
                value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
            except EOFError:
                value = ""
            if not value:
                value = existing or dc._text(current)
        if value:
            dc.set_path(cfg, dotted, value)
            changed.append(dotted)

    from_current = args.from_current
    identity = device_identity() if from_current else {}
    claude_key = ""
    if identity:
        claude_key = keychain_read(identity.get("keychainService", ""), identity.get("account", ""))
    manager_token = keychain_read("solo-manager-token", os.environ.get("USER", ""))
    # 钥匙串条目名只从设备配置读，技能包里不留具体名称
    solo2_service = dc.resolve_from(cfg, "solo2.keychainService")
    solo2_cookie = keychain_read(solo2_service + "-cookie", os.environ.get("USER", "")) if solo2_service else ""
    solo2_csrf = keychain_read(solo2_service + "-csrf", os.environ.get("USER", "")) if solo2_service else ""
    gh_token = ""
    if from_current:
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        gh_token = proc.stdout.strip() if proc.returncode == 0 else ""

    print(f"配置文件: {path}")
    print("直接回车 = 保留当前值 / 采用本机检测到的值\n")

    ask("claude.apiKey", "Claude API Key", secret=True, current=claude_key)
    ask("claude.baseUrl", "Claude LLM Base URL", current=dc.FIELDS["claude.baseUrl"][1])
    ask("manager.baseUrl", "Solo Manager 地址", current=dc.FIELDS["manager.baseUrl"][1])
    ask("manager.username", "Solo Manager 账号")
    ask("manager.password", "Solo Manager 密码", secret=True)
    if manager_token and not dc._text(dc.resolve_from(cfg, "manager.token")):
        dc.set_path(cfg, "manager.token", manager_token)
        changed.append("manager.token")
        print(f"  （已导入本机 Solo Manager token，长度 {len(manager_token)}）")
    ask("solo2.baseUrl", "SOLO2 地址", current=dc.FIELDS["solo2.baseUrl"][1])
    ask("solo2.username", "SOLO2 账号")
    ask("solo2.password", "SOLO2 密码", secret=True)
    for dotted, value in (("solo2.cookie", solo2_cookie), ("solo2.csrf", solo2_csrf)):
        if value and not dc._text(dc.get_path(cfg, dotted, "")):
            dc.set_path(cfg, dotted, value)
            changed.append(dotted)
            print(f"  （已导入本机 {dotted}，长度 {len(value)}）")
    ask("github.token", "GitHub Token", secret=True, current=gh_token)

    written = dc.save(cfg, path)
    print(f"\n已写入 {written}（权限 0600），共更新 {len(set(changed))} 项")

    required = ["claude.apiKey", "claude.baseUrl", "manager.baseUrl", "solo2.baseUrl", "github.token"]
    missing = [k for k in required if not dc._text(dc.get_path(cfg, k, ""))]
    print("运行必需项：" + ("齐全" if not missing else "缺少 " + "、".join(missing)))

    for label, user_key, pass_key, fallback_key in (
        ("Solo Manager", "manager.username", "manager.password", "manager.token"),
        ("SOLO2", "solo2.username", "solo2.password", "solo2.cookie"),
    ):
        has_account = bool(dc._text(dc.get_path(cfg, user_key, ""))) and bool(dc._text(dc.get_path(cfg, pass_key, "")))
        has_session = bool(dc._text(dc.get_path(cfg, fallback_key, "")))
        if has_account:
            print(f"{label} 账号密码：已填")
        elif has_session:
            print(f"{label} 账号密码：未填（当前靠已导入的会话凭据运行）")
        else:
            print(f"{label} 账号密码：缺失，必须先填")
    print("下一步执行： configure.py verify")
    return 0


def cmd_set(args, path: Path) -> int:
    cfg = dc.load(path)
    for item in args.pairs:
        if "=" not in item:
            print(f"格式应为 KEY=VALUE：{item}", file=sys.stderr)
            return 2
        dotted, _, value = item.partition("=")
        dotted = dotted.strip()
        if dotted not in dc.FIELDS:
            print(f"未知配置项：{dotted}\n可用项：{', '.join(dc.known_fields())}", file=sys.stderr)
            return 2
        old, _ = dc.set_path(cfg, dotted, value)
        shown = dc.mask(value) if dotted in dc.SECRET_FIELDS else value
        action = "覆盖" if old not in (None, "") else "新增"
        print(f"{action} {dotted} = {shown}")
    dc.save(cfg, path)
    print(f"已保存 {path}")
    return 0


def cmd_unset(args, path: Path) -> int:
    cfg = dc.load(path)
    for dotted in args.keys:
        removed = dc.delete_path(cfg, dotted)
        print(f"删除 {dotted}" if removed is not None else f"未设置 {dotted}（忽略）")
    dc.save(cfg, path)
    return 0


def cmd_show(args, path: Path) -> int:
    exists = path.is_file()
    print(f"配置文件: {path}{'' if exists else '（尚未创建）'}")
    if exists:
        mode = oct(path.stat().st_mode & 0o777)
        print(f"权限: {mode}{'' if mode == '0o600' else '  ← 建议 0600'}")
    print()
    for dotted, info in dc.snapshot(path).items():
        value = info["value"] or "-"
        print(f"  {dotted:24s} {value:52s} [{info['source']}]")
    return 0


def cmd_path(args, path: Path) -> int:
    print(path)
    return 0


def cmd_login(args, path: Path) -> int:
    cfg = dc.load(path)
    updates: dict[str, str] = {}
    ok = True
    for name, verifier in VERIFIERS.items():
        if name == "claude":
            continue
        try:
            result = verifier(cfg)
        except Exception as exc:  # 兜底，避免裸 traceback
            result = Result(name, False, f"验证过程异常：{type(exc).__name__}: {exc}")
        print(f"[{'OK  ' if result.ok else 'FAIL'}] {name:8s} {result.detail}")
        if result.warnings:
            for warning in result.warnings:
                print(f"          注意: {warning}")
        if result.ok and result.updates:
            updates.update(result.updates)
        if not result.ok:
            ok = False
    if updates:
        for dotted, value in updates.items():
            dc.set_path(cfg, dotted, value)
        dc.save(cfg, path)
        print(f"\n已回写 {len(updates)} 项到 {path}：{', '.join(sorted(updates))}")
    return 0 if ok else 1


def cmd_verify(args, path: Path) -> int:
    cfg = dc.load(path)
    if not cfg.get("claude") and not cfg.get("manager"):
        print(f"配置文件为空或不存在: {path}\n先执行 configure.py init", file=sys.stderr)
        return 2
    wanted = args.only or list(VERIFIERS)
    updates: dict[str, str] = {}
    failures = 0
    for name in wanted:
        verifier = VERIFIERS.get(name)
        if verifier is None:
            print(f"未知验证项: {name}（可用: {', '.join(VERIFIERS)}）", file=sys.stderr)
            return 2
        try:
            result = verifier(cfg)
        except Exception as exc:  # 兜底，避免裸 traceback
            result = Result(name, False, f"验证过程异常：{type(exc).__name__}: {exc}")
        print(f"[{'OK  ' if result.ok else 'FAIL'}] {name:8s} {result.detail}")
        for warning in result.warnings:
            print(f"          注意: {warning}")
        if result.ok and result.updates:
            updates.update(result.updates)
        if not result.ok:
            failures += 1
    if updates:
        for dotted, value in updates.items():
            dc.set_path(cfg, dotted, value)
        dc.save(cfg, path)
        print(f"\n已回写 {len(updates)} 项到 {path}：{', '.join(sorted(updates))}")
    print(f"\n结论: {'全部通过' if failures == 0 else f'{failures} 项未通过'}")
    return 0 if failures == 0 else 1



# ---------------------------------------------------------------- 交互向导

SECTION_ORDER = ["claude", "manager", "solo2", "github"]
SECTION_TITLES = {
    "claude": "Claude Code 容器",
    "manager": "Solo Manager（任务来源）",
    "solo2": "SOLO2（提交平台）",
    "github": "GitHub（产物仓库）",
}


def _display_width(text: str) -> int:
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _rule(title: str) -> None:
    print()
    print("─" * 64)
    print(f"  {title}")
    print("─" * 64)


def prompt_field(cfg: dict, dotted: str, label: str, *, default: str = "",
                 secret: bool = False, validate=None) -> str:
    """读取一个字段并写入 cfg。回车=保留，输入 - =清空。"""
    current = dc._text(dc.get_path(cfg, dotted, ""))
    shown = dc.mask(current) if secret else current
    hints = []
    if shown:
        hints.append(f"当前 {shown}")
    if not current and default:
        hints.append(f"默认 {default}")
    hint = f"（{'，'.join(hints)}）" if hints else ""
    while True:
        prompt = f"{label}{hint}: "
        try:
            raw = getpass.getpass(prompt) if secret else input(prompt)
        except EOFError:
            return current or default
        raw = raw.strip()
        if not raw:
            value = current or default
            break
        if raw == "-":
            value = ""
            break
        if validate:
            problem = validate(raw)
            if problem:
                print(f"  ✗ {problem}")
                continue
        value = raw
        break
    if value:
        dc.set_path(cfg, dotted, value)
    else:
        dc.delete_path(cfg, dotted)
    return value


def section_claude(cfg: dict, path: Path, *, advanced: bool = False) -> None:
    _rule(SECTION_TITLES["claude"])
    print("  这是容器里跑 Claude Code 用的密钥与网关。")
    prompt_field(cfg, "claude.apiKey", "API Key", secret=True)
    prompt_field(cfg, "claude.baseUrl", "LLM Base URL",
                 default=dc.FIELDS["claude.baseUrl"][1],
                 validate=lambda v: None if v.startswith(("http://", "https://")) else "必须 http:// 或 https:// 开头")
    max_containers = dc.FIELDS["claude.maxContainers"][1]
    prompt_field(
        cfg,
        "claude.maxContainers",
        f"最大并发容器（默认 {max_containers}，绝对上限 6）",
        default=max_containers,
        validate=lambda v: None if v.isdigit() and 1 <= int(v) <= 6 else "必须是 1-6 的正整数",
    )
    if advanced:
        prompt_field(cfg, "claude.model", "模型名", default=dc.FIELDS["claude.model"][1])
        prompt_field(cfg, "claude.image", "Docker 镜像", default=dc.FIELDS["claude.image"][1])
        prompt_field(cfg, "claude.contextWindow", "上下文窗口", default=dc.FIELDS["claude.contextWindow"][1],
                     validate=lambda v: None if v.isdigit() else "必须是数字")
    dc.save(cfg, path)


def section_manager(cfg: dict, path: Path, *, advanced: bool = False) -> None:
    _rule(SECTION_TITLES["manager"])
    print("  任务项目从哪来。用账号密码登录换取 token，比手工贴 token 省事。")
    prompt_field(cfg, "manager.baseUrl", "平台地址", default=dc.FIELDS["manager.baseUrl"][1],
                 validate=lambda v: None if v.startswith(("http://", "https://")) else "必须 http:// 或 https:// 开头")
    has_token = bool(dc._text(dc.get_path(cfg, "manager.token", "")))
    if not has_token:
        print("  （当前没有可用 token，请填账号密码）")
    prompt_field(cfg, "manager.username", "账号")
    prompt_field(cfg, "manager.password", "密码", secret=True)
    dc.save(cfg, path)


def section_solo2(cfg: dict, path: Path, *, advanced: bool = False) -> None:
    _rule(SECTION_TITLES["solo2"])
    print("  交付数据提交到哪。用账号密码登录换取会话 Cookie。")
    prompt_field(cfg, "solo2.baseUrl", "平台地址", default=dc.FIELDS["solo2.baseUrl"][1],
                 validate=lambda v: None if v.startswith(("http://", "https://")) else "必须 http:// 或 https:// 开头")
    has_cookie = bool(dc._text(dc.get_path(cfg, "solo2.cookie", "")))
    if not has_cookie:
        print("  （当前没有可用会话，请填账号密码）")
    prompt_field(cfg, "solo2.username", "账号")
    prompt_field(cfg, "solo2.password", "密码", secret=True)
    dc.save(cfg, path)


def section_github(cfg: dict, path: Path, *, advanced: bool = False) -> None:
    _rule(SECTION_TITLES["github"])
    print("  产物要推送到 GitHub，需要一个有 repo 权限的 Token。")
    prompt_field(cfg, "github.token", "GitHub Token", secret=True)
    if advanced:
        prompt_field(cfg, "github.proxyHttp", "HTTP 代理", default=dc.FIELDS["github.proxyHttp"][1])
        prompt_field(cfg, "github.proxySocks", "SOCKS 代理", default=dc.FIELDS["github.proxySocks"][1])
    dc.save(cfg, path)


SECTION_FUNCS = {
    "claude": section_claude, "manager": section_manager,
    "solo2": section_solo2, "github": section_github,
}


def run_verify_inline(cfg: dict, path: Path, only: list[str] | None = None) -> dict[str, bool]:
    """就地验证，返回 {失败项: 是否只是网关临时故障}。"""
    updates: dict[str, str] = {}
    failed: dict[str, bool] = {}
    for name in (only or SECTION_ORDER):
        print(f"  · 正在验证 {name} ...", flush=True)
        try:
            result = VERIFIERS[name](cfg)
        except Exception as exc:  # 兜底：单个验证器异常不影响其余项
            result = Result(name, False, f"验证过程异常：{type(exc).__name__}: {exc}")
        print(f"    [{'OK' if result.ok else 'FAIL'}] {result.detail}")
        for warning in result.warnings:
            print(f"           注意: {warning}")
        if result.ok and result.updates:
            updates.update(result.updates)
        if not result.ok:
            failed[name] = result.retryable
    if updates:
        for dotted, value in updates.items():
            dc.set_path(cfg, dotted, value)
        dc.save(cfg, path)
        print(f"  已回写 {len(updates)} 项会话凭据：{', '.join(sorted(updates))}")
    return failed


def print_summary(cfg: dict, path: Path) -> None:
    _rule("配置汇总")
    print(f"  文件: {path}（权限 {oct(path.stat().st_mode & 0o777) if path.is_file() else '-'}）")
    print()
    for dotted, info in dc.snapshot(path).items():
        marker = "✓" if info["configured"] else "·"
        value = info["value"] or "-"
        print(f"  {marker} {dotted:22s} {value:50s} [{info['source']}]")


def cmd_wizard(args, path: Path) -> int:
    if not sys.stdin.isatty():
        print("向导需要交互终端。请在你自己终端里运行；自动化请用 init --from-current --non-interactive。",
              file=sys.stderr)
        return 2
    cfg = dc.load(path)
    only = args.only or SECTION_ORDER

    print()
    print("╭" + "─" * 62 + "╮")
    print("│  " + _pad("sologsb-0917 本机配置向导", 58) + "  │")
    print("│  " + _pad("直接回车 = 保留当前值；输入 - = 清空该项", 58) + "  │")
    print("╰" + "─" * 62 + "╯")
    print(f"  配置文件: {path}")

    advanced = False
    if any(s in only for s in ("claude", "github")):
        try:
            answer = input("\n是否配置高级项（模型/镜像/上下文/并发/代理）？[y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        advanced = answer in ("y", "yes")

    for name in only:
        SECTION_FUNCS[name](cfg, path, advanced=advanced)

    failed: dict[str, bool] = {}
    try:
        answer = input("\n现在联网验证并自动登录换取会话吗？[Y/n]: ").strip().lower()
    except EOFError:
        answer = "n"
    if answer not in ("n", "no"):
        _rule("验证")
        failed = run_verify_inline(cfg, path, only)
        for _ in range(3):
            if not failed:
                break
            credential = [name for name, retryable in failed.items() if not retryable]
            transient = [name for name, retryable in failed.items() if retryable]
            if credential:
                try:
                    again = input(
                        f"\n以下项目未通过：{'、'.join(credential)}。重新填写这几项吗（地址写错也可以在这里改）？[Y/n]: "
                    ).strip().lower()
                except EOFError:
                    again = "n"
                if again in ("n", "no"):
                    break
                for name in credential:
                    SECTION_FUNCS[name](cfg, path, advanced=advanced)
            else:
                print(f"\n未通过的都是网关临时故障（{'、'.join(transient)}），凭据本身没被拒绝，不需要重填。")
                try:
                    again = input("现在重试验证吗？[Y/n]: ").strip().lower()
                except EOFError:
                    again = "n"
                if again in ("n", "no"):
                    break
            print()
            failed = run_verify_inline(cfg, path, list(failed))

    print_summary(cfg, path)
    print()
    if failed:
        credential = [n for n, r in failed.items() if not r]
        transient = [n for n, r in failed.items() if r]
        if credential:
            print(f"结论：{len(credential)} 项凭据未通过（{'、'.join(credential)}），请修好后重跑 verify。")
        if transient:
            print(f"结论：{len(transient)} 项未能确认（{'、'.join(transient)}），属网关临时故障，稍后重跑 verify 即可。")
        return 1
    print("结论：全部配置完成并验证通过。")
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="configure.py", description="sologsb-0917 本机凭据一键配置",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--config", help="配置文件路径（默认 ~/.codex/sologsb/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="交互式新增配置")
    p_init.add_argument("--from-current", action="store_true", help="从本机钥匙串/gh 导入现有凭据")
    p_init.add_argument("--non-interactive", action="store_true",
                        help="不提问，只用本机检测到的值和已有配置（无 TTY 时自动启用）")
    p_init.add_argument("--all", action="store_true",
                        help="逐项重问，包括已有值的项（默认只问还没有的项）")

    p_set = sub.add_parser("set", help="新增或覆盖单项")
    p_set.add_argument("pairs", nargs="+", metavar="KEY=VALUE")

    p_unset = sub.add_parser("unset", help="删除单项")
    p_unset.add_argument("keys", nargs="+", metavar="KEY")

    sub.add_parser("login", help="用账号密码登录并回写 token/cookie")
    p_verify = sub.add_parser("verify", help="联网验证凭据可用性")
    p_verify.add_argument("--only", nargs="+", choices=list(VERIFIERS), help="只验证指定项目")
    p_wizard = sub.add_parser("wizard", help="交互式向导：一次配完并验证所有项")
    p_wizard.add_argument("--only", nargs="+", choices=SECTION_ORDER,
                          help="只配置指定分组，可多选")
    sub.add_parser("show", help="查看配置（密钥脱敏）")
    sub.add_parser("path", help="打印配置文件路径")

    args = parser.parse_args(argv)
    path = dc.config_path(args.config)
    handler = {
        "init": cmd_init, "set": cmd_set, "unset": cmd_unset, "show": cmd_show,
        "login": cmd_login, "verify": cmd_verify, "path": cmd_path,
        "wizard": cmd_wizard,
    }[args.command]
    try:
        return handler(args, path)
    except dc.ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
