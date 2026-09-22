# 0917 实测避坑清单（cy-415）

本清单固化 2026-09-17 `cy-415 / 二手闲置物品交换平台` 完整执行中反复出现的试错。下一次先按这里处理，不重新摸索。

## 平台选取

- 先用 `platform_bridge.py select --dry-run` 查候选和配额，再执行正式 `select`。正式成功即占用配额。
- 平台项目详情接口可能返回 500；不要猜配额。改查 `/projects/mine?page=1&size=200`，按同一个 `projectCode` 读取 `quotas`。
- 正式选取后立刻把 `taskId/taskNo/variant/roundId/quotaBefore/quotaAfter` 写入任务审计文件。

## 模型运行与超时

- 困难题的默认单 attempt 时限已改为 7200 秒。实测 B 的稳定成功运行约 65 分钟；3600 秒会过早重启。
- Claude 的 `api_retry` 可能超过 10 次但仍继续工作。自动重连属于同一 attempt，不能当作新尝试，也不能仅按重试次数提前杀会话。
- 单 Key 默认先跑 2 个固定目录候选；最先完成的两个映射为 A/B，候选目录不改名。多个任务共享全局容器名额，不得每个任务各跑两个候选。
- `run --side A|B --force` 重跑的是已映射候选，会重置该候选本轮调用内的 attempt 编号；跨多次手动 `force` 必须人工记录全局新增次数。
- 一个候选 staged 后，其他候选失败或终止不会作废该结果；已进入前两名且审核中的候选不要无故重跑。
- 每个候选最多六次实际尝试（含首次）；自动重连仍属于同一 attempt。
- 只在出现最终 API/网络失败、无 `end_turn`、权限询问、追问或超时后重启；不要在仍有事件增长时抢跑。

## pnpm 与 fresh clone

- pnpm 11 的 fresh clone 常见报错：`ERR_PNPM_IGNORED_BUILDS`。顺序固定为：
  1. 先执行 `CI=true pnpm install --frozen-lockfile`；
  2. 失败后执行 `pnpm approve-builds --all`；
  3. 再执行 `CI=true pnpm install --frozen-lockfile`；
  4. 最后执行 `pnpm build`。
- 第一次失败必须保留在 `monitor/verify-logs`，不能静默改写原命令或删除日志。
- 容器生成的 `node_modules` 切到 macOS 后可能触发无 TTY 的模块目录重建；录制或宿主复核前统一加 `CI=true`。
- fresh clone 的测试脚本若硬编码 `/workspace/node_modules/...`，该失败必须写成 `expectedExit=1 / observedFailure=true`，不能把 `/workspace` 软链后假装脚本可移植。

## Web 录屏干扰清理

- Chrome 首启的“登录 Chrome?”、同步、资料菜单推广会覆盖页面，必须同时禁用 `SigninPromo`、`DiceWebSigninInterception`、`ChromeSignin`、账号一致性和资料菜单相关 feature，并写入 `signin.allowed=false`、`sync.requested=false`。
- Web 启动命令优先显式绑定 `127.0.0.1`。Vite 可用 `npm run dev -- --host 127.0.0.1 --port <端口>`，终端只保留 Local 地址，不展示多网卡 Network 行。
- 开录前抽查终端首帧和页面首帧；发现登录、密码、翻译、通知、下载、恢复气泡或多项网络地址时，不得开始录制。

## Web 录屏

- Web 计划默认按锁文件选择 `pnpm dev`，并从 `dev` 脚本解析端口；不要继续硬编码 `npm run dev` + 5173。
- 录制前预检固定执行依赖准备和 `pnpm build`，正式录屏只做快速启动。
- 场景先确认当前用户、默认 Tab 和种子数据。列表存在“我发起的/我收到的”时，默认 Tab 不包含目标记录就先真实点击切换。
- 多卡片页面不要用 `first()` 盲点。优先按卡片范围定位；同名操作按钮用当前可见卡片的 `.last()` 或带业务文本的容器。
- 等待成功状态时优先使用稳定业务文案，例如“双方均已确认”；状态徽标可能受样式、遮挡或重复节点影响而不可见。
- 首次录屏失败也保留完整片段和 `browser-result.json`，修正 scenario 后重录，不删除首次失败证据。

## 验证与 GSB

