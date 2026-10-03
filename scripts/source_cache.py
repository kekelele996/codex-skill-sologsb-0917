#!/usr/bin/env python3
"""Local source-package cache for sologsb-0917.

Keeps a copy of every Solo Manager source package on this machine so later tasks
can ingest sources locally first and only fall back to the Manager when the
package is missing.  Packages are keyed by ``projectCode | variantId | taskType``
because the Manager tailors the archive to the root task type.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from common import (
    CODEX_HOME,
    SologsbError,
    read_json,
    sha256_bytes,
    utc_now,
    write_json,
)

CACHE_ROOT = CODEX_HOME / "sologsb-0917" / "source-cache"
MANIFEST_PATH = CACHE_ROOT / "manifest.json"
PACKAGES_DIR = CACHE_ROOT / "packages"
DEFAULT_TASK_TYPES = ("0-1代码生成", "feature迭代")


def _manifest() -> dict[str, Any]:
    data = read_json(MANIFEST_PATH, {}) or {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("schemaVersion", 1)
    entries = data.get("entries")
    if not isinstance(entries, dict):
        data["entries"] = {}
    return data


def _save_manifest(data: dict[str, Any]) -> None:
    data["updatedAt"] = utc_now()
    write_json(MANIFEST_PATH, data)


def entry_key(project_code: str, variant_id: str, task_type: str) -> str:
    return f"{project_code}|{variant_id}|{task_type}"


def lookup(*, project_code: str = "", project_id: str = "", task_type: str = "") -> dict[str, Any] | None:
    """Return the cached entry for a project/variant/task type, or None."""
    code = str(project_code or "").strip()
    pid = str(project_id or "").strip()
    if not code and not pid:
        return None
    data = _manifest()
    entries = data.get("entries") or {}
    candidates: list[dict[str, Any]] = []
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        if code and str(entry.get("projectCode") or "").casefold() != code.casefold():
            continue
        if pid and str(entry.get("projectId") or "") != pid:
            continue
        if task_type and str(entry.get("taskType") or "") != task_type:
            continue
        if entry.get("packagePath") and Path(str(entry["packagePath"])).is_file():
            candidates.append(entry)
    if not candidates:
        return None
    candidates.sort(key=lambda item: str(item.get("downloadedAt") or ""), reverse=True)
    return candidates[0]


def _package_target(project_code: str, variant_id: str, task_type: str) -> Path:
    folder = PACKAGES_DIR / (project_code or "unknown")
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{variant_id}--{task_type}.zip"


def store_package(
    *,
    project: dict[str, Any],
    variant: dict[str, Any],
    task_type: str,
    payload: bytes,
    base_url: str = "",
) -> dict[str, Any]:
    code = str(project.get("code") or "").strip()
    variant_id = str(variant.get("id") or "").strip()
    if not code or not variant_id:
        raise SologsbError("缓存源码包需要 projectCode 与 variantId")
    target = _package_target(code, variant_id, task_type)
    target.write_bytes(payload)
    asset = variant.get("sourceAsset") if isinstance(variant.get("sourceAsset"), dict) else {}
    entry = {
        "projectId": str(project.get("id") or ""),
        "projectCode": code,
        "projectName": str(project.get("name") or ""),
        "businessDomain": str(project.get("businessDomain") or ""),
        "category": str(project.get("category") or ""),
        "readinessStatus": str(project.get("readinessStatus") or ""),
        "variantId": variant_id,
        "variantName": str(variant.get("directoryName") or ""),
        "languages": str(variant.get("languages") or ""),
        "containerized": bool(variant.get("containerized")),
        "sourceAssetSha256": str(asset.get("sha256") or ""),
        "sourceAssetSizeBytes": int(asset.get("sizeBytes") or 0),
        "taskType": task_type,
        "packagePath": str(target.resolve()),
        "packageSha256": sha256_bytes(payload),
        "packageSizeBytes": len(payload),
        "baseUrl": base_url,
        "downloadedAt": utc_now(),
    }
    data = _manifest()
    data["entries"][entry_key(code, variant_id, task_type)] = entry
    _save_manifest(data)
    return entry


# ------------------------------------------------------------------ manager --

def _bridge():
    from source_ingest import _load_platform_bridge  # local import: avoid cycles

    return _load_platform_bridge()


def _manager_token(pb: Any) -> str:
    """Device config first — never fall back to the macOS keychain."""
    try:
        import device_config as dc

        token = dc.resolve("manager.token")
        if token:
            return token
    except Exception:
        pass
    return pb.load_manager_token()


def _manager_connection(base_url: str = "") -> tuple[Any, dict[str, str], str, str]:
    pb = _bridge()
    try:
        import device_config as dc

        resolved = base_url or dc.resolve("manager.baseUrl")
    except Exception:
        resolved = base_url
    resolved = (resolved or "").rstrip("/")
    if not resolved:
        raise SologsbError("缺少 Solo Manager 地址")
    meta = {"baseUrl": resolved, "apiBaseUrl": resolved + "/api/v1"}
    token = _manager_token(pb)
    return pb, meta, token, resolved


def _list_mine(pb: Any, meta: dict[str, str], token: str) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, 6):
        payload = pb.api_json(meta, f"/projects/mine?page={page}&size=200", token=token)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            break
        for project in items:
            if not isinstance(project, dict):
                continue
            identity = str(project.get("code") or project.get("id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            projects.append(project)
        total = int(payload.get("total") or 0)
        if len(projects) >= total:
            break
    return projects


def _usable_variants(project: dict[str, Any]) -> list[dict[str, Any]]:
    variants = [
        item
        for item in (project.get("variants") or [])
        if isinstance(item, dict) and item.get("sourceAvailable") and item.get("sourceAsset")
    ]
    variants.sort(key=lambda item: str(item.get("directoryName") or item.get("id") or ""))
    return variants


def sync(
    *,
    project_code: str = "",
    task_types: tuple[str, ...] = DEFAULT_TASK_TYPES,
    base_url: str = "",
    force: bool = False,
) -> dict[str, Any]:
    pb, meta, token, resolved = _manager_connection(base_url)
    projects = _list_mine(pb, meta, token)
    if project_code:
        wanted = project_code.casefold()
        projects = [p for p in projects if str(p.get("code") or "").casefold() == wanted]
        if not projects:
            raise SologsbError(f"平台上没有找到项目 {project_code}（或它不在我的项目列表里）")
    stored: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for project in projects:
        code = str(project.get("code") or "")
        for variant in _usable_variants(project):
            variant_id = str(variant.get("id") or "")
            for task_type in task_types:
                existing = lookup(project_code=code, task_type=task_type)
                if (
                    existing
                    and str(existing.get("variantId") or "") == variant_id
                    and not force
                ):
                    skipped.append({"projectCode": code, "variantId": variant_id, "taskType": task_type,
                                    "reason": "already-cached"})
                    continue
                request = (
                    "/projects/"
                    + urllib.parse.quote(str(project.get("id") or ""), safe="")
                    + "/variants/"
                    + urllib.parse.quote(variant_id, safe="")
                    + "/source-package?"
                    + urllib.parse.urlencode({"rootTaskType": task_type})
                )
                try:
                    with tempfile.TemporaryDirectory(prefix="sologsb-cache-") as temp:
                        target = Path(temp) / "source-package.zip"
                        pb.api_download(meta, request, target, token)
                        payload = target.read_bytes()
                    entry = store_package(
                        project=project,
                        variant=variant,
                        task_type=task_type,
                        payload=payload,
                        base_url=resolved,
                    )
                    stored.append(entry)
                except Exception as exc:  # keep going: one bad variant must not stop the sync
                    failed.append({"projectCode": code, "variantId": variant_id, "taskType": task_type,
                                   "error": str(exc)[:200]})
    return {
        "status": "ok" if not failed else "partial",
        "baseUrl": resolved,
        "projects": len(projects),
        "stored": len(stored),
        "skipped": len(skipped),
        "failed": failed[:20],
        "manifestPath": str(MANIFEST_PATH.resolve()),
        "entries": len((_manifest().get("entries") or {})),
    }


def cache_root() -> Path:
    return CACHE_ROOT


def summary() -> dict[str, Any]:
    entries = (_manifest().get("entries") or {})
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for key, entry in sorted(entries.items()):
        if not isinstance(entry, dict):
            continue
        path = Path(str(entry.get("packagePath") or ""))
        if not path.is_file():
            missing.append(key)
            continue
        rows.append({
            "key": key,
            "projectCode": entry.get("projectCode"),
            "taskType": entry.get("taskType"),
            "variantId": entry.get("variantId"),
            "sizeBytes": entry.get("packageSizeBytes"),
            "downloadedAt": entry.get("downloadedAt"),
        })
    return {
        "cacheRoot": str(CACHE_ROOT.resolve()),
        "manifestPath": str(MANIFEST_PATH.resolve()),
        "entries": len(entries),
        "usable": len(rows),
        "missingPackages": missing,
        "items": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sologsb-0917 本地源码包缓存")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="从 Solo Manager 批量下载源码包到本地")
    p_sync.add_argument("--project-code", default="")
    p_sync.add_argument("--task-types", default=",".join(DEFAULT_TASK_TYPES))
    p_sync.add_argument("--base-url", default="")
    p_sync.add_argument("--force", action="store_true")

    p_path = sub.add_parser("path", help="查询某个项目/任务类型的本地源码包路径")
    p_path.add_argument("--project-code", default="")
    p_path.add_argument("--project-id", default="")
    p_path.add_argument("--task-type", default="")

    sub.add_parser("list", help="列出本地缓存")

    args = parser.parse_args(argv)
    if args.command == "sync":
        task_types = tuple(
            item.strip() for item in str(args.task_types or "").split(",") if item.strip()
        ) or DEFAULT_TASK_TYPES
        print(json.dumps(
            sync(project_code=args.project_code, task_types=task_types,
                 base_url=args.base_url, force=args.force),
            ensure_ascii=False, indent=2,
        ))
        return 0
    if args.command == "path":
        entry = lookup(project_code=args.project_code, project_id=args.project_id,
                       task_type=args.task_type)
        if not entry:
            print(json.dumps({"status": "miss", "cacheRoot": str(CACHE_ROOT.resolve())},
                             ensure_ascii=False, indent=2))
            return 1
        print(json.dumps({"status": "hit", **entry}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "list":
        print(json.dumps(summary(), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
