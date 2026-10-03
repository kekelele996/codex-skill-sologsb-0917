#!/usr/bin/env python3
"""A/B 模型的唯一来源与“确实调用了该模型”的轨迹核对。

执行器（side_runner）和提交预检（submission/scripts/preflight.py）共用这里的判定，
两处口径不会分叉：

- ``resolve_models``：A/B 模型名以设备配置文件 ``claude.modelA`` / ``claude.modelB``
  为准（调度台“A / B 模型”写的就是这个文件）；环境变量只在文件没写时回退，
  与文件不一致时忽略环境变量并记下来。文件存在却读不了时直接报错，
  不静默退回默认模型。
- ``ModelCallAudit``：以网关实际回应为准——每条 assistant 的 ``message.model``
  与 result 的 ``modelUsage``——而不是 init 事件里客户端回显的 ANTHROPIC_MODEL。
- ``resolve_b_alternate`` / ``alternating_uses_a_model``：B 侧两个模型交替尝试的
  开关与“第几次尝试该跑 A 侧模型”的唯一口径，执行器和提交预检共用。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import device_config
from common import utc_now

DEFAULT_MODELS = {
    "A": device_config.FIELDS["claude.modelA"][1],
    "B": device_config.FIELDS["claude.modelB"][1],
}
MODEL_FIELDS = {"A": "claude.modelA", "B": "claude.modelB"}
B_ALTERNATE_FIELD = "claude.bAlternateEnabled"
# 2026-10-03 早先一版把开关放在这个字段上，读不到新字段时按旧字段兜底。
LEGACY_B_FALLBACK_FIELD = "claude.bFallbackEnabled"
_TRUTHY = {"1", "true", "yes", "on", "enabled", "enable", "开", "开启", "打开"}
# 容器启动后最先写的就是 system/init 事件；只读文件头部即可拿到回显。
MODEL_AUDIT_READ_BYTES = 256 * 1024


class ModelConfigError(RuntimeError):
    pass


def resolve_models(path: Path | None = None) -> dict[str, Any]:
    """当前应使用的 A/B 模型及各自来源（config / env / default）。"""
    target = Path(path) if path is not None else device_config.config_path()
    try:
        data = device_config.load(target)
    except device_config.ConfigError as exc:
        raise ModelConfigError(f"设备配置无法读取，拒绝用默认模型顶替：{exc}") from exc
    models: dict[str, str] = {}
    sources: dict[str, str] = {}
    ignored_env: dict[str, str] = {}
    for side, dotted in MODEL_FIELDS.items():
        env_name = device_config.FIELDS[dotted][0] or ""
        from_file = str(device_config.get_path(data, dotted) or "").strip()
        from_env = str(os.environ.get(env_name, "") or "").strip() if env_name else ""
        if from_file:
            models[side], sources[side] = from_file, "config"
            if from_env and from_env != from_file:
                ignored_env[env_name] = from_env
        elif from_env:
            models[side], sources[side] = from_env, "env"
        else:
            models[side], sources[side] = DEFAULT_MODELS[side], "default"
    return {
        "A": models["A"],
        "B": models["B"],
        "sources": sources,
        "configPath": str(target),
        "ignoredEnv": ignored_env,
    }


def _resolve_text(data: dict[str, Any], dotted: str, default: str = "") -> tuple[str, str]:
    """设备配置里的一项及其来源：config > env > default，与 resolve_models 同口径。"""
    env_name, code_default = device_config.FIELDS.get(dotted, (None, ""))
    from_file = str(device_config.get_path(data, dotted) or "").strip()
    if from_file:
        return from_file, "config"
    from_env = str(os.environ.get(env_name, "") or "").strip() if env_name else ""
    if from_env:
        return from_env, "env"
    return str(default if default != "" else code_default), "default"


def resolve_b_alternate(path: Path | None = None) -> dict[str, Any]:
    """B 侧交替开关：打开后 B 侧两个模型交替尝试。

    奇数次尝试跑 ``claude.modelB``，偶数次跑 ``claude.modelA``；只有一个布尔开关，
    不额外配置模型名。关闭时行为与改动前完全一致（B 侧只用 ``claude.modelB``）。
    """
    target = Path(path) if path is not None else device_config.config_path()
    try:
        data = device_config.load(target)
    except device_config.ConfigError as exc:
        raise ModelConfigError(f"设备配置无法读取，拒绝用默认交替开关顶替：{exc}") from exc
    enabled_text, enabled_source = _resolve_text(data, B_ALTERNATE_FIELD, "")
    if enabled_source == "default":
        # 新字段完全没写（配置和环境变量都没有）时才看早先一版的字段名。
        enabled_text, enabled_source = _resolve_text(data, LEGACY_B_FALLBACK_FIELD, "0")
    return {
        "enabled": enabled_text.strip().lower() in _TRUTHY,
        "source": {"enabled": enabled_source},
    }


def normalize_b_alternate(fallback: Any) -> dict[str, Any]:
    """把任意来源（state.modelPlan.bAlternate）归一成一个只读开关字典。"""
    data = fallback if isinstance(fallback, dict) else {}
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {
        "enabled": bool(data.get("enabled")),
        "source": {"enabled": str(source.get("enabled") or "")},
    }


def b_alternate_policy(fallback: Any) -> dict[str, Any]:
    """只保留开关状态，用于比较“任务锁定策略”与“当前设备配置”。"""
    return {"enabled": normalize_b_alternate(fallback)["enabled"]}


def alternating_uses_a_model(fallback: Any, attempt: int) -> bool:
    """这一次尝试该不该跑 A 侧模型。

    开关打开时，第 1、2 次先用 A 侧模型；从第 3 次起两个模型交替——
    第 3 次跑 B 侧模型、第 4 次跑 A 侧模型、第 5 次回到 B 侧模型，如此往复。
    开关关闭时恒为 False（B 侧只跑 ``claude.modelB``）。
    """
    if not normalize_b_alternate(fallback)["enabled"]:
        return False
    try:
        number = int(attempt)
    except (TypeError, ValueError):
        return False
    if number < 1:
        return False
    if number <= 2:
        return True
    return number % 2 == 0


def trace_init_model(path: Path | None) -> str:
    """轨迹里 system/init 事件上报的模型名；还没有该事件时返回空串。"""
    if path is None or not path.is_file():
        return ""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MODEL_AUDIT_READ_BYTES)
    except OSError:
        return ""
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") != "system" or str(event.get("subtype") or "") != "init":
            continue
        return str(event.get("model") or "").strip()
    return ""


# Claude Code 自己合成的提示消息（如 API 错误说明）不是一次模型调用。
SYNTHETIC_MODELS = {"<synthetic>"}


class ModelCallAudit:
    """核对候选容器实际调用的模型是否等于本次配置的模型。

    system/init 里的 model 只是客户端回显 ANTHROPIC_MODEL，证明不了网关用了
    哪个模型；真正的证据是每条 assistant 事件的 message.model（API 响应的
    模型名）和 result 事件的 modelUsage（本轮用到的全部模型，含后台小任务）。
    轨迹边长边读：第一条响应一出现就核对，不一致立刻掐断。
    """

    def __init__(self, *, expected: str, stdout_path: Path):
        self.expected = str(expected or "").strip()
        self.stdout_path = stdout_path
        self.offset = 0
        self._pending = b""
        self.init_model = ""
        self.response_models: dict[str, int] = {}
        self.responses_without_model = 0
        self.usage_models: list[str] = []
        self.result_seen = False
        self.violation: dict[str, Any] | None = None

    def _violate(self, observed: str, source: str, detail: str = "") -> dict[str, Any]:
        self.violation = {
            "expected": self.expected,
            "observed": observed,
            "source": source,
            "detail": detail,
            "foundAt": utc_now(),
        }
        return self.violation

    def _event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "system" and str(event.get("subtype") or "") == "init":
            self.init_model = str(event.get("model") or "").strip()
            if self.init_model and self.init_model != self.expected:
                self._violate(self.init_model, "轨迹 init")
        elif kind == "assistant":
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            model = str(message.get("model") or "").strip()
            if model in SYNTHETIC_MODELS:
                return
            if not model:
                self.responses_without_model += 1
                return
            self.response_models[model] = self.response_models.get(model, 0) + 1
            if model != self.expected:
                self._violate(model, "API 响应", str(message.get("id") or ""))
        elif kind == "result":
            self.result_seen = True
            usage = event.get("modelUsage") if isinstance(event.get("modelUsage"), dict) else {}
            self.usage_models = sorted(str(name) for name in usage if str(name) not in SYNTHETIC_MODELS)
            others = [name for name in self.usage_models if name != self.expected]
            if others:
                self._violate("、".join(others), "modelUsage")

    def _consume(self, data: bytes) -> None:
        for line in data.splitlines():
            if self.violation is not None:
                return
            if not line.strip():
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(event, dict):
                self._event(event)

    def check(self) -> dict[str, Any] | None:
        """读新增的完整行并核对；返回第一处不一致。"""
        if self.violation is not None or not self.expected:
            return self.violation
        try:
            with self.stdout_path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return None
        self.offset += len(chunk)
        data = self._pending + chunk
        cut = data.rfind(b"\n") + 1
        self._pending = data[cut:]
        self._consume(data[:cut])
        return self.violation

    def finalize(self, *, completed: bool) -> dict[str, Any] | None:
        """进程结束后收尾：读完剩余内容；正常结束却没有任何响应模型名时同样判失败。"""
        if self.violation is not None or not self.expected:
            return self.violation
        self.check()
        if self.violation is None and self._pending:
            self._consume(self._pending)
            self._pending = b""
        if self.violation is None and completed and not self.response_models:
            self._violate("", "API 响应", "轨迹里没有任何带模型名的 API 响应，无法证明实际调用的模型")
        return self.violation

    def summary(self) -> dict[str, Any]:
        return {
            "expected": self.expected,
            "initModel": self.init_model,
            "responseModels": dict(self.response_models),
            "responsesWithoutModel": self.responses_without_model,
            "usageModels": list(self.usage_models),
            "resultSeen": self.result_seen,
            "ok": self.violation is None,
        }


def audit_trace_models(path: Path | None, expected: str) -> dict[str, Any]:
    """事后复核一份完整轨迹（stream-json 或原生会话 JSONL 均可）。"""
    target = Path(path) if path else None
    if target is None or not target.is_file():
        return {"ok": False, "expected": expected, "error": f"轨迹不存在：{path}", "violation": None}
    audit = ModelCallAudit(expected=expected, stdout_path=target)
    violation = audit.finalize(completed=True)
    return {**audit.summary(), "path": str(target), "violation": violation, "error": ""}
