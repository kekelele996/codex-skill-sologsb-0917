---
name: sologsb-0917
description: >
  运行 0917 期 Pair-wise GSB：同一道困难或地狱题使用完全相同的提示词并行跑 N 个
  独立首轮候选（单 Key 默认 2 个），前两个干净完成者逻辑映射为 A/B，随后才上传源码并
  初始化 GitHub 的 main/A/B 三个分支，
  在两侧都通过结构校验和语义完成审核后原子发布产物，再校验真实产物，
  生成证据约束的 GSB 结论、官方当前 schema Excel、字段填写说明和两段真实运行录屏。
  用于“sologsb-0917”“Pair-wise GSB”“A/B 两次跑”“GSB 0917”等任务；技能内置审核与
  Python HTTP API 提交模块。完整审核通过后的默认流程是直接执行 submit --execute；
  自动批准用户记录为设备配置里的审批人。只有任一侧业务代码少于 10 行且这是唯一阻断项时，才等待
  设备配置里的审批人批准 change-volume-line-gate 例外。不使用浏览器模拟点击。最终交付展示严格使用
  固定八段式模板，不得增删标题或附加说明。
---

# sologsb-0917 Pair-wise GSB

本 Skill 把一道题做成一条可审计的 Pair-wise GSB 数据。所有事实必须来自原生轨迹、
Git commit 和真实复核命令；不得用模型最终回复代替证据。

## Git 版本信息

- 仓库：https://github.com/kekelele996/codex-skill-sologsb-0917
- 跟踪分支：`main`
- 全局版本号：`1.1.0`（语义化版本，整个技能统一只用这一个版本号）
- 发布标签：`v1.1.0`
- 精确提交号：运行 `git rev-parse v1.1.0` 获取。
- 机器可读版本：技能根目录的 `VERSION` 文件，是全局版本号的唯一来源；
  命令行用 `python3 scripts/sologsb.py --version` 或 `python3 scripts/sologsb.py version` 读取。
- 改版本时只改 `VERSION` 的 `version` 与 `release_tag` 两行，再同步本节文字，
  CLI、`version` 子命令和自测都会跟着变，不要再在其它文件里散写版本号。

## 实测固定顺序

- 困难题默认单 attempt 超时使用 7200 秒；先区分“仍在推进”与“已失败”，不要因自动重连次数频繁重启。
- 单 Key 并发安全：`run` 默认全局最多同时 4 个 Claude 容器，按“两个任务、每个任务两个候选”共享名额。每次 `docker run` 前都必须调用 `_CONTAINER_LIMITER.acquire`；它在同一个主机级独占锁内完成“统计运行中容器 + 统计存活预占位 + 写入预占位”。运行中容器与预占位合计达到上限时，当前候选不启动并等待已有名额释放。数据库、验证 clone、监控台辅助容器等非任务容器一律不占名额。
- 上限不是写死的 4：若 `~/.codex/sologsb-0917/container-limit.json` 带 `managedBy`（调度监控台托管），其 `maxContainers` 最优先；否则动态读取设备配置 `~/.codex/sologsb/config.json` 的 `claude.maxContainers`，环境变量 `SOLOSB_MAX_CONTAINERS` 和兼容配置 `container-limit.json.maxContainers` 只作回退，默认 4，并受绝对硬顶 6 约束。每个任务启动容器前都会重新读取该文件；排队等待期间每 180 秒重新读取一次，配置调高或调低后最多 3 分钟生效。实际生效值用 `python3 scripts/side_runner.py` 内部的 `_CONTAINER_LIMITER.status()` 读取，它返回 `limit / runningContainers / reservedSlots / advisoryReservedSlots / used / available / runningNames`；其中 `used = runningContainers + reservedSlots`，`available = limit - used`。
- 调度模式由监控台 `automation.scheduleMode` 决定，执行器按提示词里的 `{{schedule_mode}}` 取值行动：
  - `容器优先`：保持运行中的候选容器数等于设定值，任务数可以少于上限；
  - `任务数量优先`：保持并行任务数等于设定值，容器数可以少于上限。
  两种模式共用同一条进门规则：**运行中容器与预占位合计 `>=` 上限**时才等待；有容量时继续逐个放行。
