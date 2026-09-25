from __future__ import annotations

# ruff: noqa: E402

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib
import zipfile
from collections import UserDict
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Retry backoff is real wall-clock sleep; tests that simulate failing attempts
# opt back in explicitly.
os.environ.setdefault("SOLOGBS_ATTEMPT_BACKOFF_SECONDS", "0")
sys.path.insert(0, str(ROOT / "scripts"))

from common import (  # noqa: E402
    SologsbError,
    github_env,
    global_recording_lock,
    read_json,
    safe_slug,
    sha256_file,
    skill_version,
    skill_version_info,
    write_json,
)
from gsb_tools import (
    _validate_artifact_description,
    _validate_claim_evidence,
    _validate_low_value_noise_claims,
    _validate_negative_claim_triggers,
    _validate_reason_layer_coverage,
    _validate_reason_markdown,
    evaluation_excluded_reason_errors,
    build_values,
    reason_flow_errors,
    reason_language_errors,
    reason_style_errors,
    reason_style_warnings,
    validate_delivery,
    validate_draft,
    write_excel,
    write_field_guide,
)  # noqa: E402
from prompt_tools import check_history  # noqa: E402
from project_claims import (
    claim_release_reason,
    claimed_project_codes,
    release_claim_if_finished,
    release_project_claim,
    start_project_claim,
)  # noqa: E402
import source_ingest  # noqa: E402
from source_ingest import ingest_source  # noqa: E402
from github_repo import (
    _clone_branch,
    _derive_repo_base,
    _ensure_origin_commit,
    _normalize_repo_prefix,
    _push_topology,
    _random_code,
    init_github_repo,
)  # noqa: E402
from semantic_review import validate_review  # noqa: E402
import side_runner  # noqa: E402
from recorder import default_plan, prepare_recording, recording_output_name  # noqa: E402
from recorder import (  # noqa: E402
    _capture_otty_pane_text,
    _copy_final,
    _ensure_sck_recorder,
    _MouseCursorGuard,
    SCK_RECORDER_SOURCE,
    normalize_pointer_strategy,
    _otty_open_window,
    _recording_service_ports,
    _require_window_id,
    _validate_recording_window,
    recording_isolation_ok,
    _terminal_command,
    _window_info_payload,
    recording_command_ok,
    validate_api_requests,
    validate_recording_targets,
    video_dimensions,
)
from artifact_verifier import suggest_plan  # noqa: E402
from trace_validator import validate_single_round  # noqa: E402
from sologsb import build_parser  # noqa: E402


