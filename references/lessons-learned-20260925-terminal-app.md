# 2026-09-25 录屏终端从 Otty 切换到 Terminal.app

- Otty 录屏频繁崩溃，且 CLI 偶发 IPC 超时后迟到创建窗口；为压住录制窗口额外打开的 `-guard` 锚点窗口会持续抢前台，
  一次录制出现多个终端窗口。改用 Terminal.app 后每侧只开一个窗口，不再需要锚点。
- 旧流程先 `open -a Otty` 再创建前台守卫，守卫记下的“用户原应用”其实是 Otty；现在守卫必须在启动任何录制应用之前创建。
- `open -na "Google Chrome" --new-window` 会经 LaunchServices 激活 Chrome 抢前台；改为直接运行二进制并带
  `--no-startup-window`，再用 CDP `Target.createTarget(background:true)` 在后台建窗。
- Terminal AppleScript 的 `id of window` 与 Quartz `kCGWindowNumber` 一致，可直接绑定 ScreenCaptureKit；Quartz 的
  `kCGWindowOwnerName` 是本地化名（中文系统为“终端”），门禁必须按 bundle id 归一。
- tty 上仍有进程时关闭 Terminal 窗口会弹出持久的确认表单（后续 AppleScript 会卡住）；必须先 TERM → HUP → KILL
  清空该 tty 的当前用户进程（zsh 忽略 TERM；root 的 `login` 进程不用管），再 `close`。`pkill -t` 在 macOS 上不可靠，改用 `ps -t`。
- Terminal 重新启动会恢复上次窗口；后台启动需加 `-ApplePersistenceIgnoreState YES`，并关闭本次启动产生的默认窗口。
- `exec /bin/zsh -f` 让录制画面不受用户 rc/主题影响，但会丢失 rc 中的函数与别名（如 `nvm`）；导出的 PATH 仍然继承。

## 端到端实测补充（cy-406 副本，A/B 各录一次）

- **标题栏泄露命令行**：Terminal 默认在标题里显示前台进程及参数，`npm run dev` 期间会露出
  `esbuild · npm run dev --host … ANTHROPIC_…` 这类完整命令行。AppleScript 改不了这些开关，
  只能写进 prefs（`ShowActiveProcess*InTitle` 等，见 `TERMINAL_PROFILE_TITLE_KEYS`），而且 Terminal
  只在启动时读取 prefs。因此要在 `_terminal_ensure_ready()` 之前调用 `_terminal_write_profile_prefs()`；
  开窗后 `_terminal_assert_clean_title()` 还要求标题只能是 `sologsb — sologsb`，否则拒绝录制，
  并提示用户完全退出 Terminal 后重试。
- **焦点守卫漏判**：`chrome-open` 检查只看 Chrome 自己的 pid。如果此时最前面是更早创建的
  Terminal 录制窗口，就会被当作“不是录制进程”跳过，结果 5 次前台采样落在录制窗口上。
  现在改为对照本次录制累计的全部 `recording_pids`。
- **报告汇总串目录**：旧的 `recording/<side>/web-otty/` 残留会被 `rglob` 一起汇总进来，导致白名单
  判为 {Otty, Terminal, Chrome} 而失败。现在汇总范围限定在本次的 `runtime_dir`。

## 画面清晰度（分辨率保持 1280x720）

- 模糊的根源是**多次有损编码**：片段转码（crf20 veryfast）→ 拼接（ffmpeg 默认 crf23）→ 成片（crf20 veryfast），
  每过一次，UI 文字边缘就糊一层；缩放也用的是默认 bicubic。现在中间产物用 crf10，只有成片用
  `preset slow / crf 16 / tune animation`，缩放统一用 lanczos（`VIDEO_SCALE_FILTER`、`*_X264_ARGS`）。
  同一份原始录制，与无损 lanczos 参考相比，PSNR 44.8→55.0 dB，40 秒成片约 360KB→630KB。
- Chrome 窗口由 1440x900（16:10）改为 1440x810（16:9）：2x 采集得到 2880x1620，正好 2.25 倍缩到
  1280x720，两边不再有黑边，网页有效宽度从 1136 像素增加到 1280 像素，CSS 宽度不变，页面布局不受影响。

## 防串录实测（2026-09-25）

- 录制器用 `SCContentFilter(desktopIndependentWindow:)`，只采集目标窗口自己的画面，不采集屏幕合成后的结果；
  同时 `capturesAudio=false`、`showsCursor=false`。
- 对抗实验：Terminal 窗口录制期间，另起一个进程放置悬浮的洋红色窗口，盖住目标窗口左半边（经 CGWindowList
  前后顺序确认确实在其上方），同时弹出一条系统通知。逐帧扫描 64 帧，洋红像素数为 0；被遮住的区域照样录到了
  Terminal 自己的内容，通知也没有出现。
- 窗口来源：Terminal 窗口 ID 由创建它的 AppleScript 直接返回；Chrome 窗口按本次启动的进程 pid 选取（该进程
  使用独立的临时 `--user-data-dir`，与用户自己的 Chrome 不是同一进程）。录制前按 owner 白名单校验，
  SCK 就绪文件里的 windowId 也必须与之一致。

## 最前窗口采样只覆盖采集时段（2026-09-26）

- 用户自己也在用 Google Chrome 时，用户的前台 Chrome 窗口在录制中换窗、关窗会让 macOS 在同一个 app 的
  窗口之间重新排序，短暂把本次录制的 Chrome 窗口顶到最前一次；实测 `sologsb-1003` 连续四次落在
  `browser-screen.mov` 收尾之后（`browser-cropped.mp4` 生成前后）的那一秒，采集早已停止。
- 这段收尾不属于“录制期间”，却会把 `recordingWindowFrontmostSamples` 记成 1，整侧被判 `ok=false`。
  现在 `_FrontmostWindowMonitor` 增加 `resume()` / `pause()`：`_start_window_segment` 在真正的窗口采集
  进程启动前 `resume()`，`_stop_window_segment` 在采集进程停止后立刻 `pause()`，报告里写
  `sampledDuringCaptureOnly=true`。采集时段内的采样强度不变，收尾不再计入。

## 采集期间改窗口大小会让页面比例失真（2026-09-27）

- `sologsb-1018` 需要在 820px 宽的手机断点下录屏，scenario 里用 `Browser.setWindowBounds` 把录制窗口从
  `1440x810` 改成 `820x560`。ScreenCaptureKit 的采集面在开录时就固定成 `1440x810`，窗口改小后 macOS 把
  窗口内容整体放大 1.45 倍填满采集面高度，成片里页面被放大、右侧多出 18% 黑边（原始 `browser-screen.mov`
  仍是 1440x810，输出帧里内容只占 0–1055 列），页面比例明显不正常。
- 修法是把窗口尺寸提前到开录之前：计划里新增 `browserWindow: {left, top, width, height}`，录制器用它创建
  Chrome 窗口，默认仍是 `1440x810`，并要求 16:9（容差 1.5%）；需要窄版就写 `820x461`，这样采集面与窗口
  从一开始就一致，成片铺满 1280x720 且页面按正常比例显示。
- 采集结束后录制器重新读取窗口尺寸，写 `windowBoundsAtCaptureStart` / `windowBoundsAtCaptureStop` /
  `windowBoundsChangedDuringCapture`；尺寸变过就把该片段标成 `failed`，`record` 返回非零并退回 `gsb_ready`，
  防止这类比例失真的成片被当成通过。