- Anthropic 兼容中转站地址取自设备配置 `claude.baseUrl`，可在 `run` 中传 `--base-url`，或通过 `SOLOSB_ANTHROPIC_BASE_URL` 覆盖；运行器会校验容器实际 Base URL。
- 网关 429 `max_parallel_requests` 属于准入失败：正式候选运行前先等待最小 `/v1/messages` 探测成功；发生最终 429 后不要立即重启新容器，先等待 Key 恢复。恢复后仍按红线使用新容器、新 Claude home 和新 SessionID，实际尝试次数照记。
- pnpm fresh clone 固定按“安装失败留证 → `pnpm approve-builds --all` → 再次安装 → 构建”顺序处理。
- Web 录屏先确认默认 Tab、当前用户和重复卡片选择器；同名操作按钮使用卡片范围或 `.last()`；同时清除 Chrome 登录/同步/密码/通知等浮层与终端多网卡干扰行。
- 录屏必须使用窗口级后台模式：先用 Quartz 定位 Otty/Chrome 的数字 `CGWindowID`，再调用 ScreenCaptureKit 的 `SCContentFilter(desktopIndependentWindow:)` 和 `SCRecordingOutput` 采集；必须设置 `showsCursor=false`、`showMouseClicks=false`、`capturesAudio=false`。全程不激活、不置前、不最小化录制窗口。开录瞬间必须复核窗口仍在当前 Space 且 `ownerPid + ownerName` 未变化，找不到就停机。
- 具体踩坑记录见 `references/lessons-learned-20260917.md`。

## 固定红线

- 所有候选必须使用同一个 UTF-8 提示词文件，发送字节逐字一致。
- 只允许 `困难`、`地狱`；任务类型不得选择 `代码理解`。
- 提示词不得设计成低改动量任务。困难、地狱题建议每个 A/B 至少产生 30 行非测试业务代码改动并跨至少 3 个业务文件。发布阶段仍生成 A/B 产物快照并记录行数门禁失败，平台提交前再执行最终阻断。
- 发布产物前必须排除 `node_modules`、构建目录、缓存、虚拟环境和锁文件；提交预检必须从远端 commit 复算改动量并按平台 G11 口径阻断低改动或脏仓库。只有“任一侧业务代码少于 10 行”且这是唯一阻断项时，才允许 `change-volume-line-gate` 例外审批。
- 例外审批必须绑定 payload 哈希、交付表哈希和改动量复核哈希，批准用户取自设备配置 `solo2.approver`；其他任何门禁失败都不能绕过。
- 默认先拉取同一初始快照的 2 份独立候选源码，不使用 A/B 目录名，也不改名。
  固定目录为 `source/candidates/candidate-1..N`，每份源码各自使用独立容器、Claude home、
  SessionID 和轨迹；默认用 `run --side both --candidates 2` 并行无头执行，需要观察时加 `--live`。
- 每个候选只发送一次提示词。只要出现追问、权限询问、重复真人输入、最终 API/网络失败、
  无最终 `end_turn` 或异常退出，就必须销毁该候选工作区和容器，从本地初始快照重新 clone，
  再用新容器、新 Claude home、新 SessionID 从提示词重新开始；每个候选最多六次实际尝试（含首次）。
  Claude Code 自动重连不算一次新尝试，也不能替代上述重启；默认允许自动重连十次。
  已通过结构校验的候选结果保持 staged，不因其他候选失败或终止而作废。
- 前两个通过结构校验的候选按完成顺序映射为代号 A、B；文件夹和候选编号永不改名，
  映射写入 `monitor/state.json.candidateMapping`。一旦前两名产生，立即主动停止其余候选，
  不等待它们完成。
- 候选阶段绝不创建 GitHub 仓库、绝不 push。重复启动同一任务根时，`run_candidates` 会先只读探测候选任务锁，占用就直接退出，不再清空状态或重建正在使用的工作区；`_clone_candidate` 也不会删除仍被运行中容器挂载的目录。
- GitHub 仓库名必须以平台项目标识开头，再跟 3–6 位小写字母数字唯一后缀，例如 `cy-291-a1b2`；平台项目拿不到 `projectCode` 时禁止创建仓库。
- GitHub 网络红线：所有 GitHub 网络访问，包括 `gh api`、`gh repo view/create/delete`、`git clone/fetch/push/ls-remote`，必须经 Loon 代理。优先使用显式 `SOLOSB_GITHUB_PROXY`，否则自动探测 HTTP `127.0.0.1:17890`，再探测 SOCKS5 `127.0.0.1:17891`；两者不可用时停止作业，禁止裸网直连。
- Loon 端口都不可达或经代理仍出现 TLS/SSL 故障时，保留真实错误并按基础设施门禁停止，不得反复创建候选仓库。
- A/B 映射完成后才允许 `github-init` 以原始源码创建 `main/A/B`；A/B 产物先停在 staged，必须两侧都通过语义完成审核，
  才允许通过原子 push 同时发布 A、B。
