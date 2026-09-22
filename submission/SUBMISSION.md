---
name: gsb-submit-api
description: >
  审核并通过 Python HTTP API 提交 SOLO2 GSB 数据，不使用浏览器模拟点击。
  先执行只读预检、历史提示词与 GSB 理由双重去重和交付哈希校验；完整审核通过后自动批准并记录 liudong，只有改动量低于 10 行且为唯一阻断项时才要求 liudong 例外审批，最后上传轨迹/录屏并调用
  /api/v1/gsb/submissions，轮询 /api/v1/gsb/submissions/{id} 直到质检终态。
  用于“GSB 提交”“提交 0917 数据”“用 Python 提交 GSB”“提取独立提交技能”等场景。
---

# GSB Submit API

这个技能把 GSB 提交从浏览器表单中抽离出来。任何上传或提交都必须显式加 `--execute`；不带该参数只作诊断。完整审核通过时自动生成批准记录，`approvedBy=liudong`。只有 `change-volume-line-gate` 例外需要与 payload、交付表和改动量复核哈希一致的人工审批。

## 依赖

`python3`、`requests`、`openpyxl` 和 `ffprobe`。预检脚本只读，不触碰对象存储。

## 固定接口

- 表单配置：`GET /api/v1/gsb/form-schema`
- 上传轨迹：`POST /api/v1/submissions/upload`，multipart `file`，响应取 `name/path/size`
- 上传录屏：`POST /api/v1/submissions/upload`，multipart `file` 加 `kind=video`，响应取 `url`
- 创建 GSB：`POST /api/v1/gsb/submissions`，JSON `{data, schema_fingerprint}`
- 查询质检：`GET /api/v1/gsb/submissions/{id}`

认证读取 `SOLO_QA_COOKIE` / `SOLO_QA_CSRF`（由设备配置文件
`~/.codex/sologsb/config.json` 自动注入），或回退到 Keychain 的
`solo2-jzxhnh-cookie` / `solo2-jzxhnh-csrf`。会话失效时用配置里的账号密码自动重登，
不需要人工更换 Cookie。不把凭据写入交付物或日志。

## 提交顺序

1. 运行只读审核：

```bash
python3 scripts/preflight.py --task-root /absolute/task/root
```

2. 先跑 dry-run，确认四个上传文件、payload 和接口：

```bash
python3 scripts/submit_api.py --task-root /absolute/task/root
```

默认不会上传、不会提交。

3. 完整审核通过时直接上传并提交；系统自动批准并记录 `liudong`：

```bash
python3 scripts/submit_api.py --task-root /absolute/task/root --execute
```

4. 只有实时预检状态为 `line_gate_approval_required` 时，才由 `liudong` 在真实 TTY 中生成例外审批：

```bash
python3 scripts/confirm_submission.py --task-root /absolute/task/root
python3 scripts/submit_api.py \
  --task-root /absolute/task/root \
  --approval /absolute/task/root/workspace/评审文件/pre-submit/change-volume-line-gate-approval.json \
  --execute
```

脚本会依次上传 A/B 轨迹和录屏，调用创建接口，轮询到 `SUBMITTED` 以外的质检终态。只有
`QC_PASSED` 才返回成功；`QC_REJECTED`、`QC_DISCARDED`、`QC_FAILED`、`PENDING_FIX`
或超时均返回非零并保留原始响应到：

```text
workspace/评审文件/pre-submit/submission-api-result.json
monitor/submission/api-result.json
workspace/评审文件/pre-submit/submission-result.json  # 兼容预检去重

monitor/gsb-prompt-history.json
monitor/prompt-dedup-review.json
monitor/gsb-reason-history.json
monitor/gsb-reason-dedup-review.json
$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json
```

## 审核门禁

## G11 改动量与仓库洁净门禁

