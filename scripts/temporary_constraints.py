#!/usr/bin/env python3
"""Expiring, self-pruning policy checks for the sologsb-0917 skill."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = SKILL_ROOT / "references" / "temporary-constraints.json"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _as_shanghai(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return _as_shanghai(parsed)


def _read_policy(policy_path: Path) -> dict:
    try:
        value = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _remove_marker_block(skill_root: Path, policy: dict) -> bool:
    documentation = policy.get("documentation") or {}
    relative_path = str(documentation.get("path") or "").strip()
    start_marker = str(documentation.get("startMarker") or "").strip()
    end_marker = str(documentation.get("endMarker") or "").strip()
    if not relative_path or not start_marker or not end_marker:
        return False
    target = (skill_root / relative_path).resolve()
    try:
        target.relative_to(skill_root.resolve())
    except ValueError:
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return False
    start = text.find(start_marker)
    if start < 0:
        return False
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        return False
    end += len(end_marker)
    replacement = text[:start].rstrip() + "\n\n" + text[end:].lstrip("\n")
    target.write_text(replacement.rstrip() + "\n", encoding="utf-8")
    return True


def prune_expired_policies(
    now: datetime | None = None,
    *,
    policy_path: Path | None = None,
    skill_root: Path | None = None,
) -> dict:
    """Delete expired temporary policy files and their documented SKILL.md blocks."""
    policy_file = (policy_path or POLICY_PATH).expanduser().resolve()
    policy = _read_policy(policy_file)
    if not policy:
        return {"removed": False, "reason": "missing_or_invalid", "policyPath": str(policy_file)}
    expires_at = _parse_time(str(policy.get("expiresAt") or ""))
    current = _as_shanghai(now)
    if current < expires_at:
        return {
            "removed": False,
            "reason": "not_expired",
            "policyId": str(policy.get("id") or ""),
            "expiresAt": expires_at.isoformat(),
        }
    if policy.get("selfDeleteOnExpiry") is not True:
        return {
            "removed": False,
            "reason": "self_delete_disabled",
            "policyId": str(policy.get("id") or ""),
            "expiresAt": expires_at.isoformat(),
        }
    root = (skill_root or SKILL_ROOT).expanduser().resolve()
    documentation_removed = _remove_marker_block(root, policy)
    try:
        policy_file.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return {
            "removed": False,
            "reason": f"policy_delete_failed: {exc}",
            "policyId": str(policy.get("id") or ""),
            "expiresAt": expires_at.isoformat(),
            "documentationRemoved": documentation_removed,
        }
    return {
        "removed": True,
        "reason": "expired",
        "policyId": str(policy.get("id") or ""),
        "expiresAt": expires_at.isoformat(),
        "documentationRemoved": documentation_removed,
        "policyPath": str(policy_file),
    }


def active_policy(
    now: datetime | None = None,
    *,
    policy_path: Path | None = None,
    skill_root: Path | None = None,
) -> dict | None:
    policy_file = (policy_path or POLICY_PATH).expanduser().resolve()
    policy = _read_policy(policy_file)
    if not policy:
        return None
    effective_from = _parse_time(str(policy.get("effectiveFrom") or ""))
    expires_at = _parse_time(str(policy.get("expiresAt") or ""))
    current = _as_shanghai(now)
    if current >= expires_at:
        prune_expired_policies(current, policy_path=policy_file, skill_root=skill_root)
        return None
    if current < effective_from:
        return None
    return policy


def evaluate_double_perfect_delivery(
    draft: dict,
    now: datetime | None = None,
    *,
    policy_path: Path | None = None,
    skill_root: Path | None = None,
) -> dict:
    """Return the active temporary A/B double-5 delivery rule decision."""
    policy = active_policy(now, policy_path=policy_path, skill_root=skill_root)
    delivery = draft.get("delivery") if isinstance(draft.get("delivery"), dict) else {}
    scores = {
        side: ((delivery.get(side) or {}).get("score") if isinstance(delivery.get(side), dict) else None)
        for side in ("A", "B")
    }
    active = policy is not None
    violation = bool(active and scores.get("A") == 5 and scores.get("B") == 5)
    return {
        "active": active,
        "policyId": str((policy or {}).get("id") or ""),
        "effectiveFrom": str((policy or {}).get("effectiveFrom") or ""),
        "expiresAt": str((policy or {}).get("expiresAt") or ""),
        "scores": scores,
        "violation": violation,
        "discardRequired": violation,
        "action": "discard-no-submit" if violation else "none",
        "message": (
            "临时约束命中：按真实产物评定后 A/B 交付完整性都是 5 分，任务必须丢弃，"
            "不得上传、排队或调用提交接口。"
            if violation
            else (
                "2026-09-29 临时约束生效：A/B 交付完整性不得凭主观偏好压低或抬高分数；"
                "真实结果若都为 5 分则丢弃任务。"
                if active
                else "当前没有生效的交付完整性临时约束。"
            )
        ),
    }