- A、B 产物 commit 的父提交必须严格等于初始环境快照；A/B 只是候选映射后的逻辑代号。
- 所有过程与产物结论必须绑定 `monitor/evidence.json`。文中的文件、报错和缺失判断
  找不到轨迹或命令证据时，停止生成 GSB。
- GSB 不是只接收 100% 成功的任务。A/B 任一侧即使构建、启动或关键业务路径失败，失败本身也是有效评估结果；必须继续保留真实启动、报错和关键失败画面，不能因为失败而跳过该侧录屏。
- 负面评价硬门禁：每条 `polarity=negative` 的判断都必须在草稿 claim 中提供
  `triggerKind`、`trigger` 和 `objectiveConsequence`，并让触发节点与客观后果都原样出现在
  GSB 理由中。触发节点只能落到具体步骤/轮次、文件/路径、命令/工具调用或对应需求/操作；
  客观后果必须写出修改未落地、构建/服务失败、接口拒绝、需求未实现等可见结果，不能只写
  “随后通过”。负面理由还必须有文件名、函数名、命令、报错或退出码等可核对证据；
  “过程中”“操作时”“某一步”“更细”“持平”这类泛化或主观表述不能单独支撑判断。
  五维初评和 `审核结论-*.md` 同样执行该规则。
- 视频、轨迹、产物验证和 GSB 文案必须描述同一事实。通过画面只能支持成功判断，失败画面只能支持失败判断；
  若视频显示不可启动、页面崩溃或需求未完成，结论不得写成成功，必须修正评分与 GSB 后再交付。
- 录屏 `ok` 不能只看文件是否生成：`expectedAppFailure=false` 而浏览器/API 非零退出时必须写 `ok=false`、`observedAppFailure=true`，
  `record` 命令返回非零并把状态退回 `gsb_ready`；先归档失败片段，修正 scenario 或应用后重录，未通过前不得进入 `recorded/complete`。
- 录屏窗口 ID 红线：任何必须录屏的步骤只能使用 `window-id`。Web 题必须分别打开或定位 Otty 与 Chrome 窗口，并记录各自 `windowId`；API/CLI/失败题必须定位 Otty 窗口 ID。窗口缺失时先打开，无法定位或 `windowId<=0` 时立即停止，禁止回退到整屏、裁切、iTerm2 或 headless。
- 每个片段必须写 `<片段>-window-capture.json`，其中必须包含 `captureKind=window-id`、`captureBackend=screen-capture-kit`、`showsCursor=false`、`cursorCaptured=false`、目标 `windowId`、所属 PID、bounds、退出码和采集状态；窗口 ID 缺失、失效、后端不是 ScreenCaptureKit、鼠标排除标记不为 false 或采集状态非 `ok` 时该侧录屏失败。
- 开录瞬间必须再次确认目标窗口仍在当前 Space、未被最小化，且 `ownerPid + ownerName` 与定位时一致；窗口不在 Otty/Google Chrome 白名单内时立即停机。
- ChatGPT 不属于录制目标。录制器不得最小化、激活、移动或恢复任何 ChatGPT 窗口，也不生成
  `chatgpt-window-guard.json`；窗口级后台采集只处理 Otty/Google Chrome。
- 开窗可能短暂把录制窗口置前；仅当最前普通窗口属于本次录制进程时，才把用户原前台应用恢复，并写入
  `recordingMetadata.userFrontmostAppAtStart` 与 `recordingMetadata.focusRestores`。不得调用 `window focus` 或定时 `bringToFront`。
  发生焦点抢占时必须确认用户原应用重新成为最前应用并写 `focusRestoreOk=true`。
- 指针策略固定使用 `pointerStrategy=none`（`pointerPolicy=host-input-untouched`）。录制器不得移动、停靠、恢复或读取鼠标按键，不得调用任何会改变用户鼠标位置的接口；旧计划中的 `background`、`park-pointer` 必须自动归一到 `none`。
- 每个片段旁仍写纯 JSON `<片段>-cursor-guard.json`，至少包含 `segment`、数字 `windowId`、
  `captureKind=window-id`、`pointerStrategy`、`pointerPolicy`、`hostInputRespected`、`pointerMoved`、`mouseButtonsQueried`、
  `parkApplied`、`positionReadOnly`、`finalPointerInsideWindow` 和 `status`。
  验收要求 `status=ok`、`pointerStrategy=none`、`pointerPolicy=host-input-untouched`、`hostInputRespected=true`、`pointerMoved=false`、`mouseButtonsQueried=false`、`parkApplied=false`；指针是否位于窗口内不影响录制，画面出现指针也不得要求重录。
