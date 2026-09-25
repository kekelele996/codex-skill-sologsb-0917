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