class ProxyTests(unittest.TestCase):
    def test_explicit_github_proxy_is_injected(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SOLOSB_GITHUB_PROXY": "http://127.0.0.1:17890"},
            clear=False,
        ):
            env = github_env()
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:17890")
        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:17890")
        self.assertEqual(env["ALL_PROXY"], "http://127.0.0.1:17890")
        self.assertIn("127.0.0.1", env["NO_PROXY"])

    def test_required_github_proxy_fails_closed(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("common._proxy_port_open", return_value=False):
                with self.assertRaisesRegex(SologsbError, "Loon GitHub 代理不可用"):
                    github_env(require_proxy=True)


class TraceTests(unittest.TestCase):
    def _write_trace(self, path: Path, prompts: list[str], end_turn: bool = True, session_id: str | None = "session-1") -> None:
        events = [
            {
                "type": "user",
                "uuid": "u1",
                **({"sessionId": session_id} if session_id else {}),
                "message": {"role": "user", "content": prompts[0]},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                **({"sessionId": session_id} if session_id else {}),
                "message": {
                    "role": "assistant",
                    "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}],
                },
            },
            {
                "type": "user",
                "uuid": "u2",
                **({"sessionId": session_id} if session_id else {}),
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
                },
            },
            {
                "type": "assistant",
                "uuid": "a2",
                **({"sessionId": session_id} if session_id else {}),
                "message": {
                    "role": "assistant",
                    "stop_reason": "end_turn" if end_turn else "max_tokens",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ]
        for prompt in prompts[1:]:
            events.append(
                {
                    "type": "user",
                    "uuid": f"u{len(events)}",
                    **({"sessionId": session_id} if session_id else {}),
                    "message": {"role": "user", "content": prompt},
                }
            )
        path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

    def test_single_clean_round(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            self._write_trace(path, ["唯一提示词"])
            result = validate_single_round(path, expected_prompt="唯一提示词", expected_session_id="session-1")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["userPromptCount"], 1)

    def test_second_prompt_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            self._write_trace(path, ["唯一提示词", "继续"])
            result = validate_single_round(path, expected_prompt="唯一提示词", expected_session_id="session-1")
            self.assertFalse(result["ok"])
            self.assertTrue(any("恰好一个" in item for item in result["errors"]))

    def test_no_end_turn_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "trace.jsonl"
            self._write_trace(path, ["唯一提示词"], end_turn=False)
            result = validate_single_round(path, expected_prompt="唯一提示词", expected_session_id="session-1")
            self.assertFalse(result["ok"])
            self.assertTrue(any("end_turn" in item for item in result["errors"]))

    def test_missing_or_mismatched_session_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "missing.jsonl"
            self._write_trace(path, ["唯一提示词"], session_id=None)
            missing = validate_single_round(path, expected_prompt="唯一提示词", expected_session_id="session-1")
            self.assertFalse(missing["ok"])
            self.assertTrue(any("不包含 SessionID" in item for item in missing["errors"]), missing)

            path = Path(temp) / "mismatch.jsonl"
            self._write_trace(path, ["唯一提示词"], session_id="session-2")
            mismatch = validate_single_round(path, expected_prompt="唯一提示词", expected_session_id="session-1")
            self.assertFalse(mismatch["ok"])
            self.assertTrue(any("SessionID 不匹配" in item for item in mismatch["errors"]), mismatch)


class PromptTests(unittest.TestCase):
    def test_history_exact_and_shingle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            write_json(path, [{"id": "old", "prompt": "同一个完整提示词内容用于测试重复"}])
            errors = check_history("同一个完整提示词内容用于测试重复", path)
            self.assertTrue(errors)


class ExcelTests(unittest.TestCase):
    def test_excel_has_official_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "workspace" / "评审文件").mkdir(parents=True)
            schema = read_json(ROOT / "references" / "gsb-form-schema.json", {})
            values = {field["field_key"]: field["field_key"] for field in schema["fields"]}
            path = write_excel(root, schema, values)
            self.assertTrue(path.is_file())
            from openpyxl import load_workbook
            workbook = load_workbook(path, read_only=True)
            headers = [cell.value for cell in workbook["GSB提交"][1]]
            self.assertEqual(len(headers), len(schema["fields"]))
            self.assertEqual(headers[0], "User Prompt")
            self.assertEqual(headers[-1], "GSB 理由")
            self.assertNotIn("备注", headers)
            for label in ("A-交付完整性", "A-交付完整性描述", "B-交付完整性", "B-交付完整性描述"):
                self.assertIn(label, headers)
            self.assertLess(headers.index("A-运行录屏"), headers.index("A-交付完整性"))
            self.assertLess(headers.index("B-交付完整性描述"), headers.index("GSB 结论"))


    def test_reason_length_is_150_to_240(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            evidence = {
                "evidence": [
                    {"id": f"{side}-{kind}-{polarity}", "side": side, "type": kind, "text": "ok"}
                    for side in ("A", "B")
                    for kind in ("process", "artifact")
                    for polarity in ("positive", "negative")
                ]
            }
            write_json(root / "monitor" / "evidence.json", evidence)
            write_json(root / "monitor" / "state.json", {"status": "verified"})
            claims = [
                {"side": side, "type": kind, "polarity": polarity,
                 "evidenceIds": [f"{side}-{kind}-{polarity}"]}
                for side in ("A", "B")
                for kind in ("process", "artifact")
                for polarity in ("positive", "negative")
            ]
            review = Path(temp) / "review.json"
            review.write_text("{}", encoding="utf-8")
            with mock.patch("gsb_tools._validate_reason_dedup", return_value=[]), mock.patch(
                "gsb_tools.subprocess.run"
            ) as subprocess_run:
                subprocess_run.return_value = mock.Mock(stdout=json.dumps({"errors": []}), stderr="")
                short = {"verdict": "A 更好", "reason": "A" * 149, "remark": "", "claims": claims}
                long_result = validate_draft(short, root, review_path=review)
                self.assertFalse(long_result["ok"])
                self.assertTrue(any("150–240" in item for item in long_result["errors"]))
                reason = "A" * 90 + "B" * 90
                good = {
                    "verdict": "A 更好", "reason": reason, "remark": "", "claims": claims,
                    "sentenceEvidence": [{
                        "sentence": reason,
                        "evidenceIds": ["A-artifact-positive", "B-artifact-positive"],
                    }],
                }
                good_result = validate_draft(good, root, review_path=review)
                self.assertEqual(good_result["reasonLength"], 180)
                missing = {"verdict": "A 更好", "reason": reason, "remark": "", "claims": claims}
                missing_result = validate_draft(missing, root, review_path=review)
                self.assertTrue(any("sentenceEvidence" in item for item in missing_result["errors"]))
                detailed_reason = "A有113次调用和30个文件以及119分钟边界。" + "B" * 150
                detailed = {
                    "verdict": "A 更好", "reason": detailed_reason, "remark": "", "claims": claims,
                    "sentenceEvidence": [{
                        "sentence": detailed_reason,
                        "evidenceIds": ["A-artifact-positive", "B-artifact-positive"],
                    }],
                }
                detailed_result = validate_draft(detailed, root, review_path=review)
                self.assertTrue(any("数字对比过多" in item for item in detailed_result["errors"]))
                nonempty_remark = {**good, "remark": "环境差异已记录"}
                remark_result = validate_draft(nonempty_remark, root, review_path=review)
                self.assertNotIn("remark", nonempty_remark)
                self.assertFalse(any("备注" in item for item in remark_result["errors"]), remark_result)
                self.assertTrue(any("draft.delivery" in item for item in good_result["errors"]), good_result)

    def test_gsb_reason_rejects_markdown(self) -> None:
        self.assertEqual(_validate_reason_markdown("接口返回 500，邀签接口无法使用。"), [])
        for value in (
            "接口返回 `500`。",
            "**接口返回 500**。",
            "- 接口返回 500。",
            "1. 接口返回 500。",
            "> 接口返回 500。",
            "[接口](https://example.com)",
            "![录屏](/tmp/a.mp4)",
            "## 结论",
            "| 字段 | 值 |",
            "<br>",
        ):
            self.assertTrue(_validate_reason_markdown(value), value)

    def test_gsb_reason_language_gate(self) -> None:
        good = (
            "A 侧方案在联调阶段修改了保存逻辑，配置能够正常写入；"
            "B 侧方案在验证阶段读取接口返回后，发现版本字段仍然缺失。"
        )
        self.assertEqual(reason_language_errors(good), [])
        long_sentence = "A 侧方案在联调阶段修改保存逻辑并且逐项检查接口返回字段和页面展示结果以后又继续核对数据库写入状态但是最终仍没有说明配置是否生效。"
        self.assertTrue(any("单句不得超过" in item for item in reason_language_errors(long_sentence)))
        self.assertTrue(reason_language_errors("A 侧方案修改了保存逻辑，，配置仍然没有写入。"))
        self.assertTrue(reason_language_errors("A 侧方案修改了保存逻辑，配置仍然没有写入"))
        self.assertTrue(reason_language_errors("A 侧方案修改了保存逻辑（配置仍然没有写入。"))

    def test_gsb_reason_rejects_evaluation_excluded_evidence(self) -> None:
        evidence_doc = {
            "evidence": [
                {
                    "id": "A-process-noise",
                    "side": "A",
                    "type": "process",
                    "polarity": "negative",
                    "text": "命令未找到，退出码127",
                    "evaluationExcluded": True,
                }
            ]
        }
        draft = {
            "claims": [
                {
                    "side": "A",
                    "type": "process",
                    "polarity": "negative",
                    "text": "命令未找到，退出码127",
                    "evidenceIds": ["A-process-noise"],
                }
            ],
            "sentenceEvidence": [
                {"sentence": "A 侧方案命令未找到，退出码127。", "evidenceIds": ["A-process-noise"]}
            ],
        }
        errors = evaluation_excluded_reason_errors(
            "A 侧方案命令未找到，退出码127。", draft, evidence_doc
        )
        self.assertTrue(any("claim 1" in item for item in errors), errors)
        self.assertTrue(any("sentenceEvidence 1" in item for item in errors), errors)
        self.assertTrue(any("不得写入" in item for item in errors), errors)

    def test_gsb_reason_style_rules(self) -> None:
        """文案通则：禁用词、数字与英文两侧不留空格，固定称谓除外。"""
        self.assertEqual(
            reason_style_errors("A 侧方案把改动入库，计数从2变22，APIs.vue的回调也已触发。"),
            [],
        )
        self.assertTrue(reason_style_errors("A 侧方案把改动落库。"))
        self.assertTrue(reason_style_errors("A 侧方案宣称完成闭环。"))
        self.assertTrue(reason_style_errors("A 侧方案把根因写成配置缺失。"))
        self.assertTrue(reason_style_errors("A 侧方案把改动入库，计数 2 变 22。"))
        self.assertTrue(reason_style_errors("A 侧方案读取 Mock 服务配置。"))
        legacy = reason_style_errors("A 侧方案实现验收，这题最要紧的是状态一致。")
        self.assertTrue(any("这题" in item for item in legacy), legacy)
        self.assertTrue(any("最要紧" in item for item in legacy), legacy)
        self.assertEqual(
            reason_style_errors("A 侧方案实现验收，这个任务最重要的是状态一致。"),
            [],
        )
        self.assertEqual(reason_style_warnings("A 侧方案建了独立表，读库时发现停用位被默认值覆盖掉了。"), [])
        self.assertTrue(any("空泛表达" in item for item in reason_style_warnings("A 侧方案真实完成入库。")))
        warnings = reason_style_warnings("A 侧方案建独立表。读库发现默认值盖掉停用位。保存后读回正常。")
        self.assertTrue(warnings, warnings)

    def test_gsb_reason_rejects_recording_runtime_factors(self) -> None:
        product_reason = (
            "A 侧方案首页正常返回，查询接口返回预期数据；"
            "B 侧方案提交表单时返回500，数据没有写入。"
        )
        self.assertEqual(_validate_artifact_description(product_reason), [])
        self.assertTrue(_validate_artifact_description('结论落在 B 侧方案。'))
        for field_factor in (
            "A 侧录屏中页面正常，B 侧视频画面显示报错。",
            "A 侧截图显示接口返回 500。",
            "A 侧 Otty 窗口里服务正常启动。",
            "A 侧方案编写验收脚本后页面正常返回。",
            "B 侧方案在本地新建测试文件后接口返回预期数据。",
            "A 侧方案在本地验收时执行自测脚本。",
        ):
            self.assertTrue(_validate_artifact_description(field_factor), field_factor)
        self.assertEqual(_validate_artifact_description("A侧方案上传测试文件功能正常"), [])
        self.assertEqual(
            _validate_artifact_description("A侧方案在候选容器内新建测试文件并完成接口流程验证"),
            [],
        )
        for tool_noise in (
            "A 侧方案在验证时执行 python 返回 127，随后重新运行验证。",
            "B 侧方案修改 database/init.sql 时遇到 String to replace not found，重新定位后写入。",
            "B 侧方案运行测试时出现 No module named app，设置 PYTHONPATH 后通过。",
        ):
            # 工具噪声不是“录屏/画面”类场外因素；文案通则的空格提示属于另一条规则，
            # 这里只断言没有被当成场外因素拦下。
            field_factor_errors = [
                item
                for item in _validate_artifact_description(tool_noise)
                if any(word in item for word in ("录屏", "视频", "截图", "Otty"))
            ]
            self.assertEqual(field_factor_errors, [], tool_noise)
            errors = _validate_low_value_noise_claims({
                "claims": [{"side": "A", "type": "process", "polarity": "negative", "text": tool_noise}]
            })
            self.assertTrue(errors, tool_noise)

    def test_reason_layer_requires_each_side_process_and_artifact(self) -> None:
        good = {
            "claims": [
                {
                    "side": "A",
                    "type": "process",
                    "polarity": "positive",
                    "text": "A 侧方案在联调阶段读取 `database/init.sql` 和迁移脚本后",
                },
                {
                    "side": "A",
                    "type": "artifact",
                    "polarity": "negative",
                    "text": "升级后索引列表仍含 `idx_run_input_version`，接口返回缺少版本字段",
                },
                {
                    "side": "B",
                    "type": "process",
                    "polarity": "positive",
                    "text": "B 侧方案在新增指标时同步了口径说明和默认单位",
                },
                {
                    "side": "B",
                    "type": "artifact",
                    "polarity": "negative",
                    "text": "新增、编辑和重命名流程仍未接入，提交后对应配置没有写入",
                },
            ]
        }
        reason = (
            "A 侧方案在联调阶段读取 `database/init.sql` 和迁移脚本后，升级后索引列表仍含 "
            "`idx_run_input_version`，接口返回缺少版本字段；B 侧方案在新增指标时同步了口径说明和默认单位，"
            "但新增、编辑和重命名流程仍未接入，提交后对应配置没有写入。"
        )
        self.assertEqual(_validate_reason_layer_coverage(good, reason), [])

        product_only = {
            "claims": [
                {"side": "A", "type": "process", "polarity": "positive", "text": "A 侧方案升级迁移"},
                {
                    "side": "A",
                    "type": "artifact",
                    "polarity": "negative",
                    "text": "升级后索引列表仍含 `idx_run_input_version`",
                },
                {"side": "B", "type": "process", "polarity": "positive", "text": "B 侧方案接口返回"},
                {
                    "side": "B",
                    "type": "artifact",
                    "polarity": "negative",
                    "text": "接口响应仍缺少版本字段",
                },
            ]
        }
        product_only_reason = (
            "A 侧方案升级迁移里旧唯一约束没有去掉，升级后索引列表仍含 `idx_run_input_version`；"
            "B 侧方案接口响应仍缺少版本字段。"
        )
        errors = _validate_reason_layer_coverage(product_only, product_only_reason)
        self.assertTrue(any("A 侧过程 claim" in item for item in errors), errors)
        self.assertTrue(any("B 侧过程 claim" in item for item in errors), errors)


    def test_negative_claim_requires_concrete_trigger_node(self) -> None:
        bad = {
            "claims": [
                {
                    "side": "A",
                    "polarity": "negative",
                    "triggerKind": "step",
                    "trigger": "过程里",
                },
                {
                    "side": "B",
                    "polarity": "negative",
                    "triggerKind": "requirement",
                    "trigger": "只同步口径说明和默认单位",
                },
            ]
        }
        errors = _validate_negative_claim_triggers(
            bad,
            "A侧过程里曾把工作目录写错一次；B侧只同步口径说明和默认单位。",
        )
        self.assertTrue(any("泛化过程" in item for item in errors), errors)
        self.assertTrue(any("step trigger" in item for item in errors), errors)
        self.assertTrue(any("requirement trigger" in item for item in errors), errors)
        numbered_only = {
            "claims": [{
                "side": "A",
                "polarity": "negative",
                "triggerKind": "step",
                "trigger": "第一次操作时",
            }]
        }
        numbered_errors = _validate_negative_claim_triggers(
            numbered_only,
            "A侧第一次操作时出现了问题。",
        )
        self.assertTrue(any("只有序号" in item for item in numbered_errors), numbered_errors)

        good = {
            "claims": [
                {
                    "side": "A",
                    "polarity": "negative",
                    "triggerKind": "command",
                    "trigger": "第二次执行 `go test ./...` 时",
                    "objectiveConsequence": "包路径解析失败，测试没有完成",
                },
                {
                    "side": "B",
                    "polarity": "negative",
                    "triggerKind": "requirement",
                    "trigger": "新增指标时",
                    "objectiveConsequence": "新增、编辑和重命名流程没有接入",
                },
            ]
        }
        reason = (
            "A侧第二次执行 `go test ./...` 时把工作目录写错，导致包路径解析失败，测试没有完成；"
            "B侧在新增指标时只同步口径说明和默认单位，导致新增、编辑和重命名流程没有接入。"
        )
        self.assertEqual(_validate_negative_claim_triggers(good, reason), [])

        missing_impact = {
            "claims": [{
                "side": "A",
                "polarity": "negative",
                "triggerKind": "command",
                "trigger": "第二次执行 `go test ./...` 时",
            }]
        }
        impact_errors = _validate_negative_claim_triggers(
            missing_impact,
            "A侧第二次执行 `go test ./...` 时把工作目录写错。",
        )
        self.assertTrue(any("objectiveConsequence" in item for item in impact_errors), impact_errors)

        machine_step = {
            "claims": [{
                "side": "A",
                "polarity": "negative",
                "triggerKind": "step",
                "trigger": "第 323 次修改服务构造函数时",
                "objectiveConsequence": "修改没有落地",
            }]
        }
        machine_errors = _validate_negative_claim_triggers(
            machine_step,
            "A侧第 323 次修改服务构造函数时文本替换失败，修改没有落地。",
        )
        self.assertTrue(any("机器式步骤序号" in item for item in machine_errors), machine_errors)

    def test_field_guide_covers_all_schema_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "workspace" / "评审文件").mkdir(parents=True)
            schema = read_json(ROOT / "references" / "gsb-form-schema.json", {})
            values = {field["field_key"]: "示例值" for field in schema["fields"]}
            path = write_field_guide(root, schema, values)
            text = path.read_text(encoding="utf-8")
            for field in schema["fields"]:
                self.assertIn(f"`{field['field_key']}`", text)
            self.assertIn("提交：`submit_api.py` 会上传文件", text)


    def test_claim_evidence_requires_both_sides(self) -> None:
        evidence = {
            "evidence": [
                {"id": "A-artifact-check-01", "side": "A", "type": "artifact"},
                {"id": "B-artifact-check-01", "side": "B", "type": "artifact"},
            ]
        }
        draft = {
            "claims": [
                {"side": "A", "type": "artifact", "polarity": "positive", "evidenceIds": ["A-artifact-check-01"]},
                {"side": "B", "type": "artifact", "polarity": "negative", "evidenceIds": ["B-artifact-check-01"]},
            ]
        }
        errors = _validate_claim_evidence(draft, evidence)
        self.assertTrue(any("A 缺少负面" in item for item in errors))
        self.assertTrue(any("B 缺少正面" in item for item in errors))
        self.assertTrue(any("A 缺少过程层面" in item for item in errors))
        self.assertTrue(any("B 缺少过程层面" in item for item in errors))



def _api_fixture_server() -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: ANN002
            return

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self._send(200, {"data": {"token": "tok-123"}})

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") == "Bearer tok-123":
                self._send(200, {"data": {"orders": [{"id": 7}]}})
            else:
                self._send(401, {"error": "unauthorized"})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class DeliveryFieldTests(unittest.TestCase):
    """2026-09-23 新增的 A/B 交付完整性打分与描述，含“与轨迹一致”红线。"""

    REASON = (
        "A 侧方案在打开api.ts的请求定义时多拼了一层前缀，登录一直被挡在外面，页面进不去日记。"
        "B 侧方案补跑了发布流程并回读快照，这个任务最重要的是能完整走通，因此选择 B 侧方案。"
    )
    DESC_A = "登录请求在frontend/src/api.ts里多拼了一层前缀，接口返回404。用户进不了日记页，保存和发布需求都没法验证。"
    DESC_B = "逐条核对了选择行程、存草稿、发起人发布和冻结标题四项需求，构建和启动都通过，刷新后版本与发布状态一致。"

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.trace_a = root / "a.jsonl"
        self.trace_b = root / "b.jsonl"
        self.trace_a.write_text(self._edit_event("/workspace/frontend/src/api.ts"), encoding="utf-8")
        self.trace_b.write_text(self._edit_event("/workspace/src/diary.service.ts"), encoding="utf-8")

    @staticmethod
    def _edit_event(path: str, tool: str = "Edit") -> str:
        block = {"type": "tool_use", "name": tool, "input": {"file_path": path} if tool != "Bash" else {"command": path}}
        return json.dumps({"type": "assistant", "message": {"content": [block]}}, ensure_ascii=False) + "\n"

    def tearDown(self) -> None:
        self._temp.cleanup()

    @staticmethod
    def _check(id_: str, side: str, name: str, command: str, ok: bool, output: str = "", **extra) -> dict:
        return {
            "id": id_, "side": side, "type": "artifact",
            "polarity": "positive" if ok else "negative", "text": f"真实复核 {name}",
            "artifact": {"command": command, "ok": ok, "observedFailure": False, "output": output, **extra},
        }

    def _evidence(self, *, a_build_ok: bool = True, extra: list | None = None) -> dict:
        items = [
            self._check("A-artifact-check-01", "A", "登录接口", "curl -i localhost:3000/api/api/users/login", False, "HTTP/1.1 404"),
            self._check("A-artifact-check-02", "A", "npm-build", "npm run build", a_build_ok),
            self._check("B-artifact-check-01", "B", "npm-build", "npm run build", True),
            self._check("B-artifact-check-02", "B", "发布回读", "curl localhost:3000/api/diary", True),
            {"id": "A-process-final", "side": "A", "type": "process", "text": "模型最终回复",
             "trace": {"tracePath": str(self.trace_a), "quote": "登录和发布功能已全部完成"}},
        ]
        return {
            "evidence": items + (extra or []),
            "process": {"A": {"tracePath": str(self.trace_a)}, "B": {"tracePath": str(self.trace_b)}},
        }

    def _draft(self, verdict: str = "B 更好", **delivery) -> dict:
        base = {
            "A": {"score": 2, "description": self.DESC_A, "evidenceIds": ["A-artifact-check-01"]},
            "B": {"score": 5, "description": self.DESC_B, "evidenceIds": ["B-artifact-check-02"]},
        }
        for side, patch in delivery.items():
            base[side] = {**base[side], **patch}
        return {"verdict": verdict, "reason": self.REASON, "delivery": base}

    def _errors(self, draft: dict, evidence: dict | None = None) -> list[str]:
        return validate_delivery(draft, evidence or self._evidence(), self.REASON)["errors"]

    def test_valid_delivery_passes(self) -> None:
        result = validate_delivery(self._draft(), self._evidence(), self.REASON)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scores"], {"A": 2, "B": 5})

    def test_missing_delivery_blocks(self) -> None:
        self.assertFalse(validate_delivery({"verdict": "A 更好"}, self._evidence(), self.REASON)["ok"])

    def test_score_must_be_integer_1_to_5(self) -> None:
        for bad in (0, 6, 3.5, "4", True):
            errors = self._errors(self._draft(A={"score": bad}))
            self.assertTrue(any("1~5 的整数" in item for item in errors), (bad, errors))

    def test_full_score_needs_basis_and_no_open_problem(self) -> None:
        vague = self._errors(self._draft(B={"description": "整体做得很好，各项功能都符合题目描述的预期，没有看到明显的问题，可以直接交付给用户。"}))
        self.assertTrue(any("核对依据" in item for item in vague), vague)
        contradictory = self._errors(self._draft(B={"description": "逐条核对了存草稿和发布两项需求，构建通过，但成员离队后的只读限制还没有实现，缺少对应校验。"}))
        self.assertTrue(any("分数与描述矛盾" in item for item in contradictory), contradictory)

    def test_low_score_needs_location_and_consequence(self) -> None:
        errors = self._errors(self._draft(A={"description": "这一侧整体完成度一般，很多地方做得比较粗糙，和题目的要求相比还有不小的差距需要继续打磨。"}))
        self.assertTrue(any("客观后果" in item for item in errors), errors)

    def test_process_dimensions_are_rejected(self) -> None:
        errors = self._errors(self._draft(A={"description": self.DESC_A + "任务规划也比较乱。"}))
        self.assertTrue(any("过程维度" in item for item in errors), errors)

    def test_copying_reason_is_rejected(self) -> None:
        copied = "A 侧方案在打开api.ts的请求定义时多拼了一层前缀，登录一直被挡在外面，页面进不去日记。"
        errors = self._errors(self._draft(A={"description": copied}))
        self.assertTrue(any("照抄" in item or "过于相似" in item for item in errors), errors)

    def test_a_and_b_must_not_be_alike(self) -> None:
        errors = self._errors(self._draft(A={"score": 5, "description": self.DESC_B, "evidenceIds": ["A-artifact-check-02"]}))
        self.assertTrue(any("雷同" in item for item in errors), errors)

    def test_evidence_must_be_same_side(self) -> None:
        errors = self._errors(self._draft(A={"evidenceIds": ["B-artifact-check-01"]}))
        self.assertTrue(any("另一侧" in item for item in errors), errors)

    # ---- 红线：描述必须与本侧轨迹对应，不得出现对立意见 ----

    def test_file_anchor_must_exist_in_own_trace(self) -> None:
        other_side_file = "登录请求在src/diary.service.ts里多拼了一层前缀，接口返回404。用户进不了日记页，保存和发布都没法验证。"
        errors = self._errors(self._draft(A={"description": other_side_file}))
        self.assertTrue(any("diary.service.ts 在本侧轨迹中不存在" in item for item in errors), errors)

    def test_literal_error_anchor_must_exist_in_trace(self) -> None:
        desc = "迁移时报SQLSTATE23505，唯一索引建不起来，服务启动时就退出了，段位登记需求没有做完。"
        errors = self._errors(self._draft(A={"description": desc}))
        self.assertTrue(any("SQLSTATE23505" in item for item in errors), errors)

    def test_status_code_must_be_observed(self) -> None:
        desc = "登录请求在frontend/src/api.ts里多拼了一层前缀，接口返回500。用户进不了日记页，保存和发布都没法验证。"
        errors = self._errors(self._draft(A={"description": desc}))
        self.assertTrue(any("500" in item for item in errors), errors)

    def test_build_failure_caps_score_and_blocks_success_wording(self) -> None:
        evidence = self._evidence(a_build_ok=False)
        errors = self._errors(self._draft(A={"score": 3}), evidence)
        self.assertTrue(any("无法运行最高 2 分" in item for item in errors), errors)
        desc = "登录请求在frontend/src/api.ts里多拼了一层前缀，接口返回404。不过构建和启动都通过，其余需求可用。"
        errors = self._errors(self._draft(A={"description": desc}), evidence)
        self.assertTrue(any("描述与复核结果对立" in item for item in errors), errors)

    def test_all_checks_passing_contradicts_cannot_run(self) -> None:
        desc = "发布入口在src/diary.service.ts里没有接好，页面无法启动，存草稿和发布两项需求都没有完成。"
        errors = self._errors(self._draft(B={"score": 2, "description": desc, "evidenceIds": ["B-artifact-check-02"]}))
        self.assertTrue(any("无法启动" in item and "对立" in item for item in errors), errors)
        self.assertTrue(any("没有任何可引用的失败证据" in item and "probe" in item for item in errors), errors)

    def test_full_score_cannot_cite_failure_or_have_failed_checks(self) -> None:
        desc = "逐条核对了登录、选择行程和发布三项需求，页面操作都能完成，刷新后状态保持一致，结果符合预期。"
        errors = self._errors(self._draft("A 更好", A={"score": 5, "description": desc}, B={"score": 4}))
        self.assertTrue(any("失败项" in item for item in errors), errors)
        self.assertTrue(any("引用了本侧失败证据" in item for item in errors), errors)

    def test_full_score_conflicts_with_negative_reason_claim(self) -> None:
        draft = self._draft()
        draft["claims"] = [{"side": "B", "type": "artifact", "polarity": "negative", "text": "发布后标题没有冻结"}]
        errors = self._errors(draft)
        self.assertTrue(any("两处意见对立" in item for item in errors), errors)

    def test_verdict_against_scores_blocks(self) -> None:
        errors = self._errors(self._draft("A 更好"))
        self.assertTrue(any("结论与打分对立" in item for item in errors), errors)
        errors = self._errors(self._draft("Same"))
        self.assertTrue(any("结论与打分对立" in item for item in errors), errors)

    def test_false_success_needs_completion_claim_in_trace(self) -> None:
        desc = "模型最终回复宣称登录已修好，实际frontend/src/api.ts仍多拼前缀，接口返回404，属于虚假成功。"
        self.assertEqual([e for e in self._errors(self._draft(A={"description": desc})) if "虚假成功" in e], [])
        evidence = self._evidence()
        evidence["evidence"][-1]["trace"]["quote"] = "我还没来得及处理登录问题"
        errors = self._errors(self._draft(A={"description": desc}), evidence)
        self.assertTrue(any("没有宣称完成" in item for item in errors), errors)

    def test_source_attribution_wording_is_rejected(self) -> None:
        for phrase in ("从录屏来看", "编写的测试", "复核结果显示", "根据测试"):
            desc = f"{phrase}，登录请求在frontend/src/api.ts里多拼了一层前缀，接口返回404，保存和发布需求都没法验证。"
            errors = self._errors(self._draft(A={"description": desc}))
            self.assertTrue(any("不要交代信息来源" in item for item in errors), (phrase, errors))
        reason = self.REASON.replace("B 侧方案补跑了", "从测试结果看B 侧方案补跑了")
        self.assertTrue(any("不要交代信息来源" in item for item in _validate_artifact_description(reason)))

    def test_edit_claims_must_match_trace(self) -> None:
        not_changed = "frontend/src/api.ts没有修改，登录请求还是多拼一层前缀，接口返回404，保存和发布需求都没法验证。"
        errors = self._errors(self._draft(A={"description": not_changed}))
        self.assertTrue(any("没有改" in item and "完全对立" in item for item in errors), errors)
        self.trace_a.write_text(
            self._edit_event("/workspace/frontend/src/api.ts", "Read") + self._edit_event("cat frontend/src/login.ts", "Bash"),
            encoding="utf-8",
        )
        changed = "模型修改了frontend/src/api.ts的登录请求，但前缀还是多拼一层，接口返回404，发布需求没法验证。"
        errors = self._errors(self._draft(A={"description": changed}))
        self.assertTrue(any("没有任何对它的编辑" in item for item in errors), errors)
        self.trace_a.write_text(self._edit_event("sed -i s/a/b/ frontend/src/api.ts", "Bash"), encoding="utf-8")
        errors = self._errors(self._draft(A={"description": changed}))
        self.assertFalse(any("完全对立" in item for item in errors), errors)

    def test_probe_failure_is_admissible_evidence(self) -> None:
        probe = self._check(
            "A-artifact-check-03", "A", "登录接口探活", "npm run dev", False,
            "[probe] POST /api/users/login -> 404",
            probe={"method": "POST", "path": "/api/users/login", "status": 404},
        )
        desc = "请求了/api/users/login登录接口，返回404。用户进不了日记页，保存和发布需求都没法走到。"
        result = validate_delivery(
            self._draft(A={"description": desc, "evidenceIds": ["A-artifact-check-03"]}),
            self._evidence(extra=[probe]), self.REASON,
        )
        self.assertTrue(result["ok"], result)

    def test_web_recording_is_usable_like_before(self) -> None:
        web = {"id": "A-recording", "side": "A", "type": "artifact", "polarity": "negative",
               "artifact": {"ok": True, "observedFailure": True, "recordingMode": "web", "output": "登录后停在空白页"}}
        desc = "打开日记页点登录后停在空白页，用户进不了日记页，保存和发布这两项需求都没法走到。"
        result = validate_delivery(
            self._draft(A={"description": desc, "evidenceIds": ["A-recording"]}),
            self._evidence(extra=[web]), self.REASON,
        )
        self.assertTrue(result["ok"], result)

    def test_probe_not_used_for_projects_with_pages(self) -> None:
        web = {"id": "A-recording", "side": "A", "type": "artifact", "polarity": "negative",
               "artifact": {"ok": True, "observedFailure": True, "recordingMode": "web"}}
        probe = self._check("A-artifact-check-03", "A", "登录接口探活", "npm run dev", False,
                            "[probe] POST /api/users/login -> 404", probe={"status": 404})
        errors = self._errors(
            self._draft(A={"evidenceIds": ["A-artifact-check-03"]}), self._evidence(extra=[web, probe])
        )
        self.assertTrue(any("不做接口探活" in item for item in errors), errors)

    # ---- 本地编写的测试与自动化脚本不参与交付完整性描述 ----

    def test_local_scripts_cannot_be_cited_or_mentioned(self) -> None:
        local = self._check("A-artifact-check-03", "A", "验收", "node /tmp/accept.mjs", False, "404", localScript=True)
        evidence = self._evidence(extra=[local])
        errors = self._errors(self._draft(A={"evidenceIds": ["A-artifact-check-03"]}), evidence)
        self.assertTrue(any("本地编写的测试" in item for item in errors), errors)
        recording = {"id": "A-recording", "side": "A", "type": "artifact", "polarity": "positive",
                     "artifact": {"ok": True, "observedFailure": False, "recordingMode": "terminal"}}
        errors = self._errors(self._draft(A={"evidenceIds": ["A-recording"]}), self._evidence(extra=[recording]))
        self.assertTrue(any("本地编写的测试" in item for item in errors), errors)
        desc = "登录请求在frontend/src/api.ts里多拼了一层前缀，验收脚本跑出接口返回404，保存和发布需求都没法验证。"
        errors = self._errors(self._draft(A={"description": desc}))
        self.assertTrue(any("自动化脚本" in item for item in errors), errors)

    def test_local_script_failures_do_not_drive_consistency(self) -> None:
        local_build = self._check("B-artifact-check-03", "B", "启动冒烟", "bash /tmp/smoke-start.sh", False, localScript=True)
        result = validate_delivery(self._draft(), self._evidence(extra=[local_build]), self.REASON)
        self.assertTrue(result["ok"], result)

    def test_verifier_flags_untracked_scripts(self) -> None:
        import artifact_verifier
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "scripts").mkdir()
            (repo / "scripts" / "check.sh").write_text("exit 0\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            (repo / "accept.sh").write_text("exit 0\n", encoding="utf-8")
            flag = artifact_verifier.is_local_script_check
            self.assertFalse(flag({"command": "bash scripts/check.sh"}, repo))
            self.assertFalse(flag({"command": "npm run build"}, repo))
            self.assertTrue(flag({"command": "bash accept.sh"}, repo))
            self.assertTrue(flag({"command": "node /tmp/e2e.mjs"}, repo))
            self.assertTrue(flag({"command": "npx playwright test"}, repo))
            self.assertTrue(flag({"command": "npm test", "localScript": True}, repo))

    def test_verifier_probe_records_status(self) -> None:
        import artifact_verifier

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")

            def log_message(self, *args) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            result = artifact_verifier.run_probe(base, {"method": "POST", "path": "/api/users/login", "expectStatus": 200})
        finally:
            server.shutdown()
        self.assertEqual(result["status"], 404)
        self.assertFalse(result["ok"])
        self.assertIn("返回 404", result["error"])

    def test_build_values_maps_new_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir()
            trace_a, trace_b, prompt = root / "a.jsonl", root / "b.jsonl", root / "p.md"
            for path in (trace_a, trace_b, prompt):
                path.write_text("x", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "promptPath": str(prompt), "taskType": "feature迭代", "difficulty": "困难",
                "repoUrl": "https://github.com/o/r", "initialSnapshot": "a" * 40,
                "sides": {
                    "A": {"tracePath": str(trace_a), "sessionId": "s1", "harnessVersion": "2.1.197",
                          "artifactSnapshotUrl": "https://github.com/o/r/commit/" + "b" * 40},
                    "B": {"tracePath": str(trace_b), "sessionId": "s2",
                          "artifactSnapshotUrl": "https://github.com/o/r/commit/" + "c" * 40},
                },
            })
            schema = read_json(ROOT / "references" / "gsb-form-schema.json", {})
            draft = {**self._draft(), "languages": "TypeScript", "repro_level": "无外部依赖"}
            values = build_values(root, draft, schema)
            self.assertEqual(values["a_score_delivery"], 2)
            self.assertEqual(values["b_desc_delivery"], self.DESC_B)
            self.assertNotIn("remark", values)
            draft["delivery"]["A"]["score"] = "2"
            with self.assertRaises(Exception):
                build_values(root, draft, schema)