- Web 题在 Chrome 驱动中设置 `HUMAN_BROWSER_KEEP_FRONT=0`，并给 Chrome 加
  `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling`。
- Web 录制收尾必须用 `otty pane capture --pane <id> --lines 400` 写真实 `terminal.log`，不能用空文件占位。
- 录制期间每秒采样最前普通窗口，`recordingWindowFrontmostSamples` 必须为 0；采样报告写
  `frontmost-window-monitor.json`。
- 收尾必须执行 `cleanupCommands`，且只终止本次录制新起的应用端口监听进程；残留写
  `service-cleanup.json.residualAppPortListeners`，非空时 `ok=false`。临时 `chrome-profile` 默认删除。
- 录屏默认使用 Otty CLI，统一输出 1280x720（720p）。最终画面只允许出现
  Otty 和浏览器：Web 题两者必须都有，API/CLI/失败题只允许 Otty。默认值必须为 Otty；Web 题按窗口 ID
  录制仅支持 Otty，禁止出现桌面应用、IDE、Finder、系统设置、Dock 或其他应用。无法启动时也必须保留真实失败过程。
- 纯后端/API 题必须使用 `mode=terminal`（仅 Otty），并在 `record-plan.json.apiRequests` 中配置真实请求
  （方法、完整 URL、请求头、请求体、期望状态码、关键响应字段）；录屏必须展示真实请求与响应，
  不得只录启动日志或用 headless 脚本代替。
- 平台项目并发安全：`init --from-platform` 必须先获取按 Manager 地址隔离的全局选择锁，
  串行执行“查项目、确定项目、创建项目占用锁”。项目占用锁复用
  `solo2-auto` 的 `$CODEX_HOME/solo2-auto/locks/platform-claims/` 根目录，
  因此 `sologsb-0917` 与 `solo2-auto` 会互相跳过对方已占用的项目。自动选择会跳过项目锁
  已持有、容器正在运行或本地状态仍在执行的项目；显式指定 `--project-code` / `--project-id`
  命中这些项目时直接拒绝，不得绕过。项目锁由独立持有进程维持到 `cleanup`，默认 TTL 24 小时，
  进程崩溃时最长 24 小时后自动释放；`cleanup` 必须在容器清理完成后释放本项目锁。
- 录制阶段必须获取主机级全局录屏锁，锁文件固定为
  `$CODEX_HOME/run/sologsb-0917/recording.lock`。锁以项目为持有单位，同一时刻只允许一个项目
  执行预检、环境检查、启动、录屏和收尾；后到任务默认最多等待 7200 秒，超时即失败，禁止绕过锁或并发录制。
  进程退出或崩溃时由内核自动释放，不因残留锁文件阻断后续任务。
- 运行器开放 `TodoWrite` 供容器内 Claude Code 记录执行待办；TodoWrite 只用于进度可视化，
  不能替代轨迹校验、语义完成审核或产物证据。
