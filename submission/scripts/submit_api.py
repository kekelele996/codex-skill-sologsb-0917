#!/usr/bin/env python3
"""Audit and submit an existing SOLO2 GSB bundle through the HTTP API.

The browser form is intentionally not used. The default mode is a dry run. A
fully passing audit is approved automatically as the configured approver. Only a
change-volume-line-gate failure may wait for a matching approval file;
all other audit failures remain blocking and cannot be bypassed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

# --- 设备配置注入：必须在下列模块读取环境变量之前执行 ---
for _parent in Path(__file__).resolve().parents:
    if (_parent / "scripts" / "device_config.py").is_file():
        sys.path.insert(0, str(_parent / "scripts"))
        break
import device_config as _device_config  # noqa: E402

_device_config.load_and_apply()

DEFAULT_SERVER = os.environ.get("SOLO2_SERVER", "").strip().rstrip("/")
DEFAULT_KEYCHAIN_SERVICE = os.environ.get("SOLOSB_SOLO2_KEYCHAIN_SERVICE", "").strip()
TRACE_FIELDS = {"a_trace_file", "b_trace_file"}
VIDEO_FIELDS = {"a_screencast", "b_screencast"}
UPLOAD_LABELS = {
    "A-轨迹文件": "a_trace_file",
    "A-运行录屏": "a_screencast",
    "B-轨迹文件": "b_trace_file",
    "B-运行录屏": "b_screencast",
}
TERMINAL_STATUS_HINTS = {
    "QC_PASSED",
    "QC_REJECTED",
    "QC_DISCARDED",
    "QC_FAILED",
    "CASCADE_DISCARDED",
    "PENDING_FIX",
}
PASS_STATUSES = {"QC_PASSED", "PASSED"}
EXCLUDED_SUBMISSION_FIELDS = {"remark"}
CHANGE_VOLUME_APPROVAL_SCOPE = "change-volume-line-gate"
AUTO_APPROVER = os.environ.get("SOLOGBS_AUTO_APPROVER", "").strip() or "auto"


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON 必须是对象: {path}")
    return value


def find_task_root(explicit: Path | None) -> Path:
    if explicit:
        return explicit.expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "monitor" / "state.json").is_file():
            return candidate
    raise RuntimeError("未找到 monitor/state.json；请传入 --task-root")


def keychain_secret(service: str, account: str) -> str:
    proc = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def credentials(service: str) -> tuple[str, str]:
    cookie = os.environ.get("SOLO_QA_COOKIE", "").strip()
    csrf = os.environ.get("SOLO_QA_CSRF", "").strip()

    # 设备配置文件优先：提交是写操作，开始前先确认会话有效，失效就用账号密码重登。
    # 这样避免上传到一半才因为 Cookie 过期而失败。
    try:
        valid_cookie, valid_csrf = _device_config.ensure_solo2_session(validate=True)
    except Exception:
        valid_cookie = valid_csrf = ""
    if valid_cookie and valid_csrf:
        if valid_cookie != cookie:
            print("会话已失效，已用配置里的账号密码自动重新登录 SOLO2", file=sys.stderr)
        return valid_cookie, valid_csrf

    if cookie and csrf:
        return cookie, csrf
    account = os.environ.get("USER", "")
    cookie = cookie or keychain_secret(service + "-cookie", account) or keychain_secret(service, account)
    csrf = csrf or keychain_secret(service + "-csrf", account)
    if not cookie or not csrf:
        raise RuntimeError("缺少 SOLO2 凭据；请设置 SOLO_QA_COOKIE / SOLO_QA_CSRF 或配置 Keychain")
    return cookie, csrf


def api_headers(cookie: str, csrf: str, *, json_body: bool = False,
                url: str = "") -> dict[str, str]:
    base = ""
    if url:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme and parts.netloc:
            base = f"{parts.scheme}://{parts.netloc}"
    if not base:
        base = (DEFAULT_SERVER or os.environ.get("SOLO2_SERVER", "").strip()).rstrip("/")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cookie": cookie,
        "Origin": base,
        "Referer": base + "/app/gsb/submit",
        "User-Agent": "gsb-submit-api/1.0",
        "X-CSRF-Token": csrf,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def request_json(
    method: str,
    url: str,
    cookie: str,
    csrf: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    response = requests.request(
        method,
        url,
        headers=api_headers(cookie, csrf, json_body=payload is not None, url=url),
        data=body,
        timeout=timeout,
    )
    if response.status_code >= 400:
        detail = response.text[:3000]
        raise RuntimeError(f"{method} {url} 返回 HTTP {response.status_code}: {detail}")
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {url} 未返回 JSON: {response.text[:1000]}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {url} 返回数据不是对象")
    return value


def fetch_schema(server: str, cookie: str, csrf: str) -> dict[str, Any]:
    payload = request_json("GET", server + "/api/v1/gsb/form-schema", cookie, csrf)
    fields = payload.get("fields")
    if not isinstance(fields, list) or not fields:
        raise RuntimeError("GSB form-schema 缺少 fields")
    if not str(payload.get("fingerprint") or "").strip():
        raise RuntimeError("GSB form-schema 缺少 fingerprint")
    return payload


def verify_payload(payload: dict[str, Any]) -> None:
    fields = payload.get("fields") or {}
    uploads = payload.get("uploads") or {}
    if not isinstance(fields, dict) or not isinstance(uploads, dict):
        raise RuntimeError("submission payload 的 fields/uploads 必须是对象")
    delivery = payload.get("deliverySheet") or {}
    delivery_path = Path(str(delivery.get("path") or "")).expanduser()
    if not delivery_path.is_file():
        raise RuntimeError(f"交付表不存在: {delivery_path}")
    if sha256_file(delivery_path) != str(delivery.get("sha256") or ""):
        raise RuntimeError(f"交付表哈希不匹配: {delivery_path}")
    for label in UPLOAD_LABELS:
        info = uploads.get(label) or {}
        path = Path(str(info.get("path") or "")).expanduser()
        if not path.is_file():
            raise RuntimeError(f"上传文件不存在: {label}: {path}")
        expected = str(info.get("sha256") or "")
        if expected and sha256_file(path) != expected:
            raise RuntimeError(f"上传文件哈希不匹配: {label}: {path}")


def verify_approval(
    payload_path: Path,
    payload: dict[str, Any],
    approval_path: Path,
    preflight_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = str(payload.get("approvalPolicy") or "")
    payload_sha = sha256_file(payload_path)
    delivery_sha = str((payload.get("deliverySheet") or {}).get("sha256") or "")
    if policy == "automatic":
        if preflight_result is not None:
            live_payload_sha = str(preflight_result.get("submissionPayloadSha256") or "")
            if live_payload_sha and live_payload_sha != payload_sha:
                raise RuntimeError("实时预检 payload 哈希与当前 payload 不一致")
        approval = {
            "schemaVersion": 2,
            "approvalKind": "automatic",
            "scope": "complete-audit-pass",
            "approvedBy": AUTO_APPROVER,
            "approvedAt": utc_now(),
            "taskRoot": str(payload.get("taskRoot") or ""),
            "payloadPath": str(payload_path.resolve()),
            "payloadSha256": payload_sha,
            "deliverySheetSha256": delivery_sha,
            "harnessVersion": str(payload.get("harnessVersion") or ""),
        }
        approval_path.parent.mkdir(parents=True, exist_ok=True)
        approval_path.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return approval
    if policy != CHANGE_VOLUME_APPROVAL_SCOPE:
        raise RuntimeError(f"当前 payload 不允许提交，approvalPolicy={policy or 'missing'}")
    line_gate = payload.get("changeVolumeLineGate") or {}
    if line_gate.get("required") is not True:
        raise RuntimeError("payload 未声明 change-volume-line-gate 例外审批")
    if preflight_result is not None:
        live_payload_sha = str(preflight_result.get("submissionPayloadSha256") or "")
        if live_payload_sha and live_payload_sha != payload_sha:
            raise RuntimeError("实时预检 payload 哈希与当前 payload 不一致")
        live_gate = preflight_result.get("lineGate") or {}
        if live_gate.get("onlyBlocker") is not True:
            raise RuntimeError("实时预检确认改动量门禁不是唯一阻断项，禁止使用例外审批")
        if str(live_gate.get("reviewSha256") or "") != str(line_gate.get("reviewSha256") or ""):
            raise RuntimeError("实时改动量复核哈希与 payload 不一致")
    if not approval_path.is_file():
        raise RuntimeError(
            "改动量门禁是当前唯一阻断项，提交已停止；请等待配置里的审批人批准： "
            f"approve-line-gate --task-root {payload.get('taskRoot') or ''}；审批文件: {approval_path}"
        )
    approval = load_json(approval_path)
    if approval.get("manualConfirmed") is not True:
        raise RuntimeError("改动量例外审批 manualConfirmed 不是 true")
    if str(approval.get("approvalKind") or "") != "manual":
        raise RuntimeError("改动量例外审批 approvalKind 不是 manual")
    if str(approval.get("scope") or "") != CHANGE_VOLUME_APPROVAL_SCOPE:
        raise RuntimeError("审批 scope 不是 change-volume-line-gate")
    if str(approval.get("approvedBy") or "").strip().casefold() != AUTO_APPROVER:
        raise RuntimeError(f"改动量例外只能由 {AUTO_APPROVER} 批准")
    if str(approval.get("taskRoot") or "") != str(payload.get("taskRoot") or ""):
        raise RuntimeError("审批 taskRoot 与 payload 不一致")
    if str(approval.get("payloadPath") or "") != str(payload_path.resolve()):
        raise RuntimeError("审批 payloadPath 与当前 payload 不一致")
    if str(approval.get("payloadSha256") or "") != payload_sha:
        raise RuntimeError("payload 已变化，改动量例外审批失效")
    if str(approval.get("deliverySheetSha256") or "") != delivery_sha:
        raise RuntimeError("交付表哈希与改动量例外审批不一致")
    if str(approval.get("changeVolumeReviewSha256") or "") != str(line_gate.get("reviewSha256") or ""):
        raise RuntimeError("改动量复核哈希与审批不一致")
    review_path = Path(str(approval.get("changeVolumeReviewPath") or line_gate.get("reviewPath") or "")).expanduser().resolve()
    if not review_path.is_file():
        raise RuntimeError(f"改动量复核文件不存在: {review_path}")
    review_doc = load_json(review_path)
    if str(review_doc.get("reviewSha256") or "") != str(approval.get("changeVolumeReviewSha256") or ""):
        raise RuntimeError("改动量复核文件哈希字段与审批不一致")
    review_payload = {
        key: review_doc.get(key)
        for key in (
            "schemaVersion", "id", "initialSnapshot", "hardMinimumLines", "targetLines",
            "failedSides", "onlyBlocker", "codeChange",
        )
    }
    if sha256_json(review_payload) != str(review_doc.get("reviewSha256") or ""):
        raise RuntimeError("改动量复核内容与 reviewSha256 不一致")
    if list(review_doc.get("failedSides") or []) != list(line_gate.get("failedSides") or []):
        raise RuntimeError("改动量复核失败侧与 payload 不一致")
    if list(approval.get("failedSides") or []) != list(line_gate.get("failedSides") or []):
        raise RuntimeError("改动量例外审批失败侧与 payload 不一致")
    if str(approval.get("harnessVersion") or "") != str(payload.get("harnessVersion") or ""):
        raise RuntimeError("Harness 版本与审批不一致")
    approved_uploads = approval.get("uploads") or {}
    for label, info in (payload.get("uploads") or {}).items():
        approved = approved_uploads.get(label) or {}
        path = str(info.get("path") or "")
        digest = str(info.get("sha256") or "")
        if str(approved.get("path") or "") != path or str(approved.get("sha256") or "") != digest:
            raise RuntimeError(f"{label} 文件或哈希与审批不一致")
    return approval


def upload_file(
    server: str,
    cookie: str,
    csrf: str,
    path: Path,
    *,
    video: bool,
    timeout: int = 1800,
) -> dict[str, Any]:
    url = server + "/api/v1/submissions/upload"
    data: dict[str, str] = {}
    if video:
        data["kind"] = "video"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as stream:
        response = requests.post(
            url,
            headers=api_headers(cookie, csrf, url=url),
            data=data,
            files={"file": (path.name, stream, content_type)},
            timeout=timeout,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"上传失败 {path}: HTTP {response.status_code}: {response.text[:3000]}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"上传接口未返回 JSON: {path}: {response.text[:1000]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"上传接口返回不是对象: {path}")
    if video:
        if not result.get("url"):
            raise RuntimeError(f"视频上传未返回 url: {path}")
    else:
        if not result.get("path"):
            raise RuntimeError(f"轨迹上传未返回 path: {path}")
    return result


def field_key_for_label(schema: dict[str, Any], label: str) -> str:
    for field in schema.get("fields") or []:
        if str(field.get("label") or "") == label:
            return str(field.get("field_key") or "")
    raise RuntimeError(f"form-schema 中找不到字段: {label}")


def build_submission_data(
    schema: dict[str, Any],
    payload: dict[str, Any],
    uploaded: dict[str, Any],
) -> dict[str, Any]:
    values = payload.get("fields") or {}
    data: dict[str, Any] = {}
    for field in schema.get("fields") or []:
        key = str(field.get("field_key") or "")
        label = str(field.get("label") or "")
        if key in EXCLUDED_SUBMISSION_FIELDS:
            continue
        if key in uploaded:
            data[key] = uploaded[key]
        else:
            data[key] = values.get(label, "")
    for label, key in UPLOAD_LABELS.items():
        if key not in data:
            data[key] = uploaded.get(key)
        if data.get(key) in (None, "", []):
            raise RuntimeError(f"提交数据缺少上传字段: {label}")
    return data


def poll_submission(
    server: str,
    cookie: str,
    csrf: str,
    submission_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = request_json("GET", f"{server}/api/v1/gsb/submissions/{submission_id}", cookie, csrf)
        status = str(last.get("status") or "")
        if status and status != "SUBMITTED":
            return last
        time.sleep(2.5)
    last = dict(last)
    last["pollTimedOut"] = True
    return last


def write_result(task_root: Path, result: dict[str, Any]) -> Path:
    paths = [
        task_root / "workspace" / "评审文件" / "pre-submit" / "submission-api-result.json",
        task_root / "monitor" / "submission" / "api-result.json",
        task_root / "workspace" / "评审文件" / "pre-submit" / "submission-result.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.name == "submission-result.json":
            compat = {
                "schemaVersion": 1,
                "source": "python-api",
                "submissionNo": result.get("submissionId", ""),
                "submissionId": result.get("submissionId", ""),
                "status": result.get("statusValue", ""),
                "qc": result.get("statusValue", ""),
                "submitted": True,
                "ok": result.get("ok", False),
                "resultPath": str(paths[0]),
            }
            path.write_text(json.dumps(compat, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths[0]


def run_preflight(task_root: Path, preflight_script: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(preflight_script), "--task-root", str(task_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        if proc.returncode != 0:
            raise RuntimeError("提交前审核未通过:\n" + (proc.stderr.strip() or proc.stdout[-4000:])) from exc
        raise RuntimeError("提交前审核没有返回 JSON: " + proc.stdout[-2000:]) from exc
    if not result.get("ok"):
        if result.get("status") == "line_gate_approval_required" and (result.get("lineGate") or {}).get("onlyBlocker") is True:
            return result
        raise RuntimeError("提交前审核存在阻断项:\n" + json.dumps(result.get("blockers") or [], ensure_ascii=False))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="审核并通过 Python API 提交 GSB")
    parser.add_argument("--task-root", type=Path)
    parser.add_argument("--payload", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--preflight", type=Path, default=Path(__file__).with_name("preflight.py"))
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--keychain-service", default=DEFAULT_KEYCHAIN_SERVICE)
    parser.add_argument("--execute", action="store_true", help="真正上传并提交；默认只做审核和 dry-run")
    parser.add_argument("--force", action="store_true", help="忽略本地已有提交结果，允许重新提交")
    parser.add_argument("--poll-timeout", type=float, default=1800.0)
    args = parser.parse_args()

    if args.execute and args.skip_preflight:
        raise RuntimeError("真实提交禁止跳过 preflight；请先修复 G11 改动量或仓库洁净门禁")

    task_root = find_task_root(args.task_root)
    payload_path = (args.payload or task_root / "workspace" / "评审文件" / "pre-submit" / "submission-payload.json").expanduser().resolve()
    approval_path = (args.approval or task_root / "workspace" / "评审文件" / "pre-submit" / "submission-approval.json").expanduser().resolve()

    preflight_result: dict[str, Any] = {}
    if not args.skip_preflight:
        preflight_result = run_preflight(task_root, args.preflight.expanduser().resolve())
    if not payload_path.is_file():
        raise RuntimeError(f"submission payload 不存在: {payload_path}")
    payload = load_json(payload_path)
    verify_payload(payload)
    if args.approval is None and str(payload.get("approvalPolicy") or "") == CHANGE_VOLUME_APPROVAL_SCOPE:
        approval_path = task_root / "workspace" / "评审文件" / "pre-submit" / "change-volume-line-gate-approval.json"
        approval_path = approval_path.expanduser().resolve()

    plan = {
        "taskRoot": str(task_root),
        "payloadPath": str(payload_path),
        "approvalPath": str(approval_path),
        "server": args.server,
        "execute": bool(args.execute),
        "approvalPolicy": payload.get("approvalPolicy") or "legacy",
        "preflightStatus": preflight_result.get("status") or "skipped",
        "uploads": payload.get("uploads") or {},
        "endpoint": args.server + "/api/v1/gsb/submissions",
    }
    if not args.execute:
        print(json.dumps({"status": "dry_run", "ok": True, **plan}, ensure_ascii=False, indent=2))
        return 0

    approval = verify_approval(payload_path, payload, approval_path, preflight_result)
    existing_api_result = task_root / "workspace" / "评审文件" / "pre-submit" / "submission-api-result.json"
    existing_browser_result = task_root / "workspace" / "评审文件" / "pre-submit" / "submission-result.json"
    if not args.force and existing_api_result.is_file():
        raise RuntimeError(f"检测到已有 Python API 提交结果，拒绝重复提交；确认需要重提时加 --force: {existing_api_result}")
    if not args.force and existing_browser_result.is_file():
        old = load_json(existing_browser_result)
        if old.get("submissionNo") or old.get("submitted"):
            raise RuntimeError(f"检测到已有提交记录，拒绝重复提交；确认需要重提时加 --force: {existing_browser_result}")
    cookie, csrf = credentials(args.keychain_service)
    schema = fetch_schema(args.server, cookie, csrf)

    existing_id = ""
    existing_detail: dict[str, Any] = {}
    if existing_api_result.is_file():
        old_result = load_json(existing_api_result)
        candidate_id = str(old_result.get("submissionId") or "").strip()
        if candidate_id and str(old_result.get("statusValue") or "").strip() == "PENDING_FIX":
            existing_detail = request_json(
                "GET",
                f"{args.server}/api/v1/gsb/submissions/{candidate_id}",
                cookie,
                csrf,
                timeout=120,
            )
            if str(existing_detail.get("status") or "").strip() != "PENDING_FIX":
                raise RuntimeError(f"提交 {candidate_id} 当前不是待返修状态，拒绝覆盖")
            existing_id = candidate_id

    uploaded: dict[str, Any] = {}
    if existing_id:
        for key in sorted(TRACE_FIELDS | VIDEO_FIELDS):
            value = existing_detail.get(key)
            if value in (None, "", []):
                raise RuntimeError(f"待返修记录 {existing_id} 缺少原附件字段: {key}")
            uploaded[key] = value
    else:
        for label, info in (payload.get("uploads") or {}).items():
            key = UPLOAD_LABELS.get(label)
            if not key:
                continue
            path = Path(str(info.get("path") or "")).expanduser().resolve()
            result = upload_file(args.server, cookie, csrf, path, video=(key in VIDEO_FIELDS))
            if key in TRACE_FIELDS:
                uploaded[key] = [{
                    "name": result.get("name") or path.name,
                    "path": result.get("path"),
                    "size": result.get("size", path.stat().st_size),
                }]
            else:
                uploaded[key] = result.get("url")

    data = build_submission_data(schema, payload, uploaded)
    if existing_id:
        create_result = request_json(
            "PUT",
            f"{args.server}/api/v1/gsb/submissions/{existing_id}",
            cookie,
            csrf,
            payload={
                "data": data,
                "schema_fingerprint": schema.get("fingerprint"),
                "comment": "返修后重新提交",
            },
            timeout=180,
        )
        submission_id = existing_id
    else:
        create_result = request_json(
            "POST",
            args.server + "/api/v1/gsb/submissions",
            cookie,
            csrf,
            payload={"data": data, "schema_fingerprint": schema.get("fingerprint")},
            timeout=180,
        )
        submission_id = str(create_result.get("id") or "")
    if not submission_id:
        raise RuntimeError("提交接口未返回 submission id: " + json.dumps(create_result, ensure_ascii=False))
    detail = poll_submission(args.server, cookie, csrf, submission_id, args.poll_timeout)
    status = str(detail.get("status") or "")
    result = {
        "schemaVersion": 1,
        "status": "submitted" if status in PASS_STATUSES else "submitted_not_passed",
        "ok": status in PASS_STATUSES,
        "submittedAt": utc_now(),
        "submissionId": submission_id,
        "createResponse": create_result,
        "detail": detail,
        "payloadPath": str(payload_path),
        "approvalPath": str(approval_path),
        "approval": approval,
        "approvedBy": str(approval.get("approvedBy") or ""),
        "approvalKind": str(approval.get("approvalKind") or ""),
        "statusValue": status,
    }
    output = write_result(task_root, result)
    print(json.dumps({"status": result["status"], "ok": result["ok"], "submissionId": submission_id, "statusValue": status, "resultPath": str(output)}, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