- 每个产物至少独立执行：依赖准备、项目测试、生产构建、开发服务探活。
- 项目自带测试失败不等于构建失败；分开记录退出码和日志，不做合并式“全绿”表述。
- `status=complete` 前必须确认 Excel 中录屏路径已刷新为最终成功视频，而不是默认 `demo.mp4`。
- GSB 负面 claim 的 `trigger` 必须原样出现在 240 字以内的理由中；理由句子需逐句映射 evidence ID。
- GSB 理由不要用“10 个后端测试”“12 项 API 断言”这类数量堆砌，写成“后端关键路径完整覆盖”“完整接口流程验证”等业务覆盖表述，避免像审计清单。
- 提交改走 `submission/scripts/submit_api.py` 的 Python API；不带 `--execute` 只作诊断。完整审核通过后的默认流程直接加 `--execute`，自动记录批准用户（取自设备配置 `solo2.approver`）；只有改动量单项失败时才使用例外审批文件。

## 2026-09-20 G5 实测：过程与产物必须同时覆盖

- 真实打回文案只写了旧唯一约束未移除、索引仍含 `idx_run_input_version`、接口少字段和最终需求覆盖情况。
  这些都属于产物层，平台明确要求再补轨迹中的执行过程事实。
- 过程层至少写一个真实动作和定位点：读取或检查了哪个文件、修改了哪些文件、执行了什么命令、
  在哪个阶段发现约束问题、是否走过弯路或返工。不能只写“过程中进行了检查”。
- A、B 两侧都要各自覆盖过程与产物，不能 A 写过程、B 写产物，也不能整段只写两次交付物毛病。
- 草稿用 `claim.text` 作为理由锚点：每侧至少一条 process claim 和一条 artifact claim，两类 `text`
  都要原样进入 GSB 理由；`validate_draft` 和提交预检会按此阻断。

## 2026-09-18 补充实测

- Otty 用户切换场景：原生 `<select>` 的 option 会命中隐藏节点；等待可见的 `.user-brief__name strong`，不要等待 option。
- 录屏结果判定：即使 ffmpeg 生成了视频，非预期浏览器退出码也必须让 `recordings.ok=false`，`record` 返回非零并阻止进入 `recorded/complete`。
- 候选映射后若发现 B 语义门禁失败：不要 force push 旧仓库；重跑映射侧，归档废弃仓库，重新创建带新唯一后缀的干净仓库再发布。
- 发布前先验证并发路径的真实 API/页面行为；仅“同意”与顺序确认通过，不能证明双方同时确认会正确收口。

## 2026-09-19 Manager 地址更新

- Solo Manager 地址发生过一次迁移，具体地址不写在技能包里，统一由设备配置
  `manager.baseUrl` 提供。
- 根页面探活返回 HTTP 200、API 无令牌访问返回 HTTP 403 属于预期。
- 新任务应使用设备配置里的地址；历史任务审计文件中的旧地址保留原值，不做批量回写。


## 2026-09-19 G11 实测

- `cy-406` 的 A/B 业务源码实际改动足够，但产物 commit 同时包含 `node_modules` 和 `dist`，
  平台统计到 `A 改 0 行、B 改 0 行`，最终以 `G11` 废弃提交 `#1952`。
- 固定处理顺序：发布阶段先在 `.git/info/exclude` 排除依赖目录、构建产物、缓存和锁文件；stage 后
  复算非测试业务代码改动并写入 `lineGate`。任一侧少于 10 行时仍创建 A/B 快照并 push，最终阻断延后到提交预检。
- 提交预检必须从远端 `main` 与 A/B commit 复算业务改动量，并阻断脏仓库；只有低改动量是唯一阻断项时，
  才允许设备配置里的审批人批准 `change-volume-line-gate` 例外。设计提示词时建议每个
  A/B 至少 30 行、跨 3 个业务文件，避免贴着平台 10 行阈值。
- 详细规则见 `references/change-volume-gate.md`。

## 2026-09-20 GitHub 网络实测：Loon 代理

- GitHub CLI 与远端 Git 命令统一从 `SOLOSB_GITHUB_PROXY` 读取代理；未设置时自动探测本机 Loon HTTP `127.0.0.1:17890`，再探测 SOCKS5 `127.0.0.1:17891`。
- 实测 `gh api user`、`git ls-remote`、`git clone/fetch` 经 `127.0.0.1:17890` 或 `127.0.0.1:17891` 均成功；未显式注入时曾出现 `SSL connection timeout` 与空仓库清理 `TLS handshake timeout`。
- `github-init`、A/B 原子发布、远端验证 clone 和提交预检必须复用同一代理环境。代理故障是一次网络重试范围，不得计入 Claude 候选尝试次数，也不得在仓库清理失败后继续批量创建仓库。