- 2026-09-23 官方表单（fingerprint `954e9db2d25afeb4`，23 个字段全部必填）删除了“备注”，新增 `A/B-交付完整性`（1~5 整数）和 `A/B-交付完整性描述`。草稿必须提供 `delivery.A/B.score/description/evidenceIds`，打分与写法见 `references/delivery-scoring.md`：只写完整性，两侧独立撰写，允许与 GSB 理由少量重合但不得照抄，A、B 两段之间和与历史数据之间都按 G12 查重。
- 红线：A/B 交付完整性描述必须与本侧轨迹对应，不得出现对立意见。锚点必须在本侧原始轨迹中存在，分数和描述不得与本侧真实复核结果、引用证据、GSB 理由或 GSB 结论相反（详见 `references/delivery-scoring.md` 红线一）。本地（容器外）编写的任何测试和自动化脚本，包括验收、冒烟、Playwright、录制场景，都不参与交付完整性描述：不写进描述，不引用其证据，也不作为分数依据（红线二）。有页面的项目和之前一样引用录屏证据；纯后端 API 项目的录屏排除，改用验证计划的 `probe` 接口探活作为证据。描述和理由都直接写“请求了登录接口，返回404”这种主观直述，不写“从录屏来看”“根据编写的测试”。
- GSB 理由与题目提示词都要写得像人话：理由按“一侧一段话”组织，相邻句不用同一称谓起头、每侧称谓最多 3 次、结尾前交代判准；提示词像业务方交代需求，硬性措辞最多 3 处、分号最多 2 个，不用“刷新后……一致”式模板收尾。
- 提交前必须逐份读取 A/B 轨迹 JSONL，确认内容实际包含 SessionID，且与状态、Excel 中的对应 SessionID 完全一致；缺失或不一致直接阻断。
- 提交前必须从只读接口刷新历史 GSB 记录，将完整 `user_prompt` 与 `gsb_reason` 写入本地持久缓存 `$CODEX_HOME/cache/sologsb-0917/gsb-history-cache.json`，并按当前 A/B SessionID/已提交 ID 排除自身。历史文案缓存不可只保存在单个任务目录。
- 当前 `GSB 理由` 必须与历史 `gsb_reason` 逐条执行 B-5 公共长片段、模板 n-gram 和相似度检测；低整句相似度但存在公共长片段同样阻断。任何 `EXACT`、`SIMILAR`、`REVIEW_REQUIRED` 或 `MISSING` 都不得上传或提交。
- 平台反馈历史 ID 但当前凭据无权读取详情时，必须把该 ID 和已提交理由导入 `$CODEX_HOME/cache/sologsb-0917/gsb-history-manual.json` 后再去重；只有 ID、没有历史理由文本时禁止提交。
- 命中历史理由后，必须回到本次和对应历史记录的轨迹、commit、构建/测试/启动输出重写对比理由；禁止只替换项目名、保留公共句式或套用统一模板。
- GSB 理由使用完整、质朴的中文描述，不使用省略式单字；统一写“A 侧方案”“B 侧方案”，业务名词写完整。禁止使用“落在……”式收束句式，结论直接写“因此选择 B 侧方案”或“B 侧方案更好”。
- GSB 理由必须严格使用纯文本，不允许任何 Markdown 语法；标题、列表、代码块、行内代码、链接、图片、强调标记、表格、引用和 HTML 标签均由 `gsb_tools.py` 硬阻断。
- GSB 理由禁用“闭环”“根因”“落库”，数据写入统一写“入库”；常用命令“npm run build”统一写“build”，避免触发历史公共长片段。“真实”“真正”“其实”等空泛表达会给出警告。数字两侧不留空格，写“计数从2变22”。文风要朴实但不要过于干练：动词后补足语气与结果（“建了独立表”“覆盖掉了”“还是照旧读回”），禁止电报式短句。
- GSB 理由必须达到高中语文阅读水平，句子通顺；单句非空白字符不得超过 56 字，分句过多、重复标点、标点不成对、连接词堆叠、重复虚词和残句均阻断。
- 禁止用中文念法或缩写代称提交号，例如“依六四二四七二c”；引用原有业务值或代码值即可。
- 负面触发节点不能只写“首次运行阶段”这类泛化阶段，必须落到具体页面、入口、接口、文件、命令或报错原文，例如“打开专栏详情页时”“调用我的订阅接口返回401”。
- 禁止把并发现象压成名词串或读数排列；按“触发动作、现场现象、客观后果”展开成完整短句，让读者能从业务过程读懂胜负原因。
- GSB 理由必须同时覆盖 A、B 各自的过程与产物：每侧至少有一条过程 claim 和一条产物 claim，`claim.text` 原样写入理由。不能只写一侧过程、另一侧产物，也不能整段只写交付物毛病。
- 过程层必须写原生轨迹可核对的执行事实：在哪个步骤、文件、命令或需求触发，读取、检查、修改、执行了什么，是否返工、发现或修正问题；不能只写“进行了测试”“做了迁移”“改过代码”这类空泛动作。
- 产物层必须写最终可观察结果：功能、交互、数据、接口、性能、兼容性、可运行性、需求覆盖或真实失败结果。不得用录屏、视频、截图、浏览器、测试设备、运行环境、验收宿主、Otty、鼠标、分辨率、终端窗口等场外因素评价好坏；这些不能进入 GSB 理由，没有例外。
- 禁止在 GSB 理由中引用 `evaluationExcluded` 的环境或工具噪声证据；这类证据也不能作为独立 claim、评分或胜负依据。
- GSB 理由不堆测试或断言数量，改成“后端关键路径完整覆盖”“完整接口流程验证”等业务覆盖描述；`gsb_tools.py` 会直接阻断计数式写法。
- 低价值环境/工具噪声不直接参与 GSB 评定：解释器或命令未找到、测试 PYTHONPATH 缺失、编辑工具替换文本未匹配、临时工作目录、重跑等，不能作为独立 claim、评分项或胜负依据。标记为 `evaluationExcluded` 的证据不得进入 GSB 理由正文。
- 完整审核通过后必须直接执行 `submit --execute`，不需要人工审批文件，系统自动记录批准用户（取自设备配置 `solo2.approver`）；不带 `--execute` 只作人工诊断。只有改动量单项失败时才停止并等待设备配置里的审批人批准例外；确认必须重提时才使用 `--force`。
- 禁止用浏览器模拟点击、Playwright 填表或文件选择器提交 GSB；统一调用 `submission/scripts/submit_api.py`。
- 最终交付展示硬门禁：`status=complete` 后的最终回复及 SOLO2 推送后的最终回复，必须逐字遵守 `references/final-delivery-format.md`。固定八段顺序为“仓库与初始快照、A / B 会话、提交与轨迹、审核结论、GSB 文案、Excel 与字段说明、两段视频、SOLO2 推送结果、未解决问题”；不得增删二级标题、添加前言/结束语或改写标题。所有本地路径必须为绝对路径，视频必须内嵌，未提交时明确写“未执行（仅本地交付）”，无未解决问题时写“无”。

