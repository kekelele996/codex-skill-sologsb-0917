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
