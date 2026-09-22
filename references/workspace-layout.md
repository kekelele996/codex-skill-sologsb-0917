# 工作目录与 Git

```text
<TASK_ROOT>/
├── source/
│   ├── origin/
│   ├── candidates/
│   │   ├── candidate-1/
│   │   ├── candidate-2/
│   │   └── candidate-N/
│   ├── a/  # 仅旧任务兼容；新任务不使用
│   └── b/  # 仅旧任务兼容；新任务不使用
├── monitor/
│   ├── source.json
│   ├── platform-selection.json
│   ├── platform-claim.json
│   ├── state.json
│   ├── evidence.json
│   ├── audit.json
│   ├── gsb-draft.json   # gsb 命令写出的提交预检用草稿（含 claims / sentenceEvidence）
│   ├── change-volume-line-gate.json  # publish 写入的 A/B 业务代码行数门禁记录
│   ├── semantic/
│   │   ├── a.packet.json
│   │   ├── a.review.json
│   │   ├── b.packet.json
│   │   └── b.review.json
│   ├── recording/{a,b}/
│   │   ├── frontmost-window-monitor.json
│   │   ├── service-cleanup.json
│   │   └── {web-otty,terminal-otty}/*-{window-capture,cursor-guard}.json
│   ├── runtime/candidates/candidate-N/
│   └── runtime/{a,b}/
└── workspace/
    ├── 评审文件/
    │   ├── 提示词.md
    │   ├── 提示词.sha256
    │   ├── 审核结论-a.md
    │   ├── 审核结论-b.md
    │   ├── GSB提交字段说明.md
    │   ├── 交付表.xlsx
    │   └── pre-submit/
    │       ├── submission-payload.json
    │       ├── change-volume-review.json
    │       ├── submission-approval.json  # 完整审核通过时自动写入，approvedBy=liudong
    │       └── change-volume-line-gate-approval.json  # 仅例外审批时生成
    ├── 轨迹文件/candidates/candidate-N/
    ├── 轨迹文件/{a,b}/
    └── 视频信息/{a,b}/{脚本,视频}/
```

- 任务状态以 `monitor/state.json` 为唯一事实源，所有写入采用临时文件加原子替换。
- 主要状态：`prepared → prompt_ready → candidates_running → semantic_review_required`
  `→ repo_ready → ab_clean → verified → gsb_ready → recorded → complete`。
- 候选独立记录 `running`、`attempt_invalid`、`cancelled`、`staged`、`blocked`；A/B 只记录
  映射后的逻辑侧状态。单 Key 默认 N=2 个候选同时运行，最先 staged 的两个按完成顺序映射 A/B，
  其余候选立即停止。
- `candidateMapping.A/B` 保存 `candidateId`、`candidateFolder`、`workspacePath`、`completionOrder`。
  候选文件夹绝不因映射而改名；后续审核、提交和验证都必须按该映射读取原工作区。
- Claude Code 自动重连默认最多十次，仍属于同一 attempt；每个候选实际失败后的全新启动最多六次
  （含首次）。跨多次 `--force` 调用必须人工累计，不能把每次 attempt-01 当成首次。
- staged 表示结构校验通过但尚未生成产物 commit；只有两侧结构和语义审核都通过后，
  `publish` 才分别在本地提交，并原子推送 A/B。低改动量不再阻止生成快照。
- `publish` 为每侧写入 `lineGate`，并把汇总写到 `monitor/change-volume-line-gate.json`；
  少于 10 行的最终阻断发生在提交预检。
- GitHub 上传必须晚于候选竞速和前两名映射；运行器在候选完成前不得创建仓库或 push。
- `github-init` 以 `source/origin` 的初始快照创建公开仓库；仓库名必须是 `<projectCode>-<3–6位小写字母数字>`，例如 `cy-291-a1b2`。
- 远端只允许 `main`、`A`、`B`。初始提交在 `main`，A/B 初始都指向初始 SHA。
- A/B 产物 commit 分别来自映射候选目录，父提交必须等于初始 SHA，禁止 force push。
- 平台项目选择锁和项目占用锁位于
  `$CODEX_HOME/solo2-auto/locks/platform-claims/`，与 `solo2-auto` 共用。
  项目锁元数据同时写入 `monitor/platform-claim.json`；`cleanup` 停止持有进程后释放，
  默认 TTL 24 小时。不能把锁复制到任务内规避互斥。
- 录屏锁位于 `$CODEX_HOME/run/sologsb-0917/recording.lock`，不属于任何单一任务目录；
  所有并行任务共享该锁，按项目串行录制，不能把锁复制到任务内规避互斥。
- 每个片段必须保留 `<片段>-window-capture.json` 和 `<片段>-cursor-guard.json`；录制侧还必须保留
  `frontmost-window-monitor.json` 和 `service-cleanup.json`。这些报告与最终 mp4 共同构成
  `recordings.ok` 门禁，缺失或状态异常不得进入 `recorded`。录制器不读取或操作 ChatGPT 窗口。
- 每道题一个仓库；本地源和审核文件不得写进模型执行工作区以外的共享路径。