## 本机配置（每台设备一次）

设备专属凭据不在技能包内，统一放在 `~/.codex/sologsb/config.json`（明文，权限 0600）：

- Claude API Key 与 LLM Base URL
- 最大并发容器 `claude.maxContainers`（默认 4，绝对上限 6）
- Solo Manager 地址、账号、密码
- SOLO2 地址、账号、密码
- GitHub Token 与 Loon 代理

技能入口启动时会自动把它注入环境变量，一般配置优先级为
`命令行参数 > 环境变量 > 配置文件 > 代码默认值`；磁盘上不存在该文件时，
运行器按旧方式回退到 macOS 钥匙串与环境变量。容器上限 `claude.maxContainers` 是例外：
它优先读取上述设备配置，并在每个任务启动容器前以及排队等待期间的每 180 秒重新读取。

新设备接入、更换任一凭据、或 `verify` 失败时，执行向导（会联网验证四项）：

```bash
python3 ~/.codex/skills/sologsb-0917/scripts/configure.py wizard
```

其它子命令：`show` 查看（密钥脱敏）、`set KEY=VALUE` 单项新增或覆盖、
`verify` 仅验证、`path` 打印配置路径。

SOLO2 会话失效时，运行器会用配置里的账号密码自动重新登录：读接口遇到 401 会重登后重试，
提交前会先校验会话，因此不需要人工更换 Cookie。

## 工作流

1. 运行 `init` 接入平台项目、本地 ZIP 或源码目录。平台模式先拿全局选择锁并占用项目，
   再下载源码；选中结果和占用锁写入任务目录，供后续审计和 `cleanup` 释放。
2. 阅读 `source/origin` 和参考规范。**开始设计提示词前**先运行
   `$CODEX_HOME/skills/sologsb-0917/submission/scripts/prompt_dedup.py --task-root ROOT`
   拉取历史 GSB 的完整 `user_prompt` 列表，并排除当前任务自己的历史提交。基于历史列表设计
   唯一提示词后，再运行同一脚本并传 `--candidate`；只有 `UNIQUE` 才能进入
   `prompt --candidate ... --review ...`。提示词必须走固定版本 `ra-人话`。
3. 运行 `run --side both --candidates 2`（只有独立 Key 且容量确认时才显式提高 N）。困难题单 attempt 默认 7200 秒；
   运行器先建立本地初始快照并拉取 N 份隔离源码，再并行启动 N 个容器无头执行，每份分别写
   原生 JSONL。每个候选中断、异常或没有最终 `end_turn` 时，只销毁该候选现场并重新 clone，
   最多六次实际尝试；自动重连不算新尝试。前两个 staged 候选按完成顺序映射 A/B，其余候选
   立即停止。此步骤不创建 GitHub 仓库、不上传源码。
4. 运行 `github-init`。只有 `candidateMapping` 已包含 A/B 时才允许创建公开仓库，并以原始源码
   创建 `main/A/B` 三支；候选目录保持 `source/candidates/candidate-N` 原名，不复制、不改名为 A/B。
5. 运行 `semantic --side A` 与 `semantic --side B`（或 `--side both`），读取映射后审核包并写
   `<side>.review.json`，逐条核对提示词要求、轨迹、最终回答和对应候选 diff；同时按 Solo2
   五维口径完成该侧初评；所有负面判断补上触发节点，只有两侧最终都满足
   `completed=true`、`interrupted=false`、`unfinished=[]` 才能继续。
