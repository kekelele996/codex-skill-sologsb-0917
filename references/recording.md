# 录屏规则

- Web：最终画面只允许 Otty 和完整 Chrome 窗口；Otty 启动项目，Chrome 负责真实操作关键验收路径。
- API/CLI：最终画面只允许 Otty，使用 Otty CLI 的真实命令和 pane 展示输出。
- 只允许 `terminalApp=otty`；Web 题必须同时有 Otty 和 Chrome，API/CLI/失败题只允许 Otty。禁止 iTerm2、桌面模式和整屏采集。
- 禁止把桌面应用、IDE、Finder、系统设置、终端以外的工具纳入最终画面。
- 失败也必须有录屏：应用能启动但关键业务路径报错时，仍录 Otty 启动过程和真实 Chrome 中的报错画面；
  应用无法启动时只录 Otty 的真实启动与报错。不得因为预期失败而跳过该侧。
- 启动失败或场景失败是非零结果，不是录制失败；只要视频真实生成，就必须保留 `browser-result.json`、
  终端日志、退出码和失败截图，并把失败事实写进评分与 GSB。
- 禁止 headless 冒充、注入字幕、Demo 标签或后期伪造终端。
- 所有录屏强制使用 macOS 数字 `CGWindowID`，通过 ScreenCaptureKit 的独立窗口滤镜和 `SCRecordingOutput` 采集；必须设置 `showsCursor=false`、`showMouseClicks=false`、`capturesAudio=false`。不再录制整屏后按 bounds 裁切，也不得保留桌面、Dock、菜单栏、通知或其他应用窗口。
- 红线：录制前必须分别定位目标应用的 `kCGWindowNumber`。Web 题缺少 Otty 或 Chrome 窗口时先打开；仍无法定位、`windowId<=0`、窗口 ID 失效、窗口被最小化、不在当前 Space、应用不是 Otty/Google Chrome，或采集状态非 `ok` 时立即停止，禁止回退到整屏/裁切/iTerm2/headless。
- 开录瞬间必须再次读取该窗口 ID，确认 `ownerPid + ownerName` 与首次定位一致；不允许只按 PID、标题或面积猜测目标窗口。
- 录制全程不得激活、置前或最小化录制窗口，也不得调用 Otty `window focus` 或定时 `page.bringToFront()`。
- ChatGPT 不属于录制目标。录制器不得最小化、激活、移动或恢复任何 ChatGPT 窗口，也不要求生成
  `chatgpt-window-guard.json`。
- 每侧一段，统一输出 1280x720（720p），单段不超过 90 秒；超时即失败，不做静默裁剪。
- 录制结论必须与画面一致：视频显示成功时结论才可写成功；视频显示启动失败、页面崩溃或关键操作失败时，
  结论必须写失败，不能用轨迹中的局部测试通过抵消真实运行失败。
- 最终视频必须由 `ffprobe` 验证为 1280x720；分辨率不符时不得写入交付表。
- 录屏计划写到 `workspace/视频信息/<side>/脚本/record-plan.json`，Web scenario
  写到同目录 `scenario.cjs`。脚本必须使用本地测试数据。
- Web 失败计划设置 `expectedBrowserFailure=true`。运行器允许浏览器场景以非零状态结束并保留视频，
  但若预期失败而应用实际成功，则录制失败；结果中的 `expectedAppFailure`、`observedAppFailure` 和
  `appOutcome` 必须与 GSB 结论一致。
- 输出写 `workspace/视频信息/<side>/视频/demo.mp4` 或 `failed-start.mp4`，两者都必须为 720p。
- 录制前临时开启并记录 `ipc-allow-send-keys`，结束后恢复原值并 reload；不得永久改写 Otty 配置。
- 完成后必须停止开发服务、关闭独立 Chrome、关闭本次 Otty 窗口并生成哈希清单。

## 纯后端 / API 录制