- 发布阶段仍生成 A/B 产物快照，并记录每侧业务代码行数；任一侧低于 `10` 行时 `lineGate=failed`。
- 提交预检按远端 `main` 与 A/B commit 复算。低于 `10` 行且这是唯一阻断项时，只允许 `liudong` 批准 `change-volume-line-gate` 例外。
- 例外审批绑定 payload 哈希、交付表哈希和 `change-volume-review.json` 的改动量复核哈希；任一变化都使审批失效。
- 仓库存在依赖、构建、缓存、虚拟环境或锁文件，以及其他任何门禁失败时，都不能使用该例外。
- 建议至少 `30` 行且跨 `3` 个业务文件；不足时警告并重新评估题目难度。
- 本地门禁按远端 `main` 与 A/B commit 复算，平台 `G11` 判定为最终结果。
- 详细口径见 `../references/change-volume-gate.md`。

- 提交前强制刷新历史 GSB 文案；完整 `user_prompt` 与 `gsb_reason` 持久缓存在 `$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json`，当前提交按 A/B SessionID 和 submission id 自动排除。
- `GSB 理由` 必须完成 B-5 公共长片段、模板 n-gram 和相似度去重；只要不是 `UNIQUE` 即阻断。命中后必须依据本次与历史记录的实际轨迹、commit、验证输出重写，不允许只换项目名或复用公共句式。
- `GSB 理由` 必须让 A、B 两侧都分别覆盖过程与产物。过程层写轨迹中的实际动作和定位节点；产物层写最终可观察结果。整段只写交付物毛病、只写执行动作，或只让一侧覆盖两层，均阻断。
- 不得引用录屏、视频、截图、浏览器、测试设备、运行环境、验收宿主、Otty、鼠标、分辨率、终端窗口等场外因素；这些不能进入 GSB 理由，没有例外。
- 不允许任何 Markdown 语法，包括标题、列表、代码块、行内代码、链接、图片、强调标记、表格、引用和 HTML 标签。
- 禁用“闭环”“根因”“落库”，数据写入统一写“入库”；“真实”“真正”“其实”等空泛表达会给出警告。
- 句子必须达到高中语文阅读水平且通顺，单句非空白字符不得超过 56 字；分句过多、重复标点、标点不成对、连接词堆叠、重复虚词和残句均阻断。
- 不得引用 `evaluationExcluded` 的环境或工具噪声证据。
- 禁止使用“落在……”式收束句式；结论直接写“因此选择 B 侧方案”或“B 侧方案更好”。
- 平台反馈的历史 ID 当前无权读取时，使用 `scripts/prompt_dedup.py --import-history FILE` 导入历史理由；只有 ID、没有理由文本时禁止提交。
- 禁止浏览器模拟点击、Playwright 填表和文件选择器。
- 不带 `--execute` 时只作诊断，绝不调用上传或创建接口；完整审核通过后的默认流程直接加 `--execute`。
- 正常通过时自动生成批准记录，不需要人工审批文件；如果 payload、交付表、改动量复核或上传文件哈希变化，原有例外审批会失效。
- 真实提交禁止使用 `--skip-preflight` 绕过任何门禁。
- 只有 `line_gate_approval_required` 可以使用例外审批；实时预检若出现其他阻断项，直接拒绝。
- 提交前必须确认表单 schema fingerprint、字段顺序、必填项、文件大小和视频规格。
- 视频预检必须确认 `state.recordings.<side>.ok=true`，且 `expectedAppFailure`、`observedAppFailure`、`appOutcome` 与真实浏览器退出码一致；非预期非零退出直接阻断。
- 不重复提交已有 submission id；若平台返回已提交记录，保留响应并停止。确认必须重提时才使用 `--force`。
- 备注字段绝不进入 API payload；只保留在本地 Excel/草稿中供审计。
- 提交后质检终态必须保留；不得把“已提交但待质检”写成通过。

## 最终汇报

提交后的最终回复必须严格使用
[`references/final-delivery-format.md`](references/final-delivery-format.md) 的固定八段式模板。
不得增删二级标题、添加前言或改写标题；所有本地文件使用绝对路径，视频必须内嵌。
质检结果必须写 submission id、终态和 API 结果路径；未解决事项没有时写“无”。
