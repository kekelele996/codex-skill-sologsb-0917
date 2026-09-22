#!/usr/bin/env python3
"""Ingest source from a local directory, task package, or Solo Manager."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from common import (
    SOLO_SCRIPTS,
    SologsbError,
    copy_tree,
    extract_zip_safe,
    find_zip_source_root,
)
from project_claims import (
    claimed_project_codes,
    platform_selection_lock,
    release_project_claim,
    running_container_project_codes,
    start_project_claim,
)


def _load_platform_bridge():
    if str(SOLO_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SOLO_SCRIPTS))
    try:
        import platform_bridge  # type: ignore
    except Exception as exc:  # pragma: no cover - environment-specific
        raise SologsbError(f"无法加载 Solo Manager 适配器: {exc}") from exc
    return platform_bridge


def _usable_variant(project: dict[str, Any]) -> dict[str, Any] | None:
    readiness = str(project.get("readinessStatus") or "").strip().upper()
    if project.get("disabled") or (readiness and readiness != "RUNNABLE"):
        return None
    variants = [
        item
        for item in (project.get("variants") or [])
        if isinstance(item, dict)
        and item.get("sourceAvailable")
        and item.get("sourceAsset")
    ]
    variants.sort(key=lambda item: str(item.get("directoryName") or item.get("id") or ""))
    return variants[0] if variants else None


def _project_code(project: dict[str, Any]) -> str:
    return str(project.get("code") or "").strip()


def _select_platform_project(
    pb: Any,
    meta: dict[str, str],
    token: str,
    *,
    project_code: str,
    project_id: str,
    active_codes: set[str],
    active_source: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if project_id:
        project = pb.api_json(
            meta,
            "/projects/" + pb.urllib.parse.quote(project_id, safe=""),
            token=token,
        )
        if not isinstance(project, dict) or not project:
            raise SologsbError(f"平台没有找到项目 ID: {project_id}")
        code = _project_code(project) or project_id
        if code.casefold() in active_codes:
            raise SologsbError(
                f"项目 {code} 已被其他会话占用或正在执行（{active_source} 判定），拒绝选取"
            )
        variant = _usable_variant(project)
        if variant is None:
            raise SologsbError(f"项目 {code} 不可选用或缺少源码快照")
        return project, variant, {
            "stage": "指定项目 ID",
            "excludedRunning": [],
            "runningContainerSource": active_source,
        }

    excluded: list[str] = []
    errors: list[str] = []
    matched_explicit = False
    seen: set[str] = set()
    for stage, path in (
        ("我的项目", "/projects/mine?page=1&size=200"),
        ("项目池", "/projects?page=1&size=200"),
    ):
        try:
            payload = pb.api_json(meta, path, token=token)
        except Exception as exc:
            errors.append(f"{stage}: {exc}")
            continue
        for project in payload.get("items") or []:
            if not isinstance(project, dict):
                continue
            code = _project_code(project)
            identity = code.casefold() or str(project.get("id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            if project_code and code.casefold() != project_code.casefold():
                continue
            matched_explicit = bool(project_code)
            if code and code.casefold() in active_codes:
                excluded.append(code)
                if project_code:
                    raise SologsbError(
                        f"项目 {project_code} 已被其他会话占用或正在执行"
                        f"（{active_source} 判定），拒绝选取"
                    )
                continue
            variant = _usable_variant(project)
            if variant is None:
                continue
            return project, variant, {
                "stage": stage,
                "excludedRunning": excluded,
                "runningContainerSource": active_source,
            }
        if project_code and matched_explicit:
            raise SologsbError(f"平台找到项目 {project_code}，但没有可用源码快照或项目不可运行")
    target = project_code or "当前筛选条件"
    skipped = f"; 已跳过占用中项目: {', '.join(excluded[:20])}" if excluded else ""
    detail = f"; 查询失败: {' | '.join(errors[:3])}" if errors and not seen else ""
    raise SologsbError(f"没有找到可选用项目: {target}{skipped}{detail}")


def ingest_source(
    origin: Path,
    *,
    source: Path | None = None,
    package: Path | None = None,
    from_platform: bool = False,
    project_code: str = "",
    project_id: str = "",
    task_type: str = "0-1代码生成",
    platform_base_url: str = "",
    workdir: Path | None = None,
    task_root: Path | None = None,
) -> dict[str, Any]:
    if sum(bool(value) for value in (source, package, from_platform)) != 1:
        raise SologsbError("必须且只能指定 --source、--package、--from-platform 之一")

    if source:
        source = source.expanduser().resolve()
        if source.is_file() and source.suffix.lower() == ".zip":
            return ingest_source(origin, package=source)
        if not source.is_dir():
            raise SologsbError(f"本地源码目录不存在: {source}")
        count = copy_tree(source, origin, overwrite=True)
        result = {"mode": "source", "sourcePath": str(source), "fileCount": count}
        return result

    if package:
        package = package.expanduser().resolve()
        if not package.is_file():
            raise SologsbError(f"任务包不存在: {package}")
        with tempfile.TemporaryDirectory(prefix="sologsb-package-") as temp:
            extracted = Path(temp) / "extracted"
            extract_zip_safe(package, extracted)
            source_root = find_zip_source_root(extracted)
            count = copy_tree(source_root, origin, overwrite=True)
        return {"mode": "package", "packagePath": str(package), "fileCount": count}

    pb = _load_platform_bridge()
    base_url = (platform_base_url or os.environ.get("SOLO_MANAGER_BASE_URL", "")
                or "http://192.0.2.10:8080").rstrip("/")
    meta = {"baseUrl": base_url, "apiBaseUrl": base_url + "/api/v1"}
    token = pb.load_manager_token()
    root = (task_root or origin.parents[1]).expanduser().resolve()
    with platform_selection_lock(base_url):
        running_codes, running_source = running_container_project_codes(workdir)
        claim_codes = claimed_project_codes(base_url)
        active_codes = running_codes | claim_codes
        if claim_codes:
            running_source = f"{running_source} + 项目占用锁"
        active_source = running_source or "项目锁与容器探针"
        project, variant, origin_info = _select_platform_project(
            pb,
            meta,
            token,
            project_code=project_code,
            project_id=project_id,
            active_codes=active_codes,
            active_source=active_source,
        )
        code = _project_code(project)
        claim = start_project_claim(
            root,
            base_url,
            code,
            project_name=str(project.get("name") or ""),
        )

    try:
        with tempfile.TemporaryDirectory(prefix="sologsb-platform-") as temp:
            package_path = Path(temp) / "source-package.zip"
            request = (
                "/projects/"
                + pb.urllib.parse.quote(str(project["id"]), safe="")
                + "/variants/"
                + pb.urllib.parse.quote(str(variant["id"]), safe="")
                + "/source-package?"
                + pb.urllib.parse.urlencode({"rootTaskType": task_type})
            )
            pb.api_download(meta, request, package_path, token)
            extracted = Path(temp) / "extracted"
            extract_zip_safe(package_path, extracted)
            source_root = find_zip_source_root(extracted)
            count = copy_tree(source_root, origin, overwrite=True)
    except Exception:
        release_project_claim(root)
        raise

    selection = {
        "projectId": project.get("id", ""),
        "projectCode": code,
        "projectName": project.get("name", ""),
        "businessDomain": project.get("businessDomain", ""),
        "category": project.get("category", ""),
        "readinessStatus": project.get("readinessStatus", ""),
        "variantId": variant.get("id", ""),
        "variantName": variant.get("directoryName", ""),
        "taskType": task_type,
        "candidateStage": origin_info.get("stage", ""),
        "excludedRunningProjects": origin_info.get("excludedRunning") or [],
        "runningContainerSource": origin_info.get("runningContainerSource", ""),
        "baseUrl": base_url,
        "apiBaseUrl": meta["apiBaseUrl"],
    }
    return {
        "mode": "platform",
        **selection,
        "platformClaim": claim,
        "platformSelection": selection,
        "fileCount": count,
    }
