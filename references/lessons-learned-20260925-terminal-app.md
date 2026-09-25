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