6. 运行 `publish --semantic-a ... --semantic-b ...`。Skill 从 `candidateMapping` 找到原名候选目录，
   分别生成本地产物 commit，再通过 GitHub 原子 push 同时更新 A、B；任一审核失败或原子 push
   失败时不得发布。
7. 根据项目真实入口填写 `monitor/verification-plan.json`，运行 `audit`。没有
   可执行验证计划时不得进入 GSB。发布前由 `publish` 排除依赖和构建产物并记录少于 10 行的门禁失败，最终提交阶段再阻断。
8. 读取 `monitor/evidence.json` 和 `monitor/audit.json`，生成 `gsb-draft.json`。
   每侧必须分别提供 process/artifact claim，且两类 claim 的 `text` 都要原样进入理由；
   过程 claim 必须包含轨迹可核对的实际动作与文件、命令、步骤或需求定位点，
   产物 claim 必须包含返回、缺少、未实现、失败、写入、生成或通过等可观察结果。
   每条判断引用证据 ID，并把每条负面 claim 的触发节点与客观后果写入
   `triggerKind`/`trigger`/`objectiveConsequence`；同时按 `references/delivery-scoring.md` 填写
   `delivery.A/B`（1~5 整数分、只谈完整性的独立描述、本侧 `evidenceIds`）；运行 `gsb --draft ... --review ...`；
   该命令会同时把草稿写到提交预检固定读取的 `monitor/gsb-draft.json`，录屏后刷新 Excel 也会保持同步。
9. 为 A、B 分别生成录屏计划和 scenario，再运行 `record --side A/B --plan ...`。
   命令会在锁内执行预检/预构建、环境检查、真实录屏和收尾；另一项目持锁时当前任务等待而不是并发启动。
   默认使用 Otty；终端只显示相对路径，不暴露真实绝对路径。录制固定当前 Space，不切换 Space；
   不对 ChatGPT 做任何窗口操作；先定位或打开 Otty/Chrome 窗口并取得数字 `CGWindowID`，开录瞬间复核后只用
   ScreenCaptureKit 按窗口 ID 后台采集，不激活录制窗口。若开窗短暂抢到前台，只把用户原应用恢复；默认
   `pointerStrategy=none`，全程不操作鼠标，并强制 `showsCursor=false`，用户仍可正常操作鼠标。结束后检查前台采样、焦点恢复和服务清理。
   视频统一保存为 `<项目编号-项目名>-验证A产物.mp4` 和 `<项目编号-项目名>-验证B产物.mp4`。
   成功侧录成功链路，失败侧录真实失败链路；场景脚本必须执行到可观察的最终状态，不能因预期失败而提前停止或伪造成功。
   纯后端/API 题在 `apiRequests` 中模拟多步业务请求（可用 `extract` 提取 token 传给后续请求），断言失败按真实失败结果记录。
10. 运行 `status` 复核所有产物。
11. `status=complete` 后直接运行 `submit --task-root ROOT --execute`，不再先做单独的人工 dry-run。提交命令会先执行完整预检、刷新历史文案缓存并对 `user_prompt` 与 `gsb_reason` 双重去重。
12. 完整审核通过时，系统自动生成批准记录，`approvedBy` 为设备配置里的审批人，不使用人工审批文件；脚本通过 `/api/v1/submissions/upload` 上传四个文件，再调用 `/api/v1/gsb/submissions` 创建记录并轮询质检终态。
    - 如果预检状态为 `line_gate_approval_required`，确认低于 10 行是唯一阻断项后停止提交，等待设备配置里的审批人 在真实 TTY 中运行 `approve-line-gate --task-root ROOT`；审批后重新运行 `submit --task-root ROOT --execute`。
    - 如果存在其他任何阻断项，直接修复后重跑，不得使用例外审批。

## CLI

