# 历史 GSB 文案去重

历史来源：`https://solo2.jzxhnh.com/app/gsb/submissions`

## 本地缓存

默认持久缓存：

`$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json`

可用 `SOLOGBS_0917_GSB_HISTORY_CACHE` 覆盖在线缓存路径，用 `SOLOGBS_0917_GSB_HISTORY_MANUAL_CACHE` 覆盖手工缓存路径，用 `SOLOGBS_0917_GSB_HISTORY_CACHE_TTL` 调整默认新鲜度。提交预检会强制在线刷新；在线失败时可回退缓存，但 `historyFresh=false` 会作为提交阻断项。

## 数据提取

只读接口：

1. 列表：`/api/v1/gsb/submissions?...&page=N&page_size=200`
2. 详情：`/api/v1/gsb/submissions/<id>`

列表项只有截断摘要，不能用于最终去重。必须按每条记录的 `id` 调详情接口，同时读取完整 `user_prompt` 与 `gsb_reason`。

历史记录数、可见范围和分页以运行时只读接口为准；本地缓存会保存当前凭据可见的完整记录。若平台反馈了当前凭据无权读取的历史 ID，必须先把该次冲突文案导入本地缓存，不能把“列表查不到”当作去重通过。

## 提示词去重规则

先对文本做 NFKC、去空白、去常见中英文标点并转小写，再比较：

- 规范化后完全相同：`EXACT`，阻断。
- 最长连续公共片段 ≥18 字：`SIMILAR`，阻断。
- 相似度 ≥0.82：`SIMILAR`，阻断。
- 相似度 0.62–0.82：`REVIEW_REQUIRED`，阻断并人工检查。
- 其他：`UNIQUE`，通过。

## GSB 理由去重规则

`GSB 理由` 单独与历史 `gsb_reason` 比较，重点拦截 B-5“公共长片段/套模板”。整句相似度低也必须检查公共片段：

- 完全相同：`EXACT`。
- 公共连续片段 ≥12 字，或相似度 ≥0.40，或 6-gram containment ≥0.45：`SIMILAR`。
- 公共连续片段 ≥8 字，或 6-gram containment ≥0.20，或相似度 ≥0.12 且公共片段 ≥4 字：`REVIEW_REQUIRED`。
- 非空理由只有 `UNIQUE` 允许提交。

命中后将 `rule` 标为 `B-5.*`，并要求依据本次与历史记录两次运行的实际轨迹、commit、测试或启动输出重写。

## 已知但无权读取的历史记录

平台可能只返回历史 ID（例如 `#2090`）而当前凭据无权读取详情。此时必须把平台审核原文、历史 ID 和已提交理由导入本地手动缓存后再去重：

```bash
python3 scripts/prompt_dedup.py \
  --import-history /absolute/known-history.json \
  --reason-file /absolute/task/root/monitor/gsb-draft.json
```

`known-history.json` 支持单条对象、对象数组或 `{"items": [...]}`，字段可用 `id`、`gsb_reason`、`user_prompt`。仅记录 ID、没有历史理由文本时，提交预检必须阻断。

## 手动检查 GSB 理由

```bash
python3 scripts/prompt_dedup.py --task-root TASK_ROOT \
  --reason-file TASK_ROOT/monitor/gsb-draft.json
```

`--reason-file` 也支持纯文本理由文件；也可直接用 `--reason "..."`。结论非 `UNIQUE` 时退出码为 2。

## 输出文件

- `$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json`
  - 跨任务持久缓存
  - 完整 `user_prompt`、`gsb_reason`、提交 ID、状态、时间、任务类型、难度、Repo、A/B SessionID
- `$CODEX_HOME/cache/sologsb-0917/gsb-history-manual.json`
  - 平台反馈或当前凭据无权读取的手工历史记录
  - 与在线缓存合并后参与提示词和 GSB 理由去重
- `monitor/gsb-prompt-history.json`
  - 当前任务过滤后的完整历史文案列表
- `monitor/prompt-dedup-review.json`
  - 候选 Prompt 去重结果
- `monitor/gsb-reason-history.json`
  - 当前任务过滤后的历史 GSB 理由列表
- `monitor/gsb-reason-dedup-review.json`
  - `decision`、`rule`、相似度百分比、最长公共片段、模板 n-gram containment
  - `rewriteInstruction`：要求依据两次实际运行轨迹与产物重写

## 规则

- 不使用列表页截断的 `prompt_excerpt` 作为判定依据。
- 当前任务已提交的同一记录必须按 A/B SessionID 自动排除，避免自比自重复。
- 任何提示词重复都必须重新出题；任何理由命中都必须依据实际轨迹与产物重写。
- 改写后必须重新运行 preflight；完整审核通过则重新自动批准，改动量例外则原审批随 payload 哈希变化自动失效。