## 2026-09-20 录屏窗口隔离实测

- 全屏录屏后按窗口 bounds 裁切无法识别像素归属，其他窗口只要覆盖目标矩形就会进入视频；`bringToFront` 只能降低概率。
- 固定方案为当前 Space 直接按 `kCGWindowNumber` 采集：`screencapture -x -v -l<windowId> <输出.mov>`，再统一转码为 1280x720。
- 不自动切换 Space；录制直接在当前 Space 对目标窗口 ID 采集。
- `screencapture` 启动后过早发送停止信号可能得到空文件；停止器至少保留 4 秒采集时间，正常录屏远长于该窗口。
- 旧版曾要求最小化并恢复 ChatGPT 窗口；该规则已于 2026-09-21 废除。录制器不得触碰 ChatGPT，
  窗口采集与前台采样只围绕 Otty/Google Chrome 执行。
- Otty CLI 偶发“IPC response timed out”后仍可能延迟创建窗口；打开失败时按标题继续轮询十秒并关闭迟到窗口，避免残留 Orphan Window。
- 2026-09-20 实做遮挡测试：底层白色 Chrome 窗口被上层红色 Chrome 窗口覆盖后，按底层 `windowId` 的视频和截图重叠区仍为白色；确认窗口 ID 采集不会录入上层应用像素。

## 2026-09-20 录屏窗口 ID 红线

- 取消倒计时遮罩及其 `*-countdown.json` 门禁；录制开始前不再运行 `countdown_overlay.py`。
- 所有必须录屏的任务只允许 `captureKind=window-id`。Web 题必须先定位 Otty 和独立 Chrome 的具体窗口 ID；
  窗口不存在就打开，仍无法定位或 `windowId<=0` 时立即停止，禁止整屏、裁切、iTerm2 或 headless 回退。
- GitHub 网络访问统一走 Loon：显式 `SOLOSB_GITHUB_PROXY`，否则探测 HTTP `127.0.0.1:17890`，再探测 SOCKS5 `127.0.0.1:17891`；不可用即按基础设施门禁停止。

## 2026-09-21 窗口级后台录制加固

- `kCGWindowBounds` 在 PyObjC 下不能按普通 `dict` 判断；必须用 `collections.abc.Mapping` 接收并
  `dict()` 归一化，否则窗口在录制开始时会因 bounds 假无效而被错误阻断。
- 开录前只定位一次窗口不够。真正调用 `screencapture` 前必须重新读取同一个数字 `CGWindowID`，复核
  当前 Space、未最小化状态和 `ownerPid + ownerName`；任何一项变化都停机，禁止退回整屏或裁切。
- 录制窗口不得靠 `window focus` 或定时 `page.bringToFront()` 保持前台。开窗短暂抢到前台时，只在最前普通窗口
  属于本次录制进程的条件下恢复用户原应用；用户切到其他应用后不得抢回。
- 2026-09-22 起统一使用 `pointerStrategy=none`：录制器不得移动、停靠、恢复指针，也不得查询鼠标按键或调用
  任何会改变鼠标位置的系统接口。旧的 `background`、`park-pointer` 计划自动归一到 `none`。
- 每个片段保留 `<片段>-cursor-guard.json`，记录 `pointerStrategy=none`、`pointerPolicy=host-input-untouched`、
  `hostInputRespected=true`、`pointerMoved=false`、`mouseButtonsQueried=false`、`parkApplied=false` 等字段；指针是否在窗口内不影响门禁。
- Web 模式不能只依赖 tee 生成终端日志。收尾必须调用 `otty pane capture --pane <id> --lines 400` 写入真实
  `terminal.log`，否则 `recorded/complete` 状态不成立。
- 收尾必须执行 `cleanupCommands`，再只终止本次录制新起的应用端口监听 PID；报告中的
  `residualAppPortListeners` 必须为空。临时 `chrome-profile` 默认删除，保留排障时显式设置环境变量。
- 实测最小化窗口后，窗口会掉出 on-screen 列表并只能采到黑帧，因此禁止“先最小化再录”。前台策略只能后台采集和焦点恢复。
