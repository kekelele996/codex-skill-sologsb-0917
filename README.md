# sologsb-0917

全局版本号 `1.0.0`（唯一来源：根目录 `VERSION`，可用 `python3 scripts/sologsb.py --version` 读取）。

0917 期 Pair-wise GSB 的可复用 Codex Skill。详见 `SKILL.md`。

默认流程：

1. 困难题默认单 attempt 超时为 7200 秒，避免 3600 秒过早重启；
2. 先建立本地初始快照，再拉取 N 份隔离源码（单 Key 默认 2），目录固定为
   `source/candidates/candidate-1..N`；候选目录永不改名。
3. `run --side both --candidates 2` 让 N 个候选在独立容器中并行无头执行；Base URL 取设备配置的 `claude.baseUrl`。
   每次启动容器都在主机级独占锁内预占名额，运行中容器与预占位合计不得超过设备配置
   `claude.maxContainers`（默认 4，绝对上限 6）；该值每个任务动态读取，排队时每 180 秒重读，
   超限时排队等待释放。
   前两个干净完成者按完成顺序映射为逻辑 A/B，其余候选立即停止。
4. 每个候选最多六次实际尝试（含首次）。模型异常后从本地初始快照重新 clone，
   并创建新容器、新 Claude home、新 SessionID 从提示词重开；自动重连不算一次新尝试。
5. 候选映射完成后才运行 `github-init`，以原始源码创建 `main/A/B`，再开始 A/B 语义审核。
6. 只有两侧语义审核全部通过，Skill 才从映射候选目录生成本地产物 commit，并原子推送 A/B；依赖目录、构建产物、缓存和锁文件不进入 commit。
7. 发布阶段生成 A/B 产物快照并按 G11 记录非测试业务代码改动量；任一侧少于 10 行写入 `lineGate=failed`，最终提交预检再阻断。建议至少 30 行并跨 3 个业务文件。
8. 再执行真实产物验证、GSB、Excel 和录屏；GSB 理由必须分别写出 A/B 各自的过程事实与产物结果，过程 claim 要包含实际动作和定位点，产物 claim 要包含可观察结果；视频名为 `<项目编号-项目名>-验证A/B产物.mp4`。
9. 平台选项目复用 `solo2-auto` 的共享项目锁：全局选择串行化，选中后立即占用项目；
   已占用或运行中的项目自动跳过，显式指定时拒绝，`cleanup` 完成后释放。
10. 所有任务共用一个全局录屏锁，并以项目为持有单位；后到项目等待，避免 Otty/Chrome 与系统录屏并发争抢。
11. 录屏只允许窗口级后台采集：Web 题分别定位 Otty 与 Chrome 的数字 `CGWindowID`，纯终端题定位 Otty；使用 ScreenCaptureKit 独立窗口滤镜和 `SCRecordingOutput`，并强制 `showsCursor=false`。开录瞬间复核当前 Space、最小化状态和 `ownerPid + ownerName`，找不到立即停止。
12. 录制全程不激活、不置前录制窗口；统一使用 `pointerStrategy=none`，不移动、不停靠、不恢复鼠标，也不查询鼠标按键。ScreenCaptureKit 不采集鼠标图层，用户可继续操作鼠标。Chrome 驱动关闭定时抢前台，并记录可见成功的焦点恢复。
13. Web 收尾抓取真实 Otty pane 文本，执行 `cleanupCommands` 并清理本次新起的应用端口监听进程；每秒采样最前窗口，录制窗口置前采样必须为 0。
14. 监控台会展示 `state.candidates` 与 `runtime/candidates/candidate-N`，竞速期间不必等 A/B 命名后才可见。

15. 提交前强制刷新历史 GSB 文案并写入 `$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json`；`user_prompt` 与 `gsb_reason` 分别去重，理由命中 B-5 公共片段或模板 n-gram 时直接阻断。
16. 技能内置 Python API 提交模块；不带 `--execute` 只作诊断。完整审核通过后的默认流程是直接运行 `submit --execute`，系统自动记录批准用户（取自设备配置 `solo2.approver`）。只有改动量少于 10 行且为唯一阻断项时，才等待设备配置里的审批人批准 `change-volume-line-gate` 例外。不使用浏览器模拟点击。

## 最终交付展示硬约束

任务完成及 SOLO2 提交后的最终回复，必须严格使用
`references/final-delivery-format.md`。固定八段顺序、标题文字、绝对路径和视频内嵌均不得改动；
不得增加前言、结束语、过程日志或模板外章节。未提交写“未执行（仅本地交付）”，无未解决问题写“无”。

## 多设备同步

技能包本身不含任何凭据，可以整份覆盖。每台设备的专属凭据放在技能目录**之外**的
`~/.codex/sologsb/config.json`（权限 0600），覆盖技能不会影响它。

### 新设备接入

```bash
git clone <本仓库地址> ~/.codex/skills/sologsb-0917
python3 ~/.codex/skills/sologsb-0917/scripts/configure.py wizard   # 配置该设备凭据并联网验证
```

### 日常更新

```bash
cd ~/.codex/skills/sologsb-0917 && git pull --ff-only
```

技能目录是零写入的：运行期产物一律写到 `$CODEX_HOME/cache/sologsb-0917/`，
因此 `git pull` 不会和本机运行状态冲突，也不需要维护排除清单。

### 改了技能之后

```bash
cd ~/.codex/skills/sologsb-0917
git add -A && git commit -m "说明改了什么" && git push
```

其它设备 `git pull --ff-only` 即可拿到同一份。