- 纯后端（没有浏览器页面）必须使用 `mode=terminal`，最终画面只允许 Otty，不得打开 Chrome。
- 必须模拟真实 API 请求：在 `record-plan.json.apiRequests` 中声明方法、完整 URL、请求头、请求体、
  期望状态码和关键响应内容。运行器会在 Otty 中真实执行 `curl`，逐条展示请求、响应状态和响应正文。
- 至少覆盖本题的关键验收路径；涉及登录态时用 `extract` 从响应中提取 token，再用 `{{token}}`
  传给后续请求，形成真正的多步业务链路。
- `startCommand` 必须能返回控制权（后台启动服务或 `docker compose up -d`）；用 `commands` 做健康检查或等待，
  用 `cleanupCommands` 在 API 场景结束后停止服务，避免服务残留。
- 请求体和请求头只放测试数据，禁止写入真实凭据。视频里会原样展示请求体和响应正文。
- 服务实际不可用、关键接口报错或断言不匹配时，录屏结果记为失败；失败侧同样要保留真实请求和响应画面。
- 断言失败会以非零退出码收尾，`terminal.log` 中保留 `API_CHECK_FAILED` 与真实状态码，不得改写成成功。

示例：

```json
{
  "mode": "terminal",
  "terminalApp": "otty",
  "targetApps": ["Otty"],
  "captureKind": "window-id",
  "pointerStrategy": "none",
  "requiresApiRequests": true,
  "startCommand": "nohup ./bin/server >/tmp/server.log 2>&1 &",
  "commands": [
    "for i in $(seq 1 30); do curl -sf http://127.0.0.1:8080/health && break; sleep 1; done"
  ],
  "apiRequests": [
    {
      "name": "登录",
      "method": "POST",
      "url": "http://127.0.0.1:8080/api/login",
      "headers": {"Content-Type": "application/json"},
      "body": {"username": "tester", "password": "test-only"},
      "expectedStatus": 200,
      "expectContains": ["token"],
      "extract": {"name": "token", "path": "$.data.token"}
    },
    {
      "name": "查询订单",
      "method": "GET",
      "url": "http://127.0.0.1:8080/api/orders",
      "headers": {"Authorization": "Bearer {{token}}"},
      "expectedStatus": [200],
      "expectContains": ["orderId"]
    }
  ],
  "cleanupCommands": ["pkill -f './bin/server'"]
}
```

`expectedStatus` 支持数字或数组；`expectContains` 支持字符串或字符串数组；
`extract.path` 支持 `$.data.token`、`data.token`、`data.items[0].id` 这类写法。

## 窗口 ID 采集与当前 Space

- Otty、Chrome 都必须先通过 Quartz 定位具体 `kCGWindowNumber`，再用该 ID 启动 ScreenCaptureKit 录制：
  `SCContentFilter(desktopIndependentWindow:)` 只绑定目标窗口，`SCRecordingOutput` 写入 `.mov`。窗口被其他应用遮挡不影响采集内容。
- 必须记录 `windowId`、所属 PID、应用名、窗口名和 bounds。仅按 PID、标题或“面积最大的窗口”还不够，
  实际采集必须绑定稳定窗口 ID。
- 不创建、不切换 macOS Space。录制在当前 Space 执行；脚本不得调用 Space 切换或全屏模式。
- 窗口不存在时按题型打开目标窗口：Web 题先开 Otty，再开独立 Chrome；纯终端题开 Otty。打开后仍定位不到具体窗口 ID 时停止作业。
- 开窗可能短暂把新窗口置前。录制器必须在开窗前记录用户前台应用；仅当最前普通窗口属于本次打开的 Otty/Chrome 进程时，才把用户原应用恢复。恢复事实写入
  `recordingMetadata.userFrontmostAppAtStart` 和 `recordingMetadata.focusRestores`；用户此后切到的其他应用不得被抢回。
  发生抢占时必须在 1 秒内观察到用户原应用重新成为最前应用，并写 `focusRestoreOk=true`。
- 窗口视频报告写 `<片段>-window-capture.json`，包含 `captureKind=window-id`、`captureBackend=screen-capture-kit`、
  `showsCursor=false`、`cursorCaptured=false`、`windowId`、所属 PID、bounds、录制命令、退出码和实际输出文件。