```bash
python3 scripts/sologsb.py init --workdir DIR --task-name NAME \
  [--source PATH | --package ZIP | --from-platform --project-code CODE]
python3 scripts/sologsb.py prompt --task-root ROOT --task-type TYPE \
  --difficulty 困难 --candidate FILE --review FILE
python3 scripts/sologsb.py run --task-root ROOT --side both --candidates 2 --attempts 6 --base-url https://<配置的 claude.baseUrl>
python3 scripts/sologsb.py github-init --task-root ROOT  # 候选映射完成后才可执行
python3 scripts/sologsb.py run --task-root ROOT --side A --force  # 重跑已映射候选
python3 scripts/sologsb.py run --task-root ROOT --side B --force  # 重跑已映射候选
python3 scripts/sologsb.py semantic --task-root ROOT --side A  # 任一侧完成后立即核查
# 分别完成 a.review.json、b.review.json 后：
python3 scripts/sologsb.py publish --task-root ROOT \
  --semantic-a ROOT/monitor/semantic/a.review.json \
  --semantic-b ROOT/monitor/semantic/b.review.json
python3 scripts/sologsb.py audit --task-root ROOT
python3 scripts/sologsb.py gsb --task-root ROOT --draft FILE --review FILE
python3 scripts/sologsb.py record --task-root ROOT --side A --plan FILE [--lock-timeout SECONDS]
python3 scripts/sologsb.py status --task-root ROOT
python3 scripts/sologsb.py submit --task-root ROOT                         # 可选诊断：审核 + dry-run
python3 scripts/sologsb.py submit --task-root ROOT --execute              # 完整审核通过后直接提交，自动记录设备配置里的审批人
python3 scripts/sologsb.py approve-line-gate --task-root ROOT            # 仅改动量单项失败时，由设备配置里的审批人在 TTY 中批准
python3 scripts/sologsb.py submit --task-root ROOT --approval APPROVAL --execute
python3 scripts/sologsb.py cleanup --task-root ROOT  # 同时释放平台项目占用锁
python3 scripts/sologsb.py --version                                    # 打印统一全局版本号
python3 scripts/sologsb.py version                                       # 打印版本 JSON
```

## 参考资料

- `references/prompt-standard.md`：提示词、难度和查重门禁。
- `references/workspace-layout.md`：候选目录、映射状态和 GitHub 约定。
- `references/trace-validation.md`：单轮轨迹硬校验和语义完成审核。
- `references/semantic-review-template.json`：A/B 语义完成审核模板。
- `references/side-scoring.md`：单侧完成后的 Solo2 五维初评口径。
- `references/gsb-evidence.md`：证据索引、结论和 GSB 文案。
- `references/reason-writing-rules.md`：GSB 文案编写通则（用词、句式、排版、结构与交稿自检清单），所有题目共用同一口径。
- `references/reason-word-replacements.json`：文案禁用词与推荐替换词表；新增词只改这一处，`gsb_tools.py` 与提交预检共用。
- `references/delivery-scoring.md`：A/B 交付完整性五档评分锚点、描述写法、正反例与草稿 `delivery` 格式。
- `references/recording.md`：成功、失败和不同题型录屏。
- `references/gsb-form-schema.json`：官方只读 schema 快照。
- `references/gsb-draft-template.json`：带证据 ID 的 GSB 草稿模板。
- `references/ra-renhua-review-template.json`：固定版本 ra-人话审核记录模板。
- `references/verification-plan-template.json`：A/B 真实验证命令模板。
- `references/change-volume-gate.md`：G11 改动量、仓库洁净和提交前硬门禁。
- `references/lessons-learned-20260917.md`：0917 实测避坑与固定处理顺序。
- `references/key-concurrency.md`：单 Key 并发、429 恢复和监控台执行门禁。
- `references/monitor-prompt-template.md`：可直接复制到监控台的优化提示词模板。
- `references/field-guide-template.md`：当前 schema 字段填写说明模板。
- `references/final-delivery-format.md`：任务完成及 SOLO2 提交后的固定八段式最终交付展示模板；属于硬门禁。

## 完成判定

只有 `status` 同时确认提示词哈希、Git 三支、两份干净轨迹、两个产物快照、
两侧真实验证、A/B `lineGate` 记录、G11 仓库洁净门禁、150–240 字 GSB 理由、A/B 交付完整性打分与描述、官方 schema Excel、字段说明和两段
1280x720、不超过 90 秒的视频，以及逐片段 `window-capture`、`cursor-guard` 报告、
`frontmost-window-monitor.json`、`service-cleanup.json` 均存在且状态通过，
并且 `recordingMetadata.activationPerformed=false`、`untouched=true`、焦点恢复有记录、服务无残留，
任务才可标为 `complete`。其中 A/B 任一侧可以是真实失败结果，但不能缺录屏；
`lineGate` 可以是 `failed`，但必须已生成并记录；平台提交阶段再执行最终阻断或等待例外审批。结论、评分、轨迹、命令输出和视频必须相互一致。最终回复必须严格使用 `references/final-delivery-format.md` 的八段式模板；未提交时明确写“未执行（仅本地交付）”，提交后必须补全 submission id、质检终态和 API 结果路径。标题、顺序、绝对路径或视频内嵌任一缺失，均不得标记交付完成。
