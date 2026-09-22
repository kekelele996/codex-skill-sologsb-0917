#!/usr/bin/env python3
"""设备本地配置文件（明文）读写层。

设计约定：
- 配置文件在技能目录之外，覆盖技能包不会影响它；
- 文件权限固定 0600，目录 0700；
- 只保存本机专属信息：Claude Key / Base URL、Solo Manager 地址与账号、
  SOLO2 账号、GitHub Token；
- 读取优先级：命令行参数 > 环境变量 > 配置文件 > 代码默认值。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1

DEFAULT_CONFIG_PATH = Path.home() / ".codex" / "sologsb" / "config.json"

# 每一项：配置路径 -> (环境变量名, 代码默认值)
FIELDS: dict[str, tuple[str | None, str]] = {
    "claude.apiKey": ("SOLOSB_CLAUDE_KEY", ""),
    "claude.baseUrl": ("SOLOSB_ANTHROPIC_BASE_URL", "https://llm2.jzxhnh.com"),
    "claude.model": ("SOLOSB_MODEL", "auto_model/urm"),
    "claude.image": ("SOLOSB_DOCKER_IMAGE", "adminfather/benzhi-claude-code:20260916-toolchains-v2"),
    "claude.contextWindow": ("SOLOSB_CONTEXT_WINDOW", "1000000"),
    "claude.maxContainers": ("SOLOSB_MAX_CONTAINERS", "4"),
    "manager.baseUrl": ("SOLO_MANAGER_BASE_URL", "http://192.168.31.26:8080"),
    "manager.username": ("SOLO_MANAGER_USERNAME", ""),
    "manager.password": (None, ""),
    "manager.token": ("SOLO_MANAGER_TOKEN", ""),
    "solo2.baseUrl": ("SOLO2_SERVER", "https://solo2.jzxhnh.com"),
    "solo2.username": (None, ""),
    "solo2.password": (None, ""),
    "solo2.cookie": ("SOLO_QA_COOKIE", ""),
    "solo2.csrf": ("SOLO_QA_CSRF", ""),
    "github.token": ("GITHUB_TOKEN", ""),
    "github.username": ("SOLOSB_GITHUB_USERNAME", ""),
    "github.proxyHttp": ("SOLOSB_GITHUB_PROXY", "127.0.0.1:17890"),
    "github.proxySocks": (None, "127.0.0.1:17891"),
}

# 这些字段在 show / 日志里必须脱敏
SECRET_FIELDS = {
    "claude.apiKey",
    "manager.password",
    "manager.token",
    "solo2.password",
    "solo2.cookie",
    "solo2.csrf",
    "github.token",
}


class ConfigError(RuntimeError):
    pass


def config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("SOLOSB_CONFIG", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_CONFIG_PATH


def _empty_config() -> dict[str, Any]:
    return {"configVersion": CONFIG_VERSION}


def load(path: Path | None = None) -> dict[str, Any]:
    target = path or config_path()
    if not target.is_file():
        return _empty_config()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置文件无法解析: {target} ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {target}")
    data.setdefault("configVersion", CONFIG_VERSION)
    return data


def save(data: dict[str, Any], path: Path | None = None) -> Path:
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    data = dict(data)
    data.setdefault("configVersion", CONFIG_VERSION)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)
    return target


def get_path(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_path(data: dict[str, Any], dotted: str, value: Any) -> tuple[Any, Any]:
    """写入或覆盖一个点分路径，返回 (旧值, 新值)。"""
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    old = node.get(parts[-1])
    node[parts[-1]] = value
    return old, value


def delete_path(data: dict[str, Any], dotted: str) -> Any:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            return None
        node = child
    return node.pop(parts[-1], None)


def known_fields() -> list[str]:
    return sorted(FIELDS)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def resolve(dotted: str, *, cli: Any = None, path: Path | None = None) -> str:
    """按 命令行 > 环境变量 > 配置文件 > 默认值 解析一个字符串配置项。"""
    if cli is not None and _text(cli):
        return _text(cli)
    env_name, default = FIELDS.get(dotted, (None, ""))
    if env_name:
        env_value = _text(os.environ.get(env_name))
        if env_value:
            return env_value
    data = load(path)
    value = _text(get_path(data, dotted))
    if value:
        return value
    return default


def resolve_from(data: dict[str, Any], dotted: str, *, cli: Any = None) -> str:
    """对已加载的配置字典做完整优先级解析：命令行 > 环境变量 > 配置 > 默认值。"""
    if cli is not None and _text(cli):
        return _text(cli)
    env_name, default = FIELDS.get(dotted, (None, ""))
    value = _text(get_path(data, dotted))
    if value:
        return value
    if env_name:
        env_value = _text(os.environ.get(env_name))
        if env_value:
            return env_value
    return default


def mask(value: Any, *, keep: int = 4) -> str:
    text = _text(value)
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]}（长度 {len(text)}）"



# ---------------------------------------------------------------- SOLO2 会话

def _post_json(url: str, body: dict, *, headers: dict[str, str] | None = None,
               timeout: float = 20):
    import urllib.request

    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            headers_out = dict(response.headers)
            # 同一个响应可能有多个 Set-Cookie，dict() 只会保留一个，必须单独收集
            all_cookies = response.headers.get_all("Set-Cookie") or []
            if all_cookies:
                headers_out["Set-Cookie"] = list(all_cookies)
            return response.status, json.loads(response.read().decode("utf-8", "replace") or "{}"), headers_out
    except Exception as exc:  # HTTPError 也带 headers/body
        status = getattr(exc, "code", 0)
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            raw = str(exc)
        headers_out = dict(getattr(exc, "headers", {}) or {})
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return status, payload, headers_out


def _parse_cookies(headers: dict, payload: dict | None = None) -> tuple[str, str]:
    """从响应头里提取 Cookie 串与 CSRF 值。

    兼容两种来源：多个 Set-Cookie 头，以及响应体里的 csrf_token 字段。
    """
    raw = headers.get("Set-Cookie") or headers.get("set-cookie") or []
    if isinstance(raw, str):
        raw = [raw]
    pairs: list[str] = []
    csrf = ""
    for item in raw:
        for chunk in str(item).split(","):
            piece = chunk.split(";")[0].strip()
            if "=" not in piece:
                continue
            name, _, value = piece.partition("=")
            name, value = name.strip(), value.strip()
            pairs.append(f"{name}={value}")
            if name == "solo_qa_csrf":
                csrf = value
    if not csrf and isinstance(payload, dict):
        csrf = str(payload.get("csrf_token") or "").strip()
    return "; ".join(pairs), csrf


def refresh_solo2_session(path: Path | None = None, *, write_back: bool = True) -> tuple[str, str]:
    """用配置里的账号密码重新登录 SOLO2，返回 (cookie, csrf)。

    成功后默认把新会话写回配置文件，并同步当前进程的环境变量。
    配置里没有账号密码时抛 ConfigError。
    """
    target = path or config_path()
    data = load(target)
    base_url = (resolve_from(data, "solo2.baseUrl") or "").rstrip("/")
    username = resolve_from(data, "solo2.username")
    password = resolve_from(data, "solo2.password")
    if not base_url or not username or not password:
        raise ConfigError("配置里缺少 solo2.baseUrl / solo2.username / solo2.password，无法自动登录")

    status, payload, headers = _post_json(
        f"{base_url}/api/v1/auth/login",
        {"username": username, "password": password},
        headers={"Origin": base_url},
    )
    if status != 200:
        detail = payload.get("detail") or payload.get("message") or str(payload)[:160]
        raise ConfigError(f"SOLO2 登录失败：HTTP {status} {detail}")

    cookie, csrf = _parse_cookies(headers, payload)
    if not cookie:
        raise ConfigError("SOLO2 登录成功但响应没有 Set-Cookie，无法保存会话")

    if write_back:
        set_path(data, "solo2.cookie", cookie)
        if csrf:
            set_path(data, "solo2.csrf", csrf)
        save(data, target)
    os.environ["SOLO_QA_COOKIE"] = cookie
    if csrf:
        os.environ["SOLO_QA_CSRF"] = csrf
    return cookie, csrf


def refresh_solo2_into_env(path: Path | None = None) -> bool:
    """供读取路径在 401 后调用：重登成功返回 True，失败静默返回 False。"""
    try:
        refresh_solo2_session(path)
        return True
    except Exception:
        return False


def ensure_solo2_session(path: Path | None = None, *, validate: bool = False) -> tuple[str, str]:
    """确保当前进程有一份 SOLO2 会话；缺失时自动登录。

    validate=True 时会先访问 /auth/me 校验，失效则重登（用于写操作前）。
    """
    import urllib.request

    target = path or config_path()
    data = load(target)
    cookie = resolve_from(data, "solo2.cookie")
    csrf = resolve_from(data, "solo2.csrf")

    if cookie and validate:
        base_url = (resolve_from(data, "solo2.baseUrl") or "").rstrip("/")
        request = urllib.request.Request(f"{base_url}/api/v1/auth/me")
        request.add_header("Cookie", cookie)
        if csrf:
            request.add_header("X-CSRF-Token", csrf)
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                if response.status == 200:
                    return cookie, csrf
        except Exception:
            pass
        cookie = ""

    if cookie and csrf:
        return cookie, csrf
    return refresh_solo2_session(target)

def apply_to_env(*, override: bool = False, path: Path | None = None,
                 auto_login: bool = True) -> dict[str, str]:
    """把配置文件注入标准环境变量，供技能其余代码按既有方式读取。

    默认不覆盖已存在的环境变量，因此优先级天然是：
    命令行参数 > 环境变量 > 配置文件 > 代码默认值。

    返回 {环境变量名: "set" | "kept" | "skipped"}。
    """
    data = load(path)
    applied: dict[str, str] = {}
    for dotted, (env_name, _default) in FIELDS.items():
        if not env_name:
            continue
        value = _text(get_path(data, dotted))
        if not value:
            applied[env_name] = "skipped"
            continue
        if dotted == "github.proxyHttp" and "://" not in value:
            value = f"http://{value}"
        if _text(os.environ.get(env_name)) and not override:
            applied[env_name] = "kept"
            continue
        os.environ[env_name] = value
        applied[env_name] = "set"

    if auto_login and not os.environ.get("SOLO_QA_COOKIE", "").strip():
        # 配置里有账号密码但会话缺失时自动登录一次，避免每次手工换 Cookie。
        if refresh_solo2_into_env(path):
            applied["SOLO_QA_COOKIE"] = "set"
            applied["SOLO_QA_CSRF"] = "set"
    return applied


def load_and_apply(path: Path | None = None) -> dict[str, str]:
    """技能入口统一调用：读配置并注入环境变量，失败时静默返回。"""
    try:
        return apply_to_env(path=path)
    except Exception:
        return {}


def snapshot(path: Path | None = None) -> dict[str, Any]:
    """读取全部已知字段的最终生效值（含默认值），用于展示。"""
    data = load(path)
    result: dict[str, Any] = {}
    for dotted in known_fields():
        env_name, default = FIELDS[dotted]
        value = _text(get_path(data, dotted))
        source = "config" if value else ""
        if not value and env_name:
            env_value = _text(os.environ.get(env_name))
            if env_value:
                value, source = env_value, "env"
        if not value and default:
            value, source = default, "default"
        result[dotted] = {
            "value": mask(value) if dotted in SECRET_FIELDS else value,
            "configured": bool(value),
            "source": source if value else "missing",
            "secret": dotted in SECRET_FIELDS,
        }
    return result