class ReasonFlowTests(unittest.TestCase):
    def test_consecutive_same_label_sentences_block(self) -> None:
        reason = "A 侧方案验证并发保存只成功一次。A 侧方案发布时先锁行再生成快照。B 侧方案登录、存草稿和发布全部通过。"
        self.assertTrue(any("起头" in item for item in reason_flow_errors(reason)))

    def test_label_overuse_and_fragments_block(self) -> None:
        reason = (
            "A 侧方案先改了服务层。随后B 侧方案补了校验。A 侧方案又改了页面。"
            "接着B 侧方案回读数据。A 侧方案重跑构建。最后B 侧方案通过。A 侧方案失败。B 侧方案更好。来自接口返回。"
        )
        errors = reason_flow_errors(reason)
        self.assertTrue(any("超过 3 次" in item for item in errors), errors)
        self.assertTrue(any("碎句" in item for item in errors), errors)

    def test_natural_reason_passes(self) -> None:
        reason = (
            "A 侧方案在打开api.ts的请求定义时多拼了一层前缀，结果登录一直被挡在外面，页面进不去日记。"
            "B 侧方案补跑了发布流程并回读快照，存草稿和发布都能走通，但页面样式还比较简单。"
            "这个任务最重要的是能完整走通，因此选择 B 侧方案。"
        )
        self.assertEqual(reason_flow_errors(reason), [])

    def test_telegraphic_reason_blocks(self) -> None:
        reason = (
            "A 侧方案在服务层加了乐观锁。并发保存先提交标题未更新。B 侧方案补了版本号校验。随后回读了数据。"
            "A 侧方案页面能打开。B 侧方案发布成功。两侧都跑了build。这个任务最重要的是并发保存不丢数据，因此选择 B 侧方案。"
        )
        errors = reason_flow_errors(reason)
        self.assertTrue(any("电报体" in item for item in errors), errors)
        self.assertTrue(any("衔接" in item for item in errors), errors)
        self.assertTrue(any("先提交标题未更新" in item for item in errors), errors)

    def test_missing_criterion_blocks(self) -> None:
        reason = (
            "A 侧方案在服务层给文章表加了乐观锁，但两个人同时保存时，先提交的一方写进去了，标题却没有跟着更新。"
            "结果刷新页面后看到的还是旧标题，这次编辑等于没有保存。"
            "B 侧方案在保存接口里补了版本号校验，后提交的一方会收到冲突提示，回读时标题和正文都是新值。"
            "因此选择 B 侧方案，它保存后的标题和正文都可以放心交给编辑继续使用。"
        )
        self.assertTrue(any("扣分点" in item for item in reason_flow_errors(reason)))


class DeliverySubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        for attr, name, path in (
            ("preflight", "sologsb_submit_preflight_delivery", "preflight.py"),
            ("submit_api", "sologsb_submit_api_delivery", "submit_api.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, ROOT / "submission" / "scripts" / path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            setattr(cls, attr, module)

    def test_delivery_dedup_against_history(self) -> None:
        old = "已核对登录、选择行程、保存草稿、发起人发布和冻结回读，均正常完成；页面刷新后版本与发布状态一致。"
        history = {"items": [{"id": 8224, "aDescDelivery": "", "bDescDelivery": old}]}
        exact = self.preflight.assess_delivery_dedup({"A": "登录请求多拼了一层前缀，接口返回404，用户进不了页面。", "B": old}, history)
        self.assertEqual(exact["decision"], "EXACT")
        unique = self.preflight.assess_delivery_dedup(
            {"A": "登录请求多拼了一层前缀，接口返回404，用户进不了页面。", "B": "逐条试过开单、改价和作废，库存扣减与回滚都对得上。"},
            history,
        )
        self.assertIn(unique["decision"], {"UNIQUE", "REVIEW_REQUIRED"})

    def test_page_schema_matches_official_snapshot(self) -> None:
        page = self.preflight.page_schema()
        official = read_json(ROOT / "references" / "gsb-form-schema.json", {})
        self.assertEqual([f["key"] for f in page["fields"]], [f["field_key"] for f in official["fields"]])
        self.assertEqual(page["form"]["fieldCount"], len(page["fields"]))

    def test_number_fields_are_sent_as_int(self) -> None:
        schema = read_json(ROOT / "references" / "gsb-form-schema.json", {})
        labels = {f["field_key"]: f["label"] for f in schema["fields"]}
        values = {label: "x" for label in labels.values()}
        values[labels["a_score_delivery"]] = "2"
        values[labels["b_score_delivery"]] = "5"
        uploaded = {key: "u" for key in ("a_trace_file", "b_trace_file", "a_screencast", "b_screencast")}
        data = self.submit_api.build_submission_data(schema, {"fields": values}, uploaded)
        self.assertEqual((data["a_score_delivery"], data["b_score_delivery"]), (2, 5))
        values[labels["a_score_delivery"]] = ""
        with self.assertRaises(RuntimeError):
            self.submit_api.build_submission_data(schema, {"fields": values}, uploaded)


class VideoTests(unittest.TestCase):
    def test_default_web_plan_detects_pnpm_and_script_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source" / "a"
            repo.mkdir(parents=True)
            write_json(repo / "package.json", {
                "scripts": {
                    "dev": "vite --host 0.0.0.0 --port 18415",
                    "build": "vue-tsc --noEmit && vite build",
                }
            })
            (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "taskName": "demo",
                "source": {"projectCode": "cy-415", "projectName": "二手闲置物品交换平台"},
            })
            plan = default_plan(root, "A")
            self.assertEqual(plan["mode"], "web")
            self.assertEqual(plan["startCommand"], "pnpm dev")
            self.assertEqual(plan["appUrl"], "http://127.0.0.1:18415")
            self.assertTrue(any("approve-builds --all" in item for item in plan["buildCommands"]))
            self.assertIn("pnpm build", plan["buildCommands"])
            self.assertEqual(plan["preflightCommands"], [])
            self.assertEqual(plan["captureKind"], "window-id")
            self.assertEqual(plan["pointerStrategy"], "none")
            self.assertNotIn("countdownSeconds", plan)

    def test_window_info_payload_keeps_stable_window_identity(self) -> None:
        payload = _window_info_payload(
            {
                "kCGWindowNumber": 6457,
                "kCGWindowOwnerPID": 768,
                "kCGWindowOwnerName": "Otty",
                "kCGWindowName": "sologsb-a",
                "kCGWindowLayer": 0,
                "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 1280, "Height": 720},
            }
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["windowId"], 6457)
        self.assertEqual(payload["ownerPid"], 768)
        self.assertEqual(payload["bounds"], "10,20,1280,720")

    def test_window_info_payload_accepts_mapping_not_plain_dict(self) -> None:
        payload = _window_info_payload(
            {
                "kCGWindowNumber": 6457,
                "kCGWindowOwnerPID": 768,
                "kCGWindowOwnerName": "Otty",
                "kCGWindowName": "sologsb-a",
                "kCGWindowLayer": 0,
                "kCGWindowBounds": UserDict(
                    {"X": 10, "Y": 20, "Width": 1280, "Height": 720}
                ),
            }
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["bounds"], "10,20,1280,720")

    def test_window_validation_rejects_minimized_or_owner_change(self) -> None:
        expected = {
            "windowId": 6457,
            "ownerPid": 768,
            "ownerName": "Otty",
            "windowName": "sologsb-a",
            "bounds": "10,20,1280,720",
        }
        live = {
            "kCGWindowNumber": 6457,
            "kCGWindowOwnerPID": 768,
            "kCGWindowOwnerName": "Otty",
            "kCGWindowName": "sologsb-a",
            "kCGWindowIsOnscreen": True,
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 10, "Y": 20, "Width": 1280, "Height": 720},
        }
        with mock.patch("recorder._window_list", return_value=[live]):
            self.assertEqual(_validate_recording_window(expected)["windowId"], 6457)
        with mock.patch("recorder._window_list", return_value=[]):
            with self.assertRaisesRegex(SologsbError, "不存在、已最小化或不在当前 Space"):
                _validate_recording_window(expected)
        changed = dict(live)
        changed["kCGWindowOwnerPID"] = 999
        with mock.patch("recorder._window_list", return_value=[changed]):
            with self.assertRaisesRegex(SologsbError, "窗口所有者已变化"):
                _validate_recording_window(expected)

    def test_pointer_guard_never_moves_host_mouse(self) -> None:
        self.assertEqual(normalize_pointer_strategy("background"), "none")
        self.assertEqual(normalize_pointer_strategy("park-pointer"), "none")
        self.assertEqual(normalize_pointer_strategy("none"), "none")
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "segment-cursor-guard.json"
            guard = _MouseCursorGuard(
                "100,100,1280,720",
                report_path,
                segment="segment",
                window_id=6457,
                pointer_strategy="park-pointer",
            )
            guard.start()
            guard.stop()
            report = read_json(report_path)
            self.assertEqual(report["pointerStrategy"], "none")
            self.assertEqual(report["pointerPolicy"], "host-input-untouched")
            self.assertTrue(report["hostInputRespected"])
            self.assertFalse(report["pointerMoved"])
            self.assertFalse(report["mouseButtonsQueried"])
            self.assertFalse(report["parkApplied"])

    def test_web_mode_captures_real_otty_pane_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "terminal.log"
            proc = subprocess.CompletedProcess(
                args=["otty-cli"],
                returncode=0,
                stdout=b"server ready\nGET /api/items 200\n",
                stderr=b"",
            )
            with mock.patch("recorder._otty_call", return_value=proc) as call:
                text = _capture_otty_pane_text("pane-1", output, lines=400)
            self.assertIn("GET /api/items 200", text)
            self.assertIn("GET /api/items 200", output.read_text(encoding="utf-8"))
            self.assertIn("--lines", call.call_args.args[0])

    def test_recording_service_ports_only_uses_local_targets(self) -> None:
        ports = _recording_service_ports(
            {
                "appUrl": "http://127.0.0.1:18415",
                "apiRequests": [{"url": "http://localhost:8080/api/items"}],
                "startCommand": "node server.js --port 9091",
                "commands": ["vite -p 5174"],
            }
        )
        self.assertEqual(ports, [5174, 8080, 9091, 18415])

    def test_late_otty_window_after_open_timeout_is_cleaned_up(self) -> None:
        with mock.patch("recorder._otty_call", side_effect=SologsbError("IPC response timed out")):
            with mock.patch(
                "recorder._otty_json",
                return_value=[{"id": "w_late", "title": "sologsb-late-window"}],
            ):
                with mock.patch("recorder._otty_close_window") as close_window:
                    with self.assertRaisesRegex(SologsbError, "IPC response timed out"):
                        _otty_open_window("sologsb-late-window")
        close_window.assert_called_once_with("w_late")

    def test_recording_isolation_gate_requires_window_ids(self) -> None:
        captures = [{"status": "ok", "captureKind": "window-id", "captureBackend": "screen-capture-kit", "showsCursor": False, "cursorCaptured": False, "windowId": 6457, "ownerPid": 768, "ownerName": "Otty"}]
        guards = [{
            "status": "ok",
            "pointerStrategy": "none",
            "pointerPolicy": "host-input-untouched",
            "hostInputRespected": True,
            "pointerMoved": False,
            "mouseButtonsQueried": False,
            "parkApplied": False,
            "finalPointerInsideWindow": True,
        }]
        frontmost = {"status": "ok", "sampleCount": 12, "recordingWindowFrontmostSamples": 0}
        service_cleanup = {"status": "ok", "residualAppPortListeners": []}
        self.assertTrue(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=captures,
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=[{"status": "ok", "captureKind": "screen-crop", "windowId": 1, "ownerPid": 1}],
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=[{"status": "ok", "captureKind": "window-id", "windowId": 0, "ownerPid": 1}],
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=[{
                    "status": "ok",
                    "captureKind": "window-id",
                    "windowId": 6457,
                    "ownerPid": 768,
                    "ownerName": "Otty",
                }],
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        web_captures = [
            {"status": "ok", "captureKind": "window-id", "captureBackend": "screen-capture-kit", "showsCursor": False, "cursorCaptured": False, "windowId": 6457, "ownerPid": 768, "ownerName": "Otty"},
            {"status": "ok", "captureKind": "window-id", "captureBackend": "screen-capture-kit", "showsCursor": False, "cursorCaptured": False, "windowId": 6458, "ownerPid": 769, "ownerName": "Chrome"},
        ]
        self.assertTrue(
            recording_isolation_ok(
                mode="web",
                window_capture_reports=web_captures,
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="web",
                window_capture_reports=captures,
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=captures,
                guard_reports=guards,
                frontmost_report={"status": "ok", "sampleCount": 12, "recordingWindowFrontmostSamples": 1},
                service_cleanup=service_cleanup,
            )
        )
        self.assertFalse(
            recording_isolation_ok(
                mode="terminal",
                window_capture_reports=captures,
                guard_reports=guards,
                frontmost_report=frontmost,
                service_cleanup={"status": "failed", "residualAppPortListeners": [{"pid": 1}]},
            )
        )

    def test_screen_capturekit_source_disables_cursor(self) -> None:
        source = SCK_RECORDER_SOURCE.read_text(encoding="utf-8")
        self.assertIn("configuration.showsCursor = false", source)
        self.assertIn("configuration.showMouseClicks = false", source)
        self.assertIn("configuration.capturesAudio = false", source)

    def test_screen_capturekit_recorder_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "window-recorder"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            with mock.patch.dict(os.environ, {
                "SOLOSB_SCK_RECORDER": str(binary),
            }, clear=False):
                self.assertEqual(_ensure_sck_recorder(), binary)
            binary.chmod(0o644)
            with mock.patch.dict(os.environ, {
                "SOLOSB_SCK_RECORDER": str(binary),
            }, clear=False):
                with self.assertRaisesRegex(SologsbError, "不可执行"):
                    _ensure_sck_recorder()

    def test_window_id_gate_rejects_missing_identity(self) -> None:
        self.assertEqual(_require_window_id({"windowId": 6457, "ownerPid": 768}, "Otty")["windowId"], 6457)
        with self.assertRaisesRegex(SologsbError, "无法定位Otty窗口ID"):
            _require_window_id({"windowId": 0, "ownerPid": 0}, "Otty")

    def test_recording_plan_uses_mapped_candidate_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source" / "candidates" / "candidate-3"
            repo.mkdir(parents=True)
            write_json(repo / "package.json", {"scripts": {"dev": "vite --port 18415"}})
            write_json(root / "monitor" / "state.json", {
                "taskName": "demo",
                "sides": {
                    "A": {
                        "candidateId": "candidate-3",
                        "workspacePath": str(repo),
                    }
                },
            })
            plan = default_plan(root, "A")
            self.assertEqual(plan["projectDir"], str(repo.resolve()))

    def test_default_scenario_calls_out_tabs_and_repeated_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            write_json(root / "monitor" / "state.json", {
                "source": {"projectCode": "cy-415", "projectName": "二手闲置物品交换平台"},
            })
            repo = root / "source" / "a"
            repo.mkdir(parents=True)
            write_json(repo / "package.json", {"scripts": {"dev": "vite --port 18415"}})
            prepare_recording(root, "A")
            text = (root / "workspace" / "视频信息" / "a" / "脚本" / "scenario.cjs").read_text(encoding="utf-8")
            self.assertIn("默认 Tab", text)
            self.assertIn("last()", text)

    def test_pnpm_suggested_plan_handles_build_approval_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            write_json(repo / "package.json", {"scripts": {"build": "vite build", "test": "node test.mjs"}})
            (repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            plan = suggest_plan(repo)
            commands = [item["command"] for item in plan["a"]]
            self.assertTrue(any("approve-builds --all" in command for command in commands))
            self.assertIn("pnpm build", commands)

    def test_unexpected_browser_failure_is_not_recording_success(self) -> None:
        self.assertTrue(recording_command_ok(0, False))
        self.assertFalse(recording_command_ok(1, False))
        self.assertTrue(recording_command_ok(1, True))
        with self.assertRaises(SologsbError):
            recording_command_ok(0, True)

    def test_only_terminal_and_chrome_are_allowed(self) -> None:
        self.assertEqual(validate_recording_targets("web", ["Otty", "Chrome"]), ["Otty", "Chrome"])
        self.assertEqual(validate_recording_targets("terminal", ["Otty"]), ["Otty"])
        with self.assertRaises(Exception):
            validate_recording_targets("web", ["iTerm2", "Chrome"], "iterm2")
        with self.assertRaises(Exception):
            validate_recording_targets("web", ["Otty", "Chrome", "Finder"])

    def test_backend_plan_requires_api_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source" / "a"
            repo.mkdir(parents=True)
            (repo / "go.mod").write_text("module demo\n\ngo 1.22\n", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "source": {"projectCode": "cy-701", "projectName": "纯后端服务"},
            })
            plan = default_plan(root, "A")
            self.assertEqual(plan["mode"], "terminal")
            self.assertTrue(plan["requiresApiRequests"])
            self.assertEqual(plan["apiRequests"], [])
            with self.assertRaises(Exception) as raised:
                validate_api_requests(plan)
            self.assertIn("apiRequests", str(raised.exception))

    def test_node_backend_plan_uses_terminal_api_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source" / "a"
            (repo / "src").mkdir(parents=True)
            write_json(repo / "package.json", {
                "scripts": {"start": "node src/server.js --port 8080"},
                "dependencies": {"express": "4.19.2"},
            })
            (repo / "src" / "server.js").write_text("// server\n", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "source": {"projectCode": "cy-702", "projectName": "Node 纯后端"},
            })
            plan = default_plan(root, "A")
            self.assertEqual(plan["mode"], "terminal")
            self.assertEqual(plan["targetApps"], ["Otty"])
            self.assertTrue(plan["requiresApiRequests"])
            self.assertEqual(plan["apiBaseUrl"], "http://127.0.0.1:8080")

    def test_api_request_plan_runs_real_requests_and_extracts_variable(self) -> None:
        server = _api_fixture_server()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "backend"
                project.mkdir()
                port = server.server_address[1]
                plan = {
                    "mode": "terminal",
                    "projectDir": str(project),
                    "startCommand": "true",
                    "commands": ["echo READY"],
                    "cleanupCommands": ["echo CLEANUP"],
                    "apiRequests": [
                        {
                            "name": "登录获取 token",
                            "method": "POST",
                            "url": f"http://127.0.0.1:{port}/api/login",
                            "headers": {"Content-Type": "application/json"},
                            "body": {"username": "tester"},
                            "expectedStatus": 200,
                            "expectContains": ["token"],
                            "extract": {"name": "token", "path": "$.data.token"},
                        },
                        {
                            "name": "带 token 查询订单",
                            "method": "GET",
                            "url": f"http://127.0.0.1:{port}/api/orders",
                            "headers": {"Authorization": "Bearer {{token}}"},
                            "expectedStatus": [200],
                            "expectContains": ['"id": 7'],
                        },
                    ],
                }
                script = root / "run-terminal.sh"
                log = root / "terminal.log"
                result = root / "exit-code.txt"
                _terminal_command(plan, log, script, result)
                proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
                output = log.read_text(encoding="utf-8")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("请求: POST http://127.0.0.1:", output)
                self.assertIn("已提取变量: token", output)
                self.assertIn("请求: GET http://127.0.0.1:", output)
                self.assertEqual(output.count("结果: PASS"), 2)
                self.assertIn("CLEANUP", output)
                self.assertIn("__EXIT_CODE__=0", result.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()

    def test_api_request_status_mismatch_marks_failure(self) -> None:
        server = _api_fixture_server()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "backend"
                project.mkdir()
                port = server.server_address[1]
                plan = {
                    "mode": "terminal",
                    "projectDir": str(project),
                    "startCommand": "true",
                    "apiRequests": [
                        {
                            "name": "状态码不匹配",
                            "method": "POST",
                            "url": f"http://127.0.0.1:{port}/api/login",
                            "body": {"username": "tester"},
                            "expectedStatus": 201,
                        }
                    ],
                }
                script = root / "run-terminal.sh"
                log = root / "terminal.log"
                result = root / "exit-code.txt"
                _terminal_command(plan, log, script, result)
                proc = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
                output = log.read_text(encoding="utf-8")
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("API_CHECK_FAILED: 期望状态 201，实际 200", output)
                self.assertIn("__EXIT_CODE__=1", result.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()

    def test_final_video_is_720p(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "color=c=blue:s=640x360:d=1", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            target = _copy_final(source, root, "A", "demo.mp4")
            self.assertEqual(video_dimensions(target), (1280, 720))


class RecordingLockTests(unittest.TestCase):
    def test_global_recording_lock_reports_owner_and_serializes_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            write_json(root / "monitor" / "state.json", {
                "source": {"projectCode": "cy-402", "projectName": "承岳律师案件管理系统"},
            })
            lock_path = root / "locks" / "recording.lock"
            with mock.patch.dict(os.environ, {"SOLOGBS_0917_RECORDING_LOCK": str(lock_path)}):
                with global_recording_lock(root, side="A", timeout=0) as first:
                    payload = read_json(lock_path)
                    self.assertEqual(first["status"], "acquired")
                    self.assertEqual(payload["side"], "A")
                    self.assertEqual(payload["projectCode"], "cy-402")
                    self.assertEqual(payload["projectLabel"], "cy-402 / 承岳律师案件管理系统")
                    with self.assertRaises(SologsbError) as raised:
                        with global_recording_lock(root, side="B", timeout=0.05):
                            pass
                    self.assertIn("等待全局录制锁超时", str(raised.exception))
                    self.assertIn("cy-402", str(raised.exception))
                self.assertEqual(read_json(lock_path)["status"], "released")
                with global_recording_lock(root, side="B", timeout=0) as second:
                    self.assertEqual(second["side"], "B")


class ProjectClaimTests(unittest.TestCase):
    def test_project_claim_is_shared_and_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            claim_root = base / "claims"
            with mock.patch.dict(
                os.environ,
                {"SOLO2_PLATFORM_CLAIM_ROOT": str(claim_root)},
            ):
                first_root = base / "task-1"
                second_root = base / "task-2"
                claim = start_project_claim(
                    first_root,
                    "http://platform.test",
                    "cy-501",
                    project_name="测试项目",
                )
                self.assertEqual(claim["claimedBy"], "sologsb-0917")
                self.assertEqual(
                    claimed_project_codes("http://platform.test"),
                    {"cy-501"},
                )
                with self.assertRaises(SologsbError) as raised:
                    start_project_claim(
                        second_root,
                        "http://platform.test",
                        "cy-501",
                    )
                self.assertIn("已被其他会话占用", str(raised.exception))
                released = release_project_claim(first_root)
                self.assertEqual(released["status"], "released")
                self.assertEqual(claimed_project_codes("http://platform.test"), set())

    def _write_submission(self, task_root: Path, status: str) -> None:
        write_json(
            task_root / "monitor" / "submission" / "api-result.json",
            {"submissionId": "7356", "statusValue": status},
        )

    def test_project_claim_released_after_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with mock.patch.dict(os.environ, {"SOLO2_PLATFORM_CLAIM_ROOT": str(base / "claims")}):
                root = base / "task"
                start_project_claim(root, "http://platform.test", "gb-14-1")
                self.assertEqual(claim_release_reason(root), "")
                self.assertEqual(release_claim_if_finished(root)["status"], "kept")
                self._write_submission(root, "PENDING_FIX")
                self.assertEqual(release_claim_if_finished(root)["status"], "kept")
                self.assertEqual(claimed_project_codes("http://platform.test"), {"gb-14-1"})
                self._write_submission(root, "QC_PASSED")
                released = release_claim_if_finished(root)
                self.assertEqual(released["status"], "released")
                self.assertEqual(released["reason"], "submitted:QC_PASSED")
                self.assertEqual(claimed_project_codes("http://platform.test"), set())
                self.assertEqual(release_claim_if_finished(root)["status"], "not_found")

    def test_claim_holder_exits_on_its_own_after_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            env = {
                "SOLO2_PLATFORM_CLAIM_ROOT": str(base / "claims"),
                "SOLOGBS_CLAIM_HOLDER_POLL_SECONDS": "0.2",
            }
            with mock.patch.dict(os.environ, env):
                root = base / "task"
                claim = start_project_claim(root, "http://platform.test", "gb-14-1")
                self._write_submission(root, "QC_PASSED")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and claimed_project_codes("http://platform.test"):
                    time.sleep(0.1)
                self.assertEqual(claimed_project_codes("http://platform.test"), set())
                marker = read_json(root / "monitor" / "platform-claim.json", {})
                self.assertEqual(marker.get("releaseReason"), "submitted:QC_PASSED")
                self.assertEqual(marker.get("holderPid"), claim["holderPid"])
                # A second task can take the project right away.
                second = base / "task-2"
                start_project_claim(second, "http://platform.test", "gb-14-1")
                # Late cleanup of the first task must not drop the second task's claim.
                self.assertEqual(release_project_claim(root)["stoppedHolder"], False)
                self.assertEqual(claimed_project_codes("http://platform.test"), {"gb-14-1"})
                release_project_claim(second)
                self.assertEqual(claimed_project_codes("http://platform.test"), set())

    def test_only_live_runners_make_a_task_active(self) -> None:
        from project_claims import _task_is_active

        dead = {"status": "running", "runPid": 999_999}
        live = {"status": "running", "runPid": os.getpid()}
        with mock.patch("project_claims.os.kill",
                        side_effect=lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError())
                        if pid == 999_999 else None):
            self.assertFalse(_task_is_active({"status": "blocked", "candidates": {
                "candidate-1": {"status": "blocked"}, "candidate-2": {"status": "attempt_invalid"}}}))
            self.assertFalse(_task_is_active({"status": "candidates_running",
                                              "candidates": {"candidate-1": dead}}))
            self.assertTrue(_task_is_active({"status": "candidates_running",
                                             "candidates": {"candidate-1": live}}))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write_submission(root, "QC_PASSED")
            self.assertFalse(_task_is_active({"candidates": {"candidate-1": live}}, root))

    def test_concurrent_platform_selection_uses_distinct_projects(self) -> None:
        projects = [
            {
                "id": "p-1",
                "code": "cy-601",
                "name": "项目一",
                "readinessStatus": "RUNNABLE",
                "variants": [{
                    "id": "v-1",
                    "directoryName": "one",
                    "sourceAvailable": True,
                    "sourceAsset": {"id": "asset-1"},
                }],
            },
            {
                "id": "p-2",
                "code": "cy-602",
                "name": "项目二",
                "readinessStatus": "RUNNABLE",
                "variants": [{
                    "id": "v-2",
                    "directoryName": "two",
                    "sourceAvailable": True,
                    "sourceAsset": {"id": "asset-2"},
                }],
            },
        ]

        class FakePlatformBridge:
            urllib = urllib

            def load_manager_token(self) -> str:
                return "token"

            def api_json(self, meta, path, token=""):
                del meta, token
                if path.startswith("/projects/mine"):
                    return {"items": projects}
                return {"items": []}

            def api_download(self, meta, request, destination: Path, token) -> None:
                del meta, request, token
                time.sleep(0.15)
                with zipfile.ZipFile(destination, "w") as archive:
                    archive.writestr("README.md", "source")

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with mock.patch.dict(
                os.environ,
                {"SOLO2_PLATFORM_CLAIM_ROOT": str(base / "claims")},
            ), mock.patch.object(
                source_ingest,
                "_load_platform_bridge",
                return_value=FakePlatformBridge(),
            ), mock.patch.object(
                source_ingest,
                "running_container_project_codes",
                return_value=(set(), "单元测试"),
            ):
                results: list[dict] = []
                errors: list[Exception] = []

                def ingest(index: int) -> None:
                    try:
                        task_root = base / f"task-{index}"
                        results.append(
                            source_ingest.ingest_source(
                                task_root / "source" / "origin",
                                from_platform=True,
                                platform_base_url="http://platform.test",
                                workdir=base,
                                task_root=task_root,
                            )
                        )
                    except Exception as exc:  # pragma: no cover - assertion aid
                        errors.append(exc)

                threads = [threading.Thread(target=ingest, args=(index,)) for index in (1, 2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertEqual(errors, [])
                self.assertEqual({item["projectCode"] for item in results}, {"cy-601", "cy-602"})
                for index in (1, 2):
                    release_project_claim(base / f"task-{index}")


class SemanticGateTests(unittest.TestCase):
    def _trace(self, path: Path, content: str) -> None:
        events = [
            {"type": "user", "uuid": "u1", "sessionId": "s1", "message": {"role": "user", "content": "prompt"}},
            {"type": "assistant", "uuid": "a1", "sessionId": "s1", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": content}]}},
        ]
        path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

    def test_review_requires_trace_and_artifact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "source" / "a"
            repo.mkdir(parents=True)
            (repo / "result.txt").write_text("ok", encoding="utf-8")
            trace = root / "trace.jsonl"
            self._trace(trace, "需求已经完成")
            write_json(root / "monitor" / "state.json", {
                "sides": {"A": {"status": "staged", "tracePath": str(trace)}}
            })
            review = root / "a.review.json"
            write_json(review, {
                "side": "A", "completed": True, "interrupted": False, "unfinished": [],
                "requirements": [{"requirement": "完成模块", "status": "satisfied", "evidence": [
                    {"type": "trace", "eventIndex": 1, "quote": "需求已经完成"},
                    {"type": "artifact", "path": "result.txt", "quote": "ok"},
                ]}],
                "reason": "轨迹和产物都证明该需求已经完成，可以发布。", "reviewedBy": "Codex",
            })
            self.assertTrue(validate_review(root, "A", review)["ok"])


class TransportValidationTests(unittest.TestCase):
    def test_api_retry_is_counted_but_not_a_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stdout.jsonl"
            path.write_text(
                json.dumps({"type": "system", "subtype": "api_retry", "error_status": 504, "error": "server_error"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(side_runner._api_retry_events(path)), 1)
            self.assertEqual(side_runner._api_transport_error(path), "")
            failed = Path(temp) / "failed.jsonl"
            failed.write_text(
                json.dumps({"type": "system", "subtype": "api_error", "error_status": 504, "error": "server_error"}) + "\n",
                encoding="utf-8",
            )
            self.assertIn("API/网络错误", side_runner._api_transport_error(failed))


class ParallelRunTests(unittest.TestCase):
    def test_run_defaults_to_headless_and_both_is_available(self) -> None:
        parser = build_parser()
        default = parser.parse_args(["run", "--task-root", "/tmp/task", "--side", "both"])
        custom_base = parser.parse_args([
            "run", "--task-root", "/tmp/task", "--side", "both",
            "--base-url", "https://llm.example.com/",
        ])
        live = parser.parse_args(["run", "--task-root", "/tmp/task", "--side", "both", "--live"])
        legacy_off = parser.parse_args(["run", "--task-root", "/tmp/task", "--side", "both", "--no-live"])
        semantic_a = parser.parse_args(["semantic", "--task-root", "/tmp/task", "--side", "A"])
        self.assertFalse(default.live)
        self.assertTrue(live.live)
        self.assertFalse(legacy_off.live)
        self.assertEqual(default.candidates, 2)
        self.assertEqual(default.attempts, 6)
        self.assertEqual(default.base_url, "")
        self.assertEqual(custom_base.base_url, "https://llm.example.com/")
        with mock.patch.dict(os.environ, {"SOLOSB_ANTHROPIC_BASE_URL": "https://custom.example/"}, clear=False):
            self.assertEqual(side_runner.anthropic_base_url(), "https://custom.example")
        self.assertEqual(semantic_a.side, "A")

    def test_candidate_race_maps_first_two_by_finish_order_without_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            origin = root / "source" / "origin"
            origin.mkdir(parents=True)
            (origin / "README.md").write_text("base", encoding="utf-8")
            prompt = root / "prompt.txt"
            prompt.write_text("prompt", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "status": "prompt_ready",
                "taskName": "candidate-race",
                "promptPath": str(prompt),
                "promptSha256": sha256_file(prompt),
            })

            def fake_candidate(task_root, candidate, **kwargs):
                stop_event = kwargs["stop_event"]
                workspace = task_root / "source" / "candidates" / candidate
                self.assertTrue((workspace / ".git").is_dir())
                (workspace / "result.txt").write_text(candidate, encoding="utf-8")
                if candidate == "candidate-1":
                    time.sleep(0.08)
                elif candidate == "candidate-2":
                    stop_event.wait(timeout=5)
                    return {
                        "candidateId": candidate,
                        "status": "cancelled",
                        "error": "stopped after first two",
                    }
                elif candidate == "candidate-3":
                    time.sleep(0.01)
                trace = task_root / "workspace" / "轨迹文件" / "candidates" / candidate / f"{candidate}.jsonl"
                trace.parent.mkdir(parents=True, exist_ok=True)
                session = f"s-{candidate}"
                events = [
                    {"type": "user", "uuid": "u", "sessionId": session, "message": {"role": "user", "content": "prompt"}},
                    {"type": "assistant", "uuid": "a", "sessionId": session, "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "完成"}]}},
                ]
                trace.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")
                return {
                    "candidateId": candidate,
                    "attempt": 1,
                    "status": "staged",
                    "sessionId": session,
                    "candidateTracePath": str(trace),
                    "tracePath": str(trace),
                    "changedFiles": ["result.txt"],
                    "diffStat": "",
                }

            with mock.patch.object(side_runner, "_ensure_image", return_value="image"):
                with mock.patch.object(side_runner, "_run_candidate_locked", side_effect=fake_candidate):
                    result = side_runner.run_both(root, timeout=10, live=False, candidate_count=3)

            self.assertEqual(result["candidateMapping"]["A"]["candidateId"], "candidate-3")
            self.assertEqual(result["candidateMapping"]["B"]["candidateId"], "candidate-1")
            self.assertIn("candidate-2", result["cancelledCandidates"])
            for candidate in ("candidate-1", "candidate-2", "candidate-3"):
                self.assertTrue((root / "source" / "candidates" / candidate).is_dir())
            self.assertTrue((root / "workspace" / "轨迹文件" / "a" / "candidate-3.jsonl").is_file())
            self.assertTrue((root / "workspace" / "轨迹文件" / "a").is_dir())
            self.assertTrue((root / "workspace" / "轨迹文件" / "b").is_dir())

    def test_candidate_uses_six_actual_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_json(root / "monitor" / "state.json", {
                "status": "prompt_ready",
                "initialSnapshot": "a" * 40,
                "candidates": {"candidate-1": {"candidateId": "candidate-1", "status": "idle"}},
            })

            def always_invalid(**kwargs):
                attempt = kwargs["attempt"]
                return {
                    "candidateId": "candidate-1",
                    "attempt": attempt,
                    "status": "attempt_invalid",
                    "error": f"invalid-{attempt}",
                }

            with mock.patch.object(side_runner, "_run_candidate_attempt", side_effect=always_invalid) as attempt_mock:
                with self.assertRaisesRegex(SologsbError, "连续 6 次"):
                    side_runner._run_candidate_locked(
                        root,
                        "candidate-1",
                        timeout=1,
                        live=False,
                        attempts=6,
                    )
            self.assertEqual(attempt_mock.call_count, 6)

    def test_attempt_backoff_grows_and_is_capped(self) -> None:
        with mock.patch.dict(os.environ, {"SOLOGBS_ATTEMPT_BACKOFF_SECONDS": "30"}):
            self.assertEqual(
                [side_runner._attempt_backoff(n) for n in range(1, 7)],
                [30, 60, 120, 240, 300, 300],
            )
        with mock.patch.dict(os.environ, {"SOLOGBS_ATTEMPT_BACKOFF_SECONDS": "0"}):
            self.assertEqual(side_runner._attempt_backoff(3), 0.0)

    def test_failed_attempts_back_off_and_stop_cuts_the_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            write_json(root / "monitor" / "state.json", {
                "status": "prompt_ready",
                "initialSnapshot": "a" * 40,
                "candidates": {"candidate-1": {"candidateId": "candidate-1", "status": "idle"}},
            })
            stop = threading.Event()
            calls: list[float] = []

            def invalid_then_stop(**kwargs):
                calls.append(time.monotonic())
                threading.Timer(0.3, stop.set).start()
                return {"candidateId": "candidate-1", "attempt": kwargs["attempt"],
                        "status": "attempt_invalid", "error": "API Error: 429"}

            with mock.patch.dict(os.environ, {"SOLOGBS_ATTEMPT_BACKOFF_SECONDS": "60"}):
                with mock.patch.object(side_runner, "_run_candidate_attempt", side_effect=invalid_then_stop):
                    started = time.monotonic()
                    result = side_runner._run_candidate_locked(
                        root, "candidate-1", timeout=1, live=False, attempts=6, stop_event=stop,
                    )
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(len(calls), 1)
            self.assertLess(time.monotonic() - started, 10)

    def test_slot_wait_is_cancelled_by_stop_event(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            limiter = side_runner._ContainerLimiter()
            limiter.reservations = Path(temp) / "reservations"
            limiter.lock_path = Path(temp) / "slots.lock"
            stop = threading.Event()
            stop.set()
            with mock.patch.object(limiter, "_settings", return_value=(1, set(), 14400.0)):
                with self.assertRaises(side_runner.CandidateCancelled):
                    limiter.acquire("cy-1", "sologsb-x-candidate-3-1-abc", stop)

    def test_orphan_containers_with_dead_runner_are_reaped(self) -> None:
        dead_pid = 999_999
        listing = (
            f"sologsb-a-candidate-1-1-aaa\t{dead_pid}\n"
            f"sologsb-b-candidate-1-1-bbb\t{os.getpid()}\n"
            "sologsb-c-candidate-1-1-ccc\t\n"
        ).encode()
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            stdout = listing if cmd[:2] == ["docker", "ps"] else b""
            return subprocess.CompletedProcess(cmd, 0, stdout, b"")

        with mock.patch.object(side_runner, "run", side_effect=fake_run), \
                mock.patch.object(side_runner._ContainerLimiter, "_pid_alive",
                                  side_effect=lambda pid: int(pid) != dead_pid):
            removed = side_runner._ContainerLimiter._reap_orphan_containers()
        self.assertEqual(removed, ["sologsb-a-candidate-1-1-aaa"])
        self.assertIn(["docker", "rm", "-f", "sologsb-a-candidate-1-1-aaa"], calls)

    def test_github_init_is_forbidden_before_candidate_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            (root / "source" / "origin").mkdir(parents=True)
            (root / "source" / "origin" / "README.md").write_text("base", encoding="utf-8")
            write_json(root / "monitor" / "state.json", {"status": "prompt_ready", "taskName": "gate"})
            with self.assertRaisesRegex(SologsbError, "候选竞速"):
                init_github_repo(root, dry_run=True)

    def test_github_repo_base_uses_platform_project_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            (root / "source" / "origin").mkdir(parents=True)
            write_json(root / "monitor" / "platform-selection.json", {
                "selection": {"projectCode": "cy-291"},
            })
            state = {"taskName": "某项目"}
            self.assertEqual(_derive_repo_base(root / "source" / "origin", state, "cy-291"), "cy-291")
            self.assertEqual(_normalize_repo_prefix("idea-name", "cy-291"), "cy-291")
            self.assertRegex(f"cy-291-{_random_code()}", r"^cy-291-[a-z0-9]{4}$")


class ParallelPublishTests(unittest.TestCase):
    def test_two_staged_sides_publish_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "source" / "origin"
            origin.mkdir(parents=True)
            (origin / "README.md").write_text("base", encoding="utf-8")
            initial = _ensure_origin_commit(origin, "chore: initial environment snapshot")
            bare = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            _push_topology(origin, str(bare), initial)
            for side in ("A", "B"):
                _clone_branch(str(bare), side, root / "source" / side.lower(), initial)
                (root / "source" / side.lower() / f"{side}.txt").write_text(side, encoding="utf-8")
                (root / "source" / side.lower() / "app.ts").write_text(
                    "\n".join(f"export const {side.lower()}{i} = {i};" for i in range(12)) + "\n",
                    encoding="utf-8",
                )
                trace = root / "workspace" / "轨迹文件" / side.lower() / f"{side}.jsonl"
                trace.parent.mkdir(parents=True, exist_ok=True)
                events = [
                    {"type": "user", "uuid": "u", "sessionId": f"s-{side}", "message": {"role": "user", "content": "prompt"}},
                    {"type": "assistant", "uuid": "a", "sessionId": f"s-{side}", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "完成"}]}},
                ]
                trace.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")
                review = root / "monitor" / "semantic" / f"{side.lower()}.review.json"
                write_json(review, {
                    "side": side, "completed": True, "interrupted": False, "unfinished": [],
                    "requirements": [{"requirement": "完成", "status": "satisfied", "evidence": [
                        {"type": "trace", "eventIndex": 1, "quote": "完成"},
                        {"type": "artifact", "path": f"{side}.txt", "quote": side},
                    ]}],
                    "reason": "轨迹与产物均证明目标已经完成，可以原子发布。", "reviewedBy": "Codex",
                })
            write_json(root / "monitor" / "state.json", {
                "status": "semantic_review_required", "owner": "smoke", "repoUrl": str(bare),
                "remoteUrl": str(bare), "initialSnapshot": initial,
                "sides": {side: {"status": "staged", "sessionId": f"s-{side}", "tracePath": str(root / "workspace" / "轨迹文件" / side.lower() / f"{side}.jsonl")} for side in ("A", "B")},
            })
            result = side_runner.publish_sides(
                root,
                semantic_a=root / "monitor" / "semantic" / "a.review.json",
                semantic_b=root / "monitor" / "semantic" / "b.review.json",
            )
            self.assertEqual(result["status"], "ab_clean")
            self.assertNotEqual(result["remoteHeads"]["A"], initial)
            self.assertNotEqual(result["remoteHeads"]["B"], initial)
            self.assertNotEqual(result["remoteHeads"]["A"], result["remoteHeads"]["B"])

    def test_candidate_folders_publish_to_mapped_a_b_branches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "source" / "origin"
            origin.mkdir(parents=True)
            (origin / "README.md").write_text("base", encoding="utf-8")
            initial = _ensure_origin_commit(origin, "chore: initial environment snapshot")
            bare = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            _push_topology(origin, str(bare), initial)
            mapping = {"A": "candidate-3", "B": "candidate-1"}
            for side, candidate in mapping.items():
                workspace = root / "source" / "candidates" / candidate
                _clone_branch(str(bare), "main", workspace, initial)
                (workspace / "result.txt").write_text(candidate, encoding="utf-8")
                (workspace / "app.ts").write_text(
                    "\n".join(f"export const {candidate.replace('-', '')}{i} = {i};" for i in range(12)) + "\n",
                    encoding="utf-8",
                )
                trace = root / "workspace" / "轨迹文件" / side.lower() / f"{candidate}.jsonl"
                trace.parent.mkdir(parents=True, exist_ok=True)
                events = [
                    {"type": "user", "uuid": "u", "sessionId": f"s-{candidate}", "message": {"role": "user", "content": "prompt"}},
                    {"type": "assistant", "uuid": "a", "sessionId": f"s-{candidate}", "message": {"role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text", "text": "完成"}]}},
                ]
                trace.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")
                review = root / "monitor" / "semantic" / f"{side.lower()}.review.json"
                write_json(review, {
                    "side": side, "completed": True, "interrupted": False, "unfinished": [],
                    "requirements": [{"requirement": "完成", "status": "satisfied", "evidence": [
                        {"type": "trace", "eventIndex": 1, "quote": "完成"},
                        {"type": "artifact", "path": "result.txt", "quote": candidate},
                    ]}],
                    "reason": "轨迹与产物均证明目标已经完成，可以原子发布。", "reviewedBy": "Codex",
                })
            write_json(root / "monitor" / "state.json", {
                "status": "semantic_review_required", "owner": "smoke", "repoUrl": str(bare),
                "remoteUrl": str(bare), "initialSnapshot": initial,
                "sides": {
                    side: {
                        "status": "staged",
                        "sessionId": f"s-{candidate}",
                        "candidateId": candidate,
                        "workspacePath": str(root / "source" / "candidates" / candidate),
                        "tracePath": str(root / "workspace" / "轨迹文件" / side.lower() / f"{candidate}.jsonl"),
                    }
                    for side, candidate in mapping.items()
                },
            })
            side_runner.publish_sides(
                root,
                semantic_a=root / "monitor" / "semantic" / "a.review.json",
                semantic_b=root / "monitor" / "semantic" / "b.review.json",
            )
            self.assertTrue((root / "source" / "candidates" / "candidate-3").is_dir())
            self.assertTrue((root / "source" / "candidates" / "candidate-1").is_dir())
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(bare), "show", "refs/heads/A:result.txt"],
                    check=True, capture_output=True, text=True,
                ).stdout,
                "candidate-3",
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "--git-dir", str(bare), "show", "refs/heads/B:result.txt"],
                    check=True, capture_output=True, text=True,
                ).stdout,
                "candidate-1",
            )