- ScreenCaptureKit 录制器首次运行会在 `$CODEX_HOME/cache/sologsb-0917/bin/` 按源码哈希编译并缓存；
  macOS 26/27 共用 macOS 15+ 部署目标，不需要分别维护两套二进制。首次运行需要给实际启动技能的
  终端或 Codex 应用授予“屏幕录制”权限；未授权时录制器会在 ready 文件出现前失败并写日志。
- 录制器就绪后用信号停止并等待文件落盘。停止器至少保留 4 秒采集时间；最长由看门狗限制为 90 秒，
  避免异常场景无限占用录屏。
- Web 题的 Otty 与 Chrome 分别按各自窗口 ID 采集，再拼接成最终视频。不得用同一窗口 ID、PID 粗匹配或标题猜测替代实际目标窗口 ID。
- Web 题 Chrome 必须增加 `--disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling`；浏览器驱动传 `HUMAN_BROWSER_KEEP_FRONT=0`，默认值仍为保持旧行为的 `1`。
- 录制期间每秒采样最前普通窗口并写 `frontmost-window-monitor.json`。录制窗口在最前的采样数必须为 0；非零时该侧 `ok=false`。

## 非目标窗口

- 录制器不得读取、最小化、激活、移动或恢复 ChatGPT 窗口，也不得把 ChatGPT 状态写入录屏门禁。
- 窗口白名单保持不变：Web 题只允许 Otty/Google Chrome，纯终端题只允许 Otty。

## 鼠标指针处理

- 录制器不得干扰用户鼠标。禁止移动、停靠、恢复指针，禁止查询鼠标按键，也禁止调用任何会改变鼠标位置的系统接口。
- 统一使用 `pointerStrategy=none`，对应 `pointerPolicy=host-input-untouched`。旧计划中的 `background`、`park-pointer` 会自动归一到该策略。
- ScreenCaptureKit 必须设置 `showsCursor=false`，只把目标窗口内容编码进视频。鼠标可以继续在当前桌面正常移动和点击，不应出现在最终视频里。
- 每个片段旁写纯 JSON `<片段>-cursor-guard.json`，字段包含 `segment`、`windowId`、`captureKind`、
  `pointerStrategy`、`pointerPolicy`、`hostInputRespected`、`pointerMoved`、`mouseButtonsQueried`、`parkApplied`、`positionReadOnly`、
  `finalPointerInsideWindow` 和 `status`。`finalPointerInsideWindow` 只记录只读观察结果，不参与门禁。
- 验收要求 `status=ok`、`pointerStrategy=none`、`pointerPolicy=host-input-untouched`、`hostInputRespected=true`、
  `pointerMoved=false`、`mouseButtonsQueried=false`、`parkApplied=false`。鼠标位置读取失败也不得导致重录或阻断。
- 窗口视频还必须满足 `captureBackend=screen-capture-kit`、`showsCursor=false`、`cursorCaptured=false`；
  不执行抽帧擦除、模板匹配或后期去除鼠标，因为这些做法会破坏录屏证据。

## Web 终端日志与收尾

- Web 模式的 `terminal.log` 不得留空或伪造；收尾必须在 Otty 中执行
  `otty pane capture --pane <id> --lines 400`，把真实 pane 文本写入 `terminal.log`。
- 两侧 `finally` 都必须执行计划中的 `cleanupCommands`。随后只查找应用端口上、本次录制开始后才出现的监听 PID；
  录制开始前已存在的进程绝不终止。
- 清理报告写 `service-cleanup.json`，包含 `baselineListeners`、`terminatedAppPortListeners` 和
  `residualAppPortListeners`。后者非空时该侧 `ok=false`。
- 临时 `chrome-profile` 在 Chrome 退出后默认删除，并由 `chrome-profile-cleanup.json` 记录；设置
  `SOLOSGB_0917_KEEP_CHROME_PROFILE=1` 可保留用于排障，证据文件不受影响。