class PublishGateTests(unittest.TestCase):
    def test_low_volume_still_creates_snapshot_and_records_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "tester"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "tester@example.com"], check=True)
            (repo / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
            initial = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (repo / "app.ts").write_text("export const value = 2;\nexport const extra = 3;\n", encoding="utf-8")
            result = side_runner._commit_local(repo, "A", initial)
            self.assertNotEqual(result["artifactSnapshot"], initial)
            self.assertFalse(result["lineGate"]["hardOk"])
            self.assertEqual(result["lineGate"]["status"], "failed")
            self.assertEqual(result["lineGate"]["blockingStage"], "submit_preflight")

    def test_no_commit_or_push_before_both_semantic_reviews_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            origin = root / "source" / "origin"
            origin.mkdir(parents=True)
            (origin / "README.md").write_text("base", encoding="utf-8")
            initial = _ensure_origin_commit(origin, "chore: initial environment snapshot")
            bare = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            _push_topology(origin, str(bare), initial)
            for side in ("A", "B"):
                _clone_branch(str(bare), side, root / "source" / side.lower(), initial)
                (root / "source" / side.lower() / f"{side}.txt").write_text(side, encoding="utf-8")
            write_json(root / "monitor" / "state.json", {
                "status": "semantic_review_required", "owner": "smoke", "repoUrl": str(bare),
                "remoteUrl": str(bare), "initialSnapshot": initial,
                "sides": {side: {"status": "staged"} for side in ("A", "B")},
            })
            with self.assertRaises(SologsbError):
                side_runner.publish_sides(
                    root,
                    semantic_a=root / "monitor" / "semantic" / "a.review.json",
                    semantic_b=root / "monitor" / "semantic" / "b.review.json",
                )
            for side in ("A", "B"):
                local_head = subprocess.run(
                    ["git", "-C", str(root / "source" / side.lower()), "rev-parse", "HEAD"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                remote_head = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", f"refs/heads/{side}"],
                    check=True, capture_output=True, text=True,
                ).stdout.strip()
                self.assertEqual(local_head, initial)
                self.assertEqual(remote_head, initial)


class SourceTests(unittest.TestCase):
    def test_local_source_copy_skips_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            origin = base / "origin"
            source.mkdir()
            (source / ".git").mkdir()
            (source / ".git" / "config").write_text("secret", encoding="utf-8")
            (source / "README.md").write_text("hello", encoding="utf-8")
            result = ingest_source(origin, source=source)
            self.assertEqual(result["fileCount"], 1)
            self.assertTrue((origin / "README.md").is_file())
            self.assertFalse((origin / ".git").exists())


class GitTopologyTests(unittest.TestCase):
    def test_main_a_b_parent_relation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            origin = base / "origin"
            bare = base / "remote.git"
            origin.mkdir()
            (origin / "README.md").write_text("hello", encoding="utf-8")
            sha = _ensure_origin_commit(origin, "chore: initial environment snapshot")
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            heads = _push_topology(origin, str(bare), sha)
            self.assertEqual(set(heads), {"main", "A", "B"})
            self.assertEqual(heads["A"], sha)
            self.assertEqual(heads["B"], sha)


class MiscTests(unittest.TestCase):
    def test_slug(self) -> None:
        self.assertEqual(safe_slug("中文 Project / A"), "Project-A")

    def test_recording_name_uses_project_code_name_and_side(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "monitor").mkdir(parents=True)
            write_json(root / "monitor" / "state.json", {"taskName": "fallback"})
            write_json(root / "monitor" / "platform-selection.json", {
                "selection": {"projectCode": "cy-402", "projectName": "承岳律师案件管理系统"}
            })
            self.assertEqual(
                recording_output_name(root, "A"),
                "cy-402-承岳律师案件管理系统-验证A产物.mp4",
            )
            self.assertEqual(
                recording_output_name(root, "B"),
                "cy-402-承岳律师案件管理系统-验证B产物.mp4",
            )


class ChangeVolumeGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "sologsb_submit_preflight", ROOT / "submission" / "scripts" / "preflight.py"
        )
        cls.preflight = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.preflight)

    def _git(self, repo: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_line_gate_is_exception_only_when_sole_blocker(self) -> None:
        checks = [
            {"id": "code-volume-hard-a", "ok": False, "severity": "blocker"},
            {"id": "code-volume-hard-b", "ok": True, "severity": "blocker"},
            {"id": "reason-markdown", "ok": True, "severity": "blocker"},
        ]
        code_change = {
            "errors": [],
            "okHygiene": True,
            "sides": {"A": {"hardOk": False}, "B": {"hardOk": True}},
        }
        result = self.preflight.classify_change_volume_line_gate(
            checks, code_change, skip_remote=False
        )
        self.assertTrue(result["onlyBlocker"], result)
        self.assertEqual(result["failedSides"], ["A"])

        checks_with_other_blocker = checks + [
            {"id": "reason-process-product", "ok": False, "severity": "blocker"}
        ]
        blocked = self.preflight.classify_change_volume_line_gate(
            checks_with_other_blocker, code_change, skip_remote=False
        )
        self.assertFalse(blocked["onlyBlocker"], blocked)
        self.assertEqual(blocked["otherBlockingCheckIds"], ["reason-process-product"])

    def test_publish_excludes_generated_paths_and_counts_business_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.name", "tester")
            self._git(repo, "config", "user.email", "tester@example.com")
            (repo / "app.ts").write_text("export const value = 1;\n", encoding="utf-8")
            (repo / "package-lock.json").write_text('{"version":1}\n', encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "base")
            initial = self._git(repo, "rev-parse", "HEAD")

            (repo / "app.ts").write_text(
                "\n".join(f"export const value{i} = {i};" for i in range(12)) + "\n",
                encoding="utf-8",
            )
            (repo / "package-lock.json").write_text('{"version":2}\n', encoding="utf-8")
            (repo / "node_modules").mkdir()
            (repo / "node_modules" / "dep.js").write_text("x\n" * 50, encoding="utf-8")
            side_runner._install_generated_path_excludes(repo)
            self._git(repo, "add", "-A")
            removed = side_runner._unstage_generated_paths(repo, initial)
            lines, files = side_runner._staged_business_change_lines(repo, initial)

            self.assertEqual(removed, ["package-lock.json"])
            self.assertEqual(lines, 12)
            self.assertEqual(files, ["app.ts"])
            self.assertNotIn("node_modules/dep.js", self._git(repo, "diff", "--cached", "--name-only", initial))

    def test_paired_lockfiles_follow_manifest_changes(self) -> None:
        from common import paired_lockfiles
        self.assertEqual(
            paired_lockfiles(["frontend/package.json", "frontend/package-lock.json", "backend/yarn.lock"]),
            {"frontend/package-lock.json"},
        )
        # workspace：锁文件在根目录，清单在子目录
        self.assertEqual(paired_lockfiles(["pnpm-lock.yaml", "apps/web/package.json"]), {"pnpm-lock.yaml"})
        self.assertEqual(paired_lockfiles(["backend/go.mod", "backend/go.sum"]), {"backend/go.sum"})
        # go.sum 只补校验记录也要发布，否则干净检出后 go build 报 missing go.sum entry
        self.assertEqual(paired_lockfiles(["backend/go.sum"]), {"backend/go.sum"})
        # 清单在别的目录，不配对
        self.assertEqual(paired_lockfiles(["backend/package.json", "frontend/package-lock.json"]), set())
        self.assertEqual(paired_lockfiles(["package-lock.json"]), set())

    def test_publish_keeps_lockfile_when_manifest_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self._git(repo, "init", "-b", "main")
            self._git(repo, "config", "user.name", "tester")
            self._git(repo, "config", "user.email", "tester@example.com")
            (repo / "frontend").mkdir()
            (repo / "frontend" / "package.json").write_text('{"dependencies":{}}\n', encoding="utf-8")
            (repo / "frontend" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-m", "base")
            initial = self._git(repo, "rev-parse", "HEAD")

            (repo / "frontend" / "package.json").write_text('{"dependencies":{"dayjs":"1.11.0"}}\n', encoding="utf-8")
            (repo / "frontend" / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            (repo / "frontend" / "node_modules").mkdir()
            (repo / "frontend" / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")
            side_runner._install_generated_path_excludes(repo)
            self._git(repo, "add", "-A")
            removed = side_runner._unstage_generated_paths(repo, initial)
            staged = self._git(repo, "diff", "--cached", "--name-only", initial).split()

            self.assertEqual(removed, [])
            self.assertIn("frontend/package-lock.json", staged)
            self.assertIn("frontend/package.json", staged)
            self.assertNotIn("frontend/node_modules/dep.js", staged)
            lines, files = side_runner._staged_business_change_lines(repo, initial)
            self.assertNotIn("frontend/package-lock.json", files)

    def test_old_exclude_file_drops_lockfile_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self._git(repo, "init", "-b", "main")
            exclude = repo / ".git" / "info" / "exclude"
            exclude.write_text(
                "# sologsb-generated-artifacts\nnode_modules/\npackage-lock.json\ngo.sum\ndist/\n", encoding="utf-8"
            )
            side_runner._install_generated_path_excludes(repo)
            text = exclude.read_text(encoding="utf-8")
            self.assertIn("node_modules/", text)
            self.assertIn("dist/", text)
            self.assertNotIn("package-lock.json", text)
            self.assertNotIn("go.sum", text)

    def test_preflight_still_flags_unpaired_lockfile(self) -> None:
        self.assertTrue(self.preflight._is_generated_or_lock_path("frontend/package-lock.json"))
        self.assertTrue(self.preflight._is_generated_or_lock_path("frontend/node_modules/x.js"))
        self.assertFalse(self.preflight._is_generated_or_lock_path("frontend/package.json"))
        self.assertEqual(
            self.preflight.paired_lockfiles(["frontend/package.json", "frontend/package-lock.json"]),
            {"frontend/package-lock.json"},
        )

    def test_submission_preflight_rejects_recording_runtime_factors(self) -> None:
        result = self.preflight.assess_reason_quality({
            "reason": (
                "A 侧方案首页正常返回，查询接口返回预期数据；"
                "B 侧录屏中提交表单返回 500，数据没有写入。"
            ),
            "claims": [
                {
                    "polarity": "negative",
                    "trigger": "提交表单时",
                    "objectiveConsequence": "提交表单返回 500，数据没有写入",
                }
            ],
        })
        self.assertFalse(result["ok"])
        self.assertTrue(any("录屏" in item for item in result["errors"]), result)

        acceptance_result = self.preflight.assess_reason_quality({
            "reason": (
                "A 侧方案编写验收脚本后页面正常返回，查询接口返回预期数据；"
                "B 侧方案提交表单返回 500，数据没有写入。"
            ),
            "claims": [
                {
                    "polarity": "negative",
                    "trigger": "提交表单时",
                    "objectiveConsequence": "提交表单返回 500，数据没有写入",
                }
            ],
        })
        self.assertFalse(acceptance_result["ok"])
        self.assertTrue(
            any("非容器内生成的测试文件" in item for item in acceptance_result["errors"]),
            acceptance_result,
        )

    def test_submission_preflight_requires_both_layers_per_side(self) -> None:
        reason = (
            "A 侧方案升级迁移里旧唯一约束没有去掉，升级后索引列表仍含 `idx_run_input_version`；"
            "B 侧方案接口响应仍缺少版本字段。"
        )
        result = self.preflight.assess_reason_quality({
            "reason": reason,
            "claims": [
                {"side": "A", "type": "process", "polarity": "positive", "text": "A 侧方案升级迁移"},
                {
                    "side": "A",
                    "type": "artifact",
                    "polarity": "positive",
                    "text": "A 侧方案升级迁移里旧唯一约束没有去掉，升级后索引列表仍含 `idx_run_input_version`",
                },
                {"side": "B", "type": "process", "polarity": "positive", "text": "B 侧方案接口返回"},
                {
                    "side": "B",
                    "type": "artifact",
                    "polarity": "positive",
                    "text": "B 侧方案接口响应仍缺少版本字段",
                },
            ],
        })
        self.assertFalse(result["ok"], result)
        self.assertFalse(result["layerCoverage"]["sides"]["A-process"], result)
        self.assertFalse(result["layerCoverage"]["sides"]["B-process"], result)
        self.assertTrue(result["layerCoverage"]["sides"]["A-artifact"], result)
        self.assertTrue(result["layerCoverage"]["sides"]["B-artifact"], result)
        self.assertTrue(any("A 侧缺少写入 GSB 理由的具体过程事实" in item for item in result["errors"]), result)
        self.assertTrue(any("B 侧缺少写入 GSB 理由的具体过程事实" in item for item in result["errors"]), result)

    def test_gsb_reason_dedup_detects_b5_common_fragment(self) -> None:
        historical = "A先把接口补完整并接通页面;B则遗漏了保存链路。两次运行分别依据各自的轨迹判断。"
        candidate = "A先补齐接口并接通页面，但B没有保存链路。这里补充另一组产物差异。"
        history = {
            "items": [
                {
                    "id": 2090,
                    "gsbReason": historical,
                    "questionType": "0-1代码生成",
                    "difficulty": "困难",
                }
            ]
        }
        result = self.preflight.assess_gsb_reason_dedup(candidate, history)
        self.assertNotEqual(result["decision"], "UNIQUE")
        self.assertEqual(result["matches"][0]["id"], 2090)
        self.assertTrue(result["matches"][0]["rule"].startswith("B-5."))
        self.assertIn("实际轨迹", result["rewriteInstruction"])

    def test_gsb_reason_dedup_accepts_distinct_trace_evidence(self) -> None:
        history = {
            "items": [
                {
                    "id": 1888,
                    "gsbReason": "A在订单导出入口遗漏权限过滤，导致普通用户可下载他人订单；B已补齐校验并保留退出码证据。",
                }
            ]
        }
        candidate = "A在日程回退时重复触发通知，导致同一条提醒发送两次；B改为幂等写入并用接口返回码验证。"
        result = self.preflight.assess_gsb_reason_dedup(candidate, history)
        self.assertEqual(result["decision"], "UNIQUE")

    def test_gsb_history_cache_is_written_and_used_on_fetch_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "gsb-history-cache.json"

            def readonly(path: str) -> dict:
                if path.startswith("/api/v1/gsb/submissions?"):
                    return {"meta": {"total": 1, "total_pages": 1}, "items": [{"id": 2090}]}
                if path == "/api/v1/gsb/submissions/2090":
                    return {
                        "id": 2090,
                        "status_label": "已交付",
                        "user_prompt": "历史提示词",
                        "gsb_reason": "历史 GSB 理由",
                        "a_session_id": "a-old",
                        "b_session_id": "b-old",
                    }
                raise AssertionError(path)

            with mock.patch.dict(os.environ, {self.preflight.GSB_HISTORY_CACHE_ENV: str(cache)}):
                with mock.patch.object(self.preflight, "_readonly_json", side_effect=readonly):
                    live = self.preflight.fetch_gsb_prompt_history(set(), set(), force_refresh=True)
                self.assertTrue(cache.is_file())
                self.assertEqual(live["items"][0]["gsbReason"], "历史 GSB 理由")
                with mock.patch.object(self.preflight, "_readonly_json", side_effect=RuntimeError("offline")):
                    fallback = self.preflight.fetch_gsb_prompt_history(set(), set(), force_refresh=True)
                self.assertTrue(fallback["cacheHit"])
                self.assertFalse(fallback["cacheFresh"])
                self.assertIn("offline", fallback["fetchError"])
                self.assertEqual(fallback["items"][0]["gsbReason"], "历史 GSB 理由")

    def test_manual_history_merges_for_inaccessible_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            live_cache = root / "live.json"
            manual_cache = root / "manual.json"
            manual_cache.write_text(json.dumps({
                "items": [
                    {"id": 2090, "gsb_reason": "平台反馈的无权读取历史理由"},
                    {"id": 2091},
                ]
            }, ensure_ascii=False), encoding="utf-8")

            def readonly(path: str) -> dict:
                if path.startswith("/api/v1/gsb/submissions?"):
                    return {"meta": {"total": 0, "total_pages": 1}, "items": []}
                raise AssertionError(path)

            with mock.patch.dict(os.environ, {
                self.preflight.GSB_HISTORY_CACHE_ENV: str(live_cache),
                self.preflight.GSB_HISTORY_MANUAL_CACHE_ENV: str(manual_cache),
            }):
                with mock.patch.object(self.preflight, "_readonly_json", side_effect=readonly):
                    history = self.preflight.fetch_gsb_prompt_history(set(), set(), force_refresh=True)
            self.assertEqual(history["manualCount"], 2)
            self.assertEqual({item["id"] for item in history["items"]}, {2090, 2091})
            self.assertEqual(self.preflight.unresolved_history_ids(history), [2091])
            item_2090 = next(item for item in history["items"] if item["id"] == 2090)
            self.assertEqual(item_2090["gsbReason"], "平台反馈的无权读取历史理由")

    def test_g11_gate_blocks_low_volume_and_dirty_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            bare = root / "remote.git"
            work.mkdir()
            self._git(work, "init", "-b", "main")
            self._git(work, "config", "user.name", "tester")
            self._git(work, "config", "user.email", "tester@example.com")
            (work / "src").mkdir()
            (work / "src" / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "base")
            base = self._git(work, "rev-parse", "HEAD")

            self._git(work, "checkout", "-b", "A")
            (work / "src" / "app.ts").write_text(
                "\n".join(f"export const a{i} = {i};" for i in range(12)) + "\n",
                encoding="utf-8",
            )
            (work / "node_modules").mkdir()
            (work / "node_modules" / "dep.js").write_text("x\n" * 50, encoding="utf-8")
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "A")
            sha_a = self._git(work, "rev-parse", "HEAD")

            self._git(work, "checkout", "main")
            self._git(work, "checkout", "-b", "B")
            (work / "src" / "app.ts").write_text(
                "\n".join(f"export const b{i} = {i};" for i in range(5)) + "\n",
                encoding="utf-8",
            )
            self._git(work, "add", "-A")
            self._git(work, "commit", "-m", "B")
            sha_b = self._git(work, "rev-parse", "HEAD")
            self._git(work, "checkout", "main")
            subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)

            result = self.preflight.verify_code_change(
                {"remoteUrl": f"file://{bare}", "initialSnapshot": base},
                {"heads": {"main": base, "A": sha_a, "B": sha_b}},
            )
            self.assertGreaterEqual(result["sides"]["A"]["lines"], 10)
            self.assertLess(result["sides"]["B"]["lines"], 10)
            self.assertIn("node_modules/dep.js", result["badPaths"])
            self.assertFalse(result["ok"])
            self.assertFalse(result["okHygiene"])


class SubmitApprovalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location(
            "sologsb_submit_api", ROOT / "submission" / "scripts" / "submit_api.py"
        )
        cls.submit_api = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.submit_api)

    def _bundle(self, root: Path) -> tuple[Path, dict]:
        pre_submit = root / "workspace" / "评审文件" / "pre-submit"
        pre_submit.mkdir(parents=True)
        sheet = pre_submit / "交付表.xlsx"
        sheet.write_bytes(b"sheet")
        uploads = {}
        for label, filename in (
            ("A-轨迹文件", "a.jsonl"),
            ("A-运行录屏", "a.mp4"),
            ("B-轨迹文件", "b.jsonl"),
            ("B-运行录屏", "b.mp4"),
        ):
            path = pre_submit / filename
            path.write_bytes(filename.encode())
            uploads[label] = {
                "path": str(path),
                "sha256": self.submit_api.sha256_file(path),
                "kind": "video" if filename.endswith(".mp4") else "trace",
            }
        payload = {
            "schemaVersion": 1,
            "taskRoot": str(root),
            "harnessVersion": "2.1.197",
            "deliverySheet": {"path": str(sheet), "sha256": self.submit_api.sha256_file(sheet)},
            "uploads": uploads,
        }
        payload_path = pre_submit / "submission-payload.json"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return payload_path, payload

    def test_complete_audit_gets_automatic_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload_path, payload = self._bundle(root)
            payload["approvalPolicy"] = "automatic"
            payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            approval_path = root / "workspace" / "评审文件" / "pre-submit" / "submission-approval.json"
            approval = self.submit_api.verify_approval(
                payload_path,
                payload,
                approval_path,
                {"status": "pass", "submissionPayloadSha256": self.submit_api.sha256_file(payload_path)},
            )
            self.assertEqual(approval["approvedBy"], self.submit_api.AUTO_APPROVER)
            self.assertEqual(approval["approvalKind"], "automatic")
            self.assertTrue(approval_path.is_file())

    def test_line_gate_exception_requires_configured_approver_and_exact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payload_path, payload = self._bundle(root)
            review_payload = {
                "schemaVersion": 1,
                "id": "change-volume-line-gate",
                "initialSnapshot": "a" * 40,
                "hardMinimumLines": 10,
                "targetLines": 30,
                "failedSides": ["B"],
                "onlyBlocker": True,
                "codeChange": {"sides": {"A": {"hardOk": True}, "B": {"hardOk": False}}},
            }
            review_sha = self.submit_api.sha256_json(review_payload)
            review_path = root / "workspace" / "评审文件" / "pre-submit" / "change-volume-review.json"
            review_path.write_text(
                json.dumps({**review_payload, "reviewSha256": review_sha, "checkedAt": "2026-09-22T00:00:00Z"}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            payload["approvalPolicy"] = "change-volume-line-gate"
            payload["changeVolumeLineGate"] = {
                "required": True,
                "failedSides": ["B"],
                "reviewPath": str(review_path),
                "reviewSha256": review_sha,
            }
            payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            preflight = {
                "submissionPayloadSha256": self.submit_api.sha256_file(payload_path),
                "lineGate": {"onlyBlocker": True, "reviewSha256": review_sha},
            }
            approval_path = root / "workspace" / "评审文件" / "pre-submit" / "change-volume-line-gate-approval.json"
            approval = {
                "schemaVersion": 2,
                "manualConfirmed": True,
                "approvalKind": "manual",
                "scope": "change-volume-line-gate",
                "approvedBy": self.submit_api.AUTO_APPROVER,
                "taskRoot": str(root),
                "payloadPath": str(payload_path.resolve()),
                "payloadSha256": self.submit_api.sha256_file(payload_path),
                "deliverySheetSha256": payload["deliverySheet"]["sha256"],
                "changeVolumeReviewPath": str(review_path),
                "changeVolumeReviewSha256": review_sha,
                "failedSides": ["B"],
                "harnessVersion": "2.1.197",
                "uploads": {
                    label: {"path": info["path"], "sha256": info["sha256"]}
                    for label, info in payload["uploads"].items()
                },
            }
            approval_path.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            verified = self.submit_api.verify_approval(payload_path, payload, approval_path, preflight)
            self.assertEqual(verified["approvedBy"], self.submit_api.AUTO_APPROVER)
            with self.assertRaisesRegex(RuntimeError, "唯一阻断项"):
                self.submit_api.verify_approval(
                    payload_path,
                    payload,
                    approval_path,
                    {**preflight, "lineGate": {"onlyBlocker": False, "reviewSha256": review_sha}},
                )


class ContainerLimitTests(unittest.TestCase):
    def test_device_config_limit_defaults_to_four(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "container-limit.json"
            device_config = Path(temp) / "config.json"
            write_json(device_config, {"configVersion": 1})
            with mock.patch.dict(os.environ, {
                "SOLOSB_CONFIG": str(device_config),
                "SOLOSB_MAX_CONTAINERS": "",
            }, clear=False):
                limit, _, _ = side_runner._ContainerLimiter(config)._settings()
            self.assertEqual(limit, 4)

    def test_device_config_limit_overrides_legacy_file_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "container-limit.json"
            device_config = Path(temp) / "config.json"
            write_json(config, {"maxContainers": 2, "waitSeconds": 1})
            write_json(device_config, {"claude": {"maxContainers": "3"}})
            with mock.patch.dict(os.environ, {
                "SOLOSB_CONFIG": str(device_config),
                "SOLOSB_MAX_CONTAINERS": "1",
            }, clear=False):
                limit, _, _ = side_runner._ContainerLimiter(config)._settings()
            self.assertEqual(limit, 3)

    def test_monitor_managed_limit_overrides_device_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "container-limit.json"
            device_config = Path(temp) / "config.json"
            write_json(config, {"maxContainers": 5, "managedBy": "sologsb-monitor"})
            write_json(device_config, {"claude": {"maxContainers": "3"}})
            with mock.patch.dict(os.environ, {
                "SOLOSB_CONFIG": str(device_config),
                "SOLOSB_MAX_CONTAINERS": "1",
            }, clear=False):
                limit, _, _ = side_runner._ContainerLimiter(config)._settings()
            self.assertEqual(limit, 5)

    def test_reservations_are_counted_and_wait_until_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "container-limit.json"
            device_config = root / "config.json"
            write_json(config, {"waitSeconds": 1})
            write_json(device_config, {"claude": {"maxContainers": "1"}})
            limiter = side_runner._ContainerLimiter(config)
            first = None
            second = None
            try:
                with mock.patch.dict(os.environ, {
                    "SOLOSB_CONFIG": str(device_config),
                    "SOLOSB_MAX_CONTAINERS": "",
                }, clear=False), mock.patch.object(
                    limiter, "_running_containers", return_value=[]
                ):
                    first = limiter.acquire(
                        "project-1", "sologsb-project-1-candidate-1-1-a"
                    )
                    status = limiter.status()
                    self.assertEqual(status["runningContainers"], 0)
                    self.assertEqual(status["reservedSlots"], 1)
                    self.assertEqual(status["used"], 1)
                    self.assertEqual(status["available"], 0)

                    sleeps: list[float] = []

                    def release_first(seconds: float) -> None:
                        sleeps.append(seconds)
                        assert first is not None
                        first.release()

                    with mock.patch.object(
                        side_runner.time, "sleep", side_effect=release_first
                    ):
                        second = limiter.acquire(
                            "project-2", "sologsb-project-2-candidate-1-1-a"
                        )
                    self.assertEqual(len(sleeps), 1)
                    self.assertIsNotNone(second.path)
            finally:
                if first is not None:
                    first.release()
                if second is not None:
                    second.release()

    def test_waiting_task_reloads_device_config_after_refresh_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "container-limit.json"
            device_config = root / "config.json"
            write_json(config, {"waitSeconds": 30})
            write_json(device_config, {"claude": {"maxContainers": "1"}})
            limiter = side_runner._ContainerLimiter(config)
            first = None
            second = None
            try:
                with mock.patch.dict(os.environ, {
                    "SOLOSB_CONFIG": str(device_config),
                    "SOLOSB_MAX_CONTAINERS": "",
                }, clear=False), mock.patch.object(
                    limiter, "_running_containers", return_value=[]
                ):
                    first = limiter.acquire(
                        "project-1", "sologsb-project-1-candidate-1-1-a"
                    )
                    sleeps: list[float] = []

                    def reload_limit(seconds: float) -> None:
                        sleeps.append(seconds)
                        write_json(device_config, {"claude": {"maxContainers": "2"}})

                    with mock.patch.object(
                        side_runner, "CONTAINER_SETTINGS_REFRESH_SECONDS", 0.0
                    ), mock.patch.object(
                        side_runner.time, "sleep", side_effect=reload_limit
                    ):
                        second = limiter.acquire(
                            "project-2", "sologsb-project-2-candidate-1-1-a"
                        )
                    self.assertEqual(len(sleeps), 1)
                    self.assertIsNotNone(second.path)
                    status = limiter.status()
                    self.assertEqual(status["limit"], 2)
                    self.assertEqual(status["used"], 2)
            finally:
                if first is not None:
                    first.release()
                if second is not None:
                    second.release()

    def test_absolute_cap_cannot_be_raised_by_env_or_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "container-limit.json"
            device_config = root / "config.json"
            write_json(config, {"maxContainers": 99, "excludedProjectCodes": []})
            write_json(device_config, {"claude": {"maxContainers": "99"}})
            limiter = side_runner._ContainerLimiter(config)
            with mock.patch.dict(os.environ, {
                "SOLOSB_CONFIG": str(device_config),
                "SOLOSB_MAX_CONTAINERS": "99",
            }, clear=False):
                limit, _, _ = limiter._settings()
            self.assertEqual(limit, 6)

    def test_test_project_is_excluded_and_new_container_gets_one_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "container-limit.json"
            write_json(config, {
                "maxContainers": 4,
                "excludedProjectCodes": ["gb-501"],
                "waitSeconds": 1,
            })
            limiter = side_runner._ContainerLimiter(config)
            running = [
                ("sologsb-cy-386-20260919-000745-candidate-1-1-a", "cy-386"),
                ("sologsb-cy-386-20260919-000745-candidate-2-1-b", "cy-386"),
                ("sologsb-cy-386-20260919-000745-candidate-3-1-c", "cy-386"),
                ("sologsb-gb-501-20260918-231421-candidate-1-1-a", ""),
            ]
            with mock.patch.object(limiter, "_running_containers", return_value=running):
                reservation = limiter.acquire("cy-new", "sologsb-cy-new-20260919-000000-candidate-1-1-a")
                self.assertIsNotNone(reservation.path)
                self.assertEqual(len(list(limiter.reservations.glob("*.json"))), 1)
                reservation.release()
                excluded = limiter.acquire("gb-501", "sologsb-gb-501-20260919-000000-candidate-1-1-a")
                self.assertIsNone(excluded.path)

    def test_monitor_can_make_every_running_container_count(self) -> None:
        docker_ps = "\n".join([
            "sologsb-cy-1-20260919-000745-candidate-1-1-a\tcy-1\ttrue",
            "cy180-web-1\t\t",
            "friendly_keller\t\t",
            "ld427-db\t\t",
        ]).encode("utf-8")
        ok = subprocess.CompletedProcess([], 0, stdout=docker_ps, stderr=b"")
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(side_runner, "run", return_value=ok):
            config = Path(temp) / "container-limit.json"
            write_json(config, {"maxContainers": 4, "excludedProjectCodes": ["ld427"],
                                "managedBy": "sologsb-monitor"})
            limiter = side_runner._ContainerLimiter(config)
            self.assertEqual(limiter.status()["runningContainers"], 1)
            write_json(config, {"maxContainers": 4, "excludedProjectCodes": ["ld427"],
                                "managedBy": "sologsb-monitor", "countAllContainers": True})
            status = limiter.status()
            self.assertEqual(status["runningContainers"], 3)
            self.assertNotIn("ld427-db", status["runningNames"])


class VersionTests(unittest.TestCase):
    """全局版本号只有一个来源：根目录 VERSION。"""

    def test_version_file_is_the_single_source(self) -> None:
        info = skill_version_info()
        self.assertRegex(info.get("version", ""), r"^\d+\.\d+\.\d+$")
        self.assertEqual(info.get("release_tag"), f"v{info.get('version')}")
        self.assertEqual(info.get("branch"), "main")
        self.assertIn("codex-skill-sologsb-0917", info.get("repository", ""))

    def test_skill_md_agrees_with_version_file(self) -> None:
        info = skill_version_info()
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"全局版本号：`{info['version']}`", text)
        self.assertIn(f"发布标签：`{info['release_tag']}`", text)

    def test_skill_version_helper_matches(self) -> None:
        self.assertEqual(skill_version(), skill_version_info().get("version"))

    def test_cli_reports_the_same_version(self) -> None:
        cli = ROOT / "scripts" / "sologsb.py"
        flag = subprocess.run(
            [sys.executable, str(cli), "--version"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(flag.returncode, 0, flag.stderr)
        self.assertEqual(flag.stdout.strip(), f"sologsb-0917 {skill_version()}")
        command = subprocess.run(
            [sys.executable, str(cli), "version"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(command.returncode, 0, command.stderr)
        payload = json.loads(command.stdout)
        self.assertEqual(payload["version"], skill_version())
        self.assertEqual(payload["releaseTag"], skill_version_info().get("release_tag"))


if __name__ == "__main__":
    unittest.main()