- `recordingMetadata` 必须写 `activationPerformed=false`、`untouched=true`、`userFrontmostAppAtStart`、
  `focusRestores`、`frontmostSampling`、`serviceCleanup`、`residualAppPortListeners` 和 `chromeProfileCleanup`。

## 全局串行锁

- 所有任务共用 `$CODEX_HOME/run/sologsb-0917/recording.lock`，锁文件位于任何任务目录之外。
- 锁以项目为持有单位，同一时刻只允许一个项目执行预检、环境检查、启动、录屏、收尾和状态写入。
- 后到任务默认最多等待 7200 秒；可用 `record --lock-timeout` 调整，设为 `0` 表示立即失败。
- 锁内记录项目标识、task root、A/B 侧、PID、主机、获取时间和等待耗时，并写入录屏结果的
  `globalRecordingLock` 字段。
- 禁止通过删除锁文件、跳过 `record` 或并发启动录屏来绕锁。进程退出或崩溃时由 `flock` 自动释放。

## 场景固定检查

- 先确认当前用户、默认 Tab 和种子数据是否包含目标记录；目标在“我收到的”时先真实点击切换。
- 原生 `<select>` 的 `<option>` 不可见，切换用户后等待 `.user-brief__name strong` 等可见资料区域，禁止等待 option 节点可见。
- `expectedAppFailure=false` 时浏览器场景非零退出属于非预期失败：保留视频、`browser-result.json` 和错误步骤，录制结果写 `ok=false`，修正后重录；不能因为视频文件已生成就写成成功。
- 多卡片页面禁止用 `first()` 盲点同名按钮；优先按卡片容器定位，必要时使用当前可见卡片的 `.last()`。
- 等待成功状态优先使用“双方均已确认”等稳定业务文案，不只等待颜色或状态徽标。
- pnpm 工作区录屏前使用 `CI=true pnpm install --frozen-lockfile`；若出现构建批准错误，再执行 `pnpm approve-builds --all` 后重装。

## 踩坑加固

- 录制前先执行 `preflightCommands`：构建镜像、准备测试数据和检查入口，正式录屏只做快速启动，
  不把 Docker 拉取、构建或数据初始化过程放进视频。
- 终端不得显示用户真实绝对路径。Otty 直接以项目目录启动，开录前执行
  `export PS1='sologsb %1~ %# '` 和 `clear`，只显示相对路径。
- Chrome 必须关闭密码管理器、账号登录、同步、资料菜单推广和首启引导：使用临时 profile，并保留录制器内置的禁用参数。
- 开录前检查 Chrome 页面没有“登录 Chrome?”、密码保存、翻译、通知、下载或崩溃恢复气泡；出现任一浮层时不得开始录制，修正启动参数后重录。
- Web 终端启动命令应优先只监听回环地址，避免出现多网卡 `Network:` 地址行；终端只保留与验收路径直接相关的输出。
- Web 自动操作默认 `pace>=1.8`。浏览器内交互使用 Playwright 合成事件并保持真实持续时间和缓动；不得使用任何系统级鼠标移动接口，录制期间主机指针始终不受影响。
  点击前停顿、点击后回落、步骤之间留出可观察间隔，避免看起来像脚本瞬移。
- 对关键状态使用明确的 `wait` 或元素等待后再继续；场景总时长以 20–75 秒为目标，仍受单段 90 秒上限约束。
- 启动命令使用单行；Otty 中发送的启动命令不得包含换行。
- 浏览器场景中使用可访问名称或稳定的 CSS 选择器；Ant Design 按钮可能显示为“改 期”。

## 命名约定

最终视频统一保存为：

```text
workspace/视频信息/a/视频/<项目编号-项目名>-验证A产物.mp4
workspace/视频信息/b/视频/<项目编号-项目名>-验证B产物.mp4
```

录制器会从 `monitor/platform-selection.json` 的 `projectCode` 和 `projectName` 生成名称，
并在 `record_side` 中强制覆盖计划里的 `outputName`，避免误用 `demo.mp4`。
