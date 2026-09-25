#!/usr/bin/env python3
"""Generate and execute real window-ID recording plans for A/B."""
from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import urlopen

from common import (
    CODEX_HOME,
    DEFAULT_RECORDING_LOCK_TIMEOUT,
    RECORDER_DIR,
    SologsbError,
    global_recording_lock,
    read_json,
    run,
    safe_local_name,
    save_state,
    utc_now,
    write_json,
)
from gsb_tools import refresh_excel

SKILL_ROOT = Path(__file__).resolve().parents[1]
CHECK_ENV = RECORDER_DIR / "scripts" / "check_environment.sh"
BROWSER_DRIVER = RECORDER_DIR / "scripts" / "human_browser_driver.cjs"
CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_BUNDLE_ID = "com.google.Chrome"
# 16:9 so the 2x capture (2880x1620) fills 1280x720 without pillarboxing.
CHROME_WINDOW_BOUNDS = (40, 40, 1440, 810)
TERMINAL_BUNDLE_ID = "com.apple.Terminal"
# Quartz reports the localized process name, e.g. "终端" on a Chinese system.
TERMINAL_OWNER_NAMES = {"Terminal", "终端"}
TERMINAL_PROFILE_NAME = "sologsb"
# AppleScript bounds: left, top, right, bottom.
TERMINAL_WINDOW_BOUNDS = (40, 40, 1320, 760)
# Every lossy pass smears UI text, so intermediates are near-lossless and only the
# final file uses the delivery CRF; lanczos keeps glyph edges crisp when downscaling.
VIDEO_SCALE_FILTER = (
    "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,"
    "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
)
INTERMEDIATE_X264_ARGS = ["-c:v", "libx264", "-preset", "fast", "-crf", "10", "-tune", "animation", "-pix_fmt", "yuv420p"]
FINAL_X264_ARGS = ["-c:v", "libx264", "-preset", "slow", "-crf", "16", "-tune", "animation", "-pix_fmt", "yuv420p"]
TERMINAL_OSASCRIPT_TIMEOUT = 20.0
TERMINAL_APP = "terminal"
TERMINAL_APP_ALIASES = {"", "terminal", "terminal.app", "otty"}
TERMINAL_TARGET_ALIASES = {"Terminal", "Terminal.app", "终端", "Otty"}
WEB_RUNTIME_DIR = "web-terminal"
TERMINAL_RUNTIME_DIR = "terminal-session"
SCK_RECORDER_SOURCE = SKILL_ROOT / "scripts" / "screencapturekit_window_recorder.swift"
SCK_RECORDER_CACHE_DIR = CODEX_HOME / "cache" / "sologsb-0917" / "bin"
SCK_RECORDER_MIN_MACOS = "15.0"
SCK_READY_TIMEOUT_SECONDS = 20.0
WINDOW_CAPTURE_MAX_SECONDS = 90
WINDOW_CAPTURE_MINIMUM_SECONDS = 4.0
FRONTMOST_SAMPLE_SECONDS = 1.0
POINTER_POLICIES = {
    "none": "host-input-untouched",
}
LEGACY_POINTER_STRATEGIES = {"background", "park-pointer"}
RECORDING_WINDOW_OWNERS = TERMINAL_OWNER_NAMES | {"Google Chrome", "Chrome"}
RECORDING_WINDOW_BUNDLES = {TERMINAL_BUNDLE_ID: "Terminal", CHROME_BUNDLE_ID: "Chrome"}


TERMINAL_OPEN_SCRIPT = """
on run argv
  tell application "Terminal"
    set t to do script ""
    set w to first window whose tabs contains t
    set current settings of t to settings set (item 2 of argv)
    set custom title of t to (item 1 of argv)
    set bounds of w to {(item 3 of argv) as integer, (item 4 of argv) as integer, (item 5 of argv) as integer, (item 6 of argv) as integer}
    return ((id of w) as text) & "|" & (tty of t)
  end tell
end run
"""

# Terminal only exposes these title toggles through its prefs, not AppleScript.
# Without them the title bar shows the foreground command line (e.g. `env ... npm run dev`).
TERMINAL_PROFILE_TITLE_KEYS = {
    "ShowActiveProcessInTitle": False,
    "ShowActiveProcessArgumentsInTitle": False,
    "ShowActiveProcessInTabTitle": False,
    "ShowActiveProcessArgumentsInTabTitle": False,
    "ShowCommandKeyInTitle": False,
    "ShowComponentsWhenTabHasCustomTitle": False,
    "ShowDimensionsInTitle": False,
    "ShowRepresentedURLInTitle": False,
    "ShowRepresentedURLPathInTitle": False,
    "ShowShellCommandInTitle": False,
    "ShowTTYNameInTabTitle": False,
    "ShowTTYNameInTitle": False,
    "ShowWindowSettingsNameInTitle": False,
}

TERMINAL_PROFILE_SCRIPT = """
on run argv
  set profileName to item 1 of argv
  tell application "Terminal"
    if not (exists settings set profileName) then
      make new settings set with properties {name:profileName}
    end if
    tell settings set profileName
      set title displays device name to false
      set title displays shell path to false
      set title displays window size to false
      set title displays settings name to false
      set title displays custom title to true
    end tell
  end tell
end run
"""


def _osascript(
    script: str,
    *args: str,
    timeout: float = TERMINAL_OSASCRIPT_TIMEOUT,
    check: bool = True,
) -> str:
    try:
        proc = run(
            ["osascript", "-", *[str(value) for value in args]],
            input_data=script.encode("utf-8"),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SologsbError(f"osascript 超时({timeout:.0f}s)，Terminal 可能弹出了确认对话框") from exc
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        if "-1743" in error:
            raise SologsbError(
                "未授权自动化控制 Terminal：请在 系统设置 > 隐私与安全性 > 自动化 中允许当前进程控制“终端”"
            )
        if check:
            raise SologsbError(f"osascript 失败: {error or proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _terminal_is_running() -> bool:
    return run(["pgrep", "-x", "Terminal"], check=False).returncode == 0


def _terminal_window_ids() -> set[int]:
    # Never talk to Terminal via AppleScript unless it is already running:
    # an AppleScript launch opens a foreground default window.
    if not _terminal_is_running():
        return set()
    output = _osascript('tell application "Terminal" to get id of every window', check=False)
    return {int(value) for value in re.findall(r"\d+", output)}


def _terminal_window_tty(window_id: int) -> str:
    return _osascript(
        'on run argv\ntell application "Terminal" to get tty of tab 1 of window id ((item 1 of argv) as integer)\nend run',
        str(window_id),
        check=False,
    )


def _tty_user_pids(tty: str) -> list[int]:
    """PIDs owned by the current user on a tty; the root-owned login process is left alone."""
    name = Path(str(tty or "")).name
    if not name:
        return []
    proc = run(["ps", "-t", name, "-o", "pid=,user="], check=False)
    user = getpass.getuser()
    pids: list[int] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1] == user:
            pids.append(int(parts[0]))
    return pids


def _terminal_close_window(window_id: int | str, tty: str = "") -> bool:
    """Close a recorder-owned Terminal window without triggering the running-process sheet."""
    try:
        window_id = int(window_id or 0)
    except (TypeError, ValueError):
        return False
    if window_id <= 0:
        return False
    if not tty:
        tty = _terminal_window_tty(window_id)
    if tty:
        # zsh ignores TERM, so escalate TERM -> HUP -> KILL until the tty is empty.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
            pids = _tty_user_pids(tty)
            if not pids:
                break
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, sig)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and _tty_user_pids(tty):
                time.sleep(0.1)
        if _tty_user_pids(tty):
            # Closing now would leave a persistent "terminate processes?" sheet.
            return False
    _osascript(
        "on run argv\n"
        "with timeout of 5 seconds\n"
        'tell application "Terminal" to close (every window whose id is ((item 1 of argv) as integer))\n'
        "end timeout\n"
        "end run",
        str(window_id),
        check=False,
    )
    return True


def _terminal_ensure_ready() -> bool:
    """Start Terminal.app in the background; return True when the recorder launched it."""
    if _terminal_is_running():
        return False
    run(
        ["open", "-g", "-b", TERMINAL_BUNDLE_ID, "--args", "-ApplePersistenceIgnoreState", "YES"],
        check=False,
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not _terminal_is_running():
        time.sleep(0.2)
    if not _terminal_is_running():
        raise SologsbError("无法在后台启动 Terminal.app")
    # A fresh launch opens one default window; only those startup windows are ours to close.
    startup: set[int] = set()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not startup:
        startup = _terminal_window_ids()
        time.sleep(0.2)
    for window_id in startup:
        _terminal_close_window(window_id)
    return True


def _terminal_quit_if_launched(launched: bool) -> None:
    if launched and _terminal_is_running() and not _terminal_window_ids():
        _osascript(
            'with timeout of 5 seconds\ntell application "Terminal" to quit\nend timeout',
            check=False,
        )


def _terminal_ensure_profile() -> None:
    _osascript(TERMINAL_PROFILE_SCRIPT, TERMINAL_PROFILE_NAME)


def _terminal_write_profile_prefs() -> None:
    """Write the title toggles into the profile; Terminal only reads them at launch."""
    from Foundation import NSUserDefaults  # type: ignore

    defaults = NSUserDefaults.standardUserDefaults()
    domain = dict(defaults.persistentDomainForName_(TERMINAL_BUNDLE_ID) or {})
    settings = dict(domain.get("Window Settings") or {})
    profile = dict(settings.get(TERMINAL_PROFILE_NAME) or {})
    profile.setdefault("name", TERMINAL_PROFILE_NAME)
    profile.setdefault("type", "Window Settings")
    profile.setdefault("ProfileCurrentVersion", 2.09)
    if all(profile.get(key) == value for key, value in TERMINAL_PROFILE_TITLE_KEYS.items()):
        return
    profile.update(TERMINAL_PROFILE_TITLE_KEYS)
    settings[TERMINAL_PROFILE_NAME] = profile
    domain["Window Settings"] = settings
    defaults.setPersistentDomain_forName_(domain, TERMINAL_BUNDLE_ID)


def _terminal_title_is_clean(title: str) -> bool:
    parts = [part.strip() for part in str(title or "").split("\u2014")]
    return bool(title) and all(part == TERMINAL_PROFILE_NAME for part in parts)


def _terminal_assert_clean_title(window_id: int, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    title = ""
    while time.monotonic() < deadline:
        title = str(_window_info_by_id(window_id).get("windowName") or "")
        if _terminal_title_is_clean(title):
            return
        time.sleep(0.2)
    raise SologsbError(
        f"Terminal 窗口标题会暴露进程/参数({title!r})：sologsb 描述文件的标题设置只在 Terminal 启动时读取，"
        "请完全退出 Terminal.app 后重试"
    )


def _terminal_send(window_id: int, command: str) -> None:
    _osascript(
        'on run argv\ntell application "Terminal" to do script (item 2 of argv) in tab 1 of window id ((item 1 of argv) as integer)\nend run',
        str(window_id),
        command,
    )


def _terminal_history(window_id: int) -> str:
    return _osascript(
        'on run argv\ntell application "Terminal" to get history of tab 1 of window id ((item 1 of argv) as integer)\nend run',
        str(window_id),
        check=False,
    )


def _terminal_clean_shell_command(cwd: Path | None) -> str:
    """Neutral prompt/title with no absolute paths, then wipe screen and scrollback."""
    setup = [f"cd {shlex.quote(str(cwd))}"] if cwd is not None else []
    setup.append("export PS1='sologsb %1~ %# '")
    setup.append("precmd() { print -Pn '\\e]7;file:///sologsb\\a\\e]2;sologsb\\a\\e]1;sologsb\\a' }")
    return " && ".join(setup) + "; clear && printf '\\033[3J'"


def _terminal_wait_clean_prompt(window_id: int, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _terminal_history(window_id).strip()
        if last.startswith("sologsb ") and last.endswith("%") and "\n" not in last:
            return
        time.sleep(0.3)
    raise SologsbError(f"Terminal 窗口未进入干净提示符: {last[-200:]!r}")


def _terminal_open_window(title: str, cwd: Path | None = None) -> tuple[int, str]:
    """Open one background Terminal window with a clean zsh; return (CGWindowID, tty)."""
    before = _terminal_window_ids()
    left, top, right, bottom = TERMINAL_WINDOW_BOUNDS
    try:
        output = _osascript(
            TERMINAL_OPEN_SCRIPT,
            title,
            TERMINAL_PROFILE_NAME,
            str(left),
            str(top),
            str(right),
            str(bottom),
        )
        window_text, _, tty = output.partition("|")
        window_id = int(window_text.strip())
        tty = tty.strip()
        if window_id <= 0 or not tty:
            raise SologsbError(f"Terminal 返回了无效窗口: {output!r}")
    except Exception:
        for orphan in _terminal_window_ids() - before:
            _terminal_close_window(orphan)
        raise
    try:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not _tty_user_pids(tty):
            time.sleep(0.2)
        # -f skips user rc files so plugins/themes never leak into the recording;
        # the exported environment (PATH etc.) is inherited from the login shell.
        _terminal_send(window_id, "exec /bin/zsh -f")
        time.sleep(0.5)
        _terminal_send(window_id, _terminal_clean_shell_command(cwd))
        _terminal_wait_clean_prompt(window_id)
        _terminal_assert_clean_title(window_id)
    except Exception:
        _terminal_close_window(window_id, tty)
        raise
    return window_id, tty


def _terminal_prepare_clean(window_id: int) -> None:
    _terminal_send(window_id, "clear && printf '\\033[3J'")
    _terminal_wait_clean_prompt(window_id)


def _capture_terminal_text(window_id: int, output: Path) -> str:
    """Persist the real Terminal scrollback for Web recordings."""
    text = _terminal_history(window_id)
    if not text.strip():
        raise SologsbError("无法抓取 Terminal 窗口文本: 空输出")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")
    return text


def _app_bundle_id(pid: int) -> str:
    try:
        from AppKit import NSRunningApplication  # type: ignore

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        return str(app.bundleIdentifier() or "") if app is not None else ""
    except Exception:
        return ""


def _canonical_owner(owner_name: str, bundle_id: str = "") -> str:
    """Map localized owner names (e.g. "终端") and bundle IDs onto Terminal/Chrome."""
    if bundle_id in RECORDING_WINDOW_BUNDLES:
        return RECORDING_WINDOW_BUNDLES[bundle_id]
    name = str(owner_name or "").strip()
    if name in TERMINAL_OWNER_NAMES:
        return "Terminal"
    if name in {"Google Chrome", "Chrome"}:
        return "Chrome"
    return name


def _window_info_payload(window: dict[str, Any]) -> dict[str, Any] | None:
    raw_bounds = window.get("kCGWindowBounds")
    if not isinstance(raw_bounds, Mapping):
        return None
    bounds = dict(raw_bounds)
    if not all(key in bounds for key in ("X", "Y", "Width", "Height")):
        return None
    window_id = window.get("kCGWindowNumber")
    if window_id is None:
        return None
    return {
        "windowId": int(window_id),
        "ownerPid": int(window.get("kCGWindowOwnerPID") or 0),
        "ownerName": str(window.get("kCGWindowOwnerName") or ""),
        "windowName": str(window.get("kCGWindowName") or ""),
        "layer": int(window.get("kCGWindowLayer") or 0),
        "bounds": ",".join(str(int(bounds[key])) for key in ("X", "Y", "Width", "Height")),
    }


def _window_list() -> list[dict[str, Any]]:
    try:
        import Quartz  # type: ignore
    except Exception as exc:
        raise SologsbError("缺少 Quartz，无法读取窗口信息") from exc
    return list(
        Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
    )


def _frontmost_window_info() -> dict[str, Any] | None:
    """Return the frontmost normal on-screen window without activating anything."""
    try:
        for window in _window_list():
            if int(window.get("kCGWindowLayer") or 0) != 0:
                continue
            if float(window.get("kCGWindowAlpha") or 1.0) <= 0:
                continue
            payload = _window_info_payload(window)
            if payload:
                return payload
    except Exception:
        return None
    return None



def _live_recording_window(window_id: int) -> dict[str, Any] | None:
    for window in _window_list():
        try:
            current_id = int(window.get("kCGWindowNumber") or 0)
        except (TypeError, ValueError):
            continue
        if current_id != int(window_id):
            continue
        if "kCGWindowIsOnscreen" in window and not bool(window.get("kCGWindowIsOnscreen")):
            return None
        return _window_info_payload(window)
    return None


def _validate_recording_window(window_info: dict[str, Any]) -> dict[str, Any]:
    """Revalidate owner, on-screen state and Space membership immediately before capture."""
    window_id = int(window_info.get("windowId") or 0)
    owner_pid = int(window_info.get("ownerPid") or 0)
    owner_name = str(window_info.get("ownerName") or "").strip()
    allowed = owner_name in RECORDING_WINDOW_OWNERS or (
        owner_pid > 0 and _app_bundle_id(owner_pid) in RECORDING_WINDOW_BUNDLES
    )
    if window_id <= 0 or owner_pid <= 0 or not allowed:
        raise SologsbError("窗口不在 Terminal/Google Chrome 白名单内，停止录制")
    live = _live_recording_window(window_id)
    if live is None:
        raise SologsbError(
            f"窗口不存在、已最小化或不在当前 Space，停止录制: windowId={window_id}"
        )
    if int(live.get("ownerPid") or 0) != owner_pid or str(live.get("ownerName") or "") != owner_name:
        raise SologsbError(
            f"窗口所有者已变化，停止录制: windowId={window_id}, "
            f"expected={owner_pid}/{owner_name}, actual={live.get('ownerPid')}/{live.get('ownerName')}"
        )
    return live


class _FocusRestoreGuard:
    """Restore the user's pre-recording app only when the recorder stole focus."""

    def __init__(self) -> None:
        self.started_at = utc_now()
        self.user_frontmost_app = self._capture_frontmost_app()
        self.events: list[dict[str, Any]] = []
        self.recording_pids: set[int] = set()
        self.target_window_ids: set[int] = set()

    @staticmethod
    def _capture_frontmost_app() -> dict[str, Any]:
        try:
            from AppKit import NSWorkspace  # type: ignore

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return {"status": "unavailable", "pid": 0, "name": "", "bundleId": ""}
            return {
                "status": "captured",
                "pid": int(app.processIdentifier()),
                "name": str(app.localizedName() or ""),
                "bundleId": str(app.bundleIdentifier() or ""),
                "capturedAt": utc_now(),
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "pid": 0,
                "name": "",
                "bundleId": "",
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _activate_original(self) -> bool:
        bundle_id = str(self.user_frontmost_app.get("bundleId") or "")
        name = str(self.user_frontmost_app.get("name") or "")
        if bundle_id == "com.openai.codex" or name in {"ChatGPT", "Codex"}:
            return False
        pid = int(self.user_frontmost_app.get("pid") or 0)
        if pid <= 0:
            return False
        try:
            from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication  # type: ignore

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is None:
                return False
            return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
        except Exception:
            return False

    def restore_if_recording_frontmost(
        self,
        pids: set[int],
        reason: str,
        target_window_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        targets = {int(pid) for pid in pids if int(pid) > 0}
        self.recording_pids.update(targets)
        if target_window_ids:
            self.target_window_ids.update(int(value) for value in target_window_ids if int(value) > 0)
        frontmost = _frontmost_window_info()
        event: dict[str, Any] = {
            "reason": reason,
            "checkedAt": utc_now(),
            "frontmostWindowId": (frontmost or {}).get("windowId"),
            "frontmostOwnerPid": (frontmost or {}).get("ownerPid"),
            "frontmostOwnerName": (frontmost or {}).get("ownerName"),
            "targetPids": sorted(targets),
            "action": "skipped",
            "restored": False,
        }
        original_pid = int(self.user_frontmost_app.get("pid") or 0)
        frontmost_window_id = int((frontmost or {}).get("windowId") or 0)
        frontmost_owner_pid = int((frontmost or {}).get("ownerPid") or 0)
        same_app_non_target = bool(
            frontmost is not None
            and original_pid > 0
            and frontmost_owner_pid == original_pid
            and frontmost_window_id not in self.target_window_ids
        )
        if same_app_non_target:
            event["action"] = "skipped"
            event["restored"] = True
            event["skipReason"] = "original-app-already-frontmost"
            self.events.append(event)
            return event
        if frontmost is None or frontmost_owner_pid not in self.recording_pids:
            event["skipReason"] = "frontmost-not-recording-process"
            self.events.append(event)
            return event
        event["action"] = "restore-user-app"
        activation_succeeded = self._activate_original()
        event["activationSucceeded"] = activation_succeeded
        restored = False
        if activation_succeeded:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                current = _frontmost_window_info()
                current_window_id = int((current or {}).get("windowId") or 0)
                current_owner_pid = int((current or {}).get("ownerPid") or 0)
                restored = bool(
                    current is not None
                    and current_window_id not in self.target_window_ids
                    and (current_owner_pid not in self.recording_pids or current_owner_pid == original_pid)
                )
                if restored:
                    break
                time.sleep(0.05)
        observed = _frontmost_window_info()
        event["observedFrontmostWindowId"] = (observed or {}).get("windowId")
        event["observedFrontmostOwnerPid"] = (observed or {}).get("ownerPid")
        event["restored"] = bool(restored)
        if not event["restored"]:
            event["skipReason"] = "focus-restore-not-observed"
        self.events.append(event)
        return event

    def restore_final(self) -> None:
        if self.recording_pids:
            self.restore_if_recording_frontmost(
                self.recording_pids,
                "final-cleanup",
                self.target_window_ids,
            )


class _FrontmostWindowMonitor:
    """Sample the frontmost normal window once per second during recording."""

    def __init__(self, report_path: Path) -> None:
        self.report_path = report_path
        self.started_at = utc_now()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._targets: dict[int, dict[str, Any]] = {}
        self._samples: list[dict[str, Any]] = []
        self._recording_frontmost_samples = 0
        self._error = ""

    def register_target(self, window_info: dict[str, Any]) -> None:
        try:
            window_id = int(window_info.get("windowId") or 0)
        except (TypeError, ValueError):
            return
        if window_id <= 0:
            return
        with self._lock:
            self._targets[window_id] = {
                "windowId": window_id,
                "ownerPid": int(window_info.get("ownerPid") or 0),
                "ownerName": str(window_info.get("ownerName") or ""),
                "windowName": str(window_info.get("windowName") or ""),
            }
        self._sample_once()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(FRONTMOST_SAMPLE_SECONDS)

    def _sample_once(self) -> None:
        with self._lock:
            targets = dict(self._targets)
        if not targets:
            return
        frontmost = _frontmost_window_info()
        if frontmost is None:
            self._error = "frontmost-window-unavailable"
            return
        with self._lock:
            self._samples.append(
                {
                    "sampledAt": utc_now(),
                    "windowId": frontmost.get("windowId"),
                    "ownerPid": frontmost.get("ownerPid"),
                    "ownerName": frontmost.get("ownerName"),
                }
            )
            if int(frontmost.get("windowId") or 0) in targets:
                self._recording_frontmost_samples += 1

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        with self._lock:
            targets = [dict(value) for value in self._targets.values()]
            samples = list(self._samples)
            frontmost_samples = self._recording_frontmost_samples
        payload = {
            "status": "ok" if targets and not self._error else ("not-applicable" if not targets else "failed"),
            "path": str(self.report_path.resolve()),
            "pollIntervalSeconds": FRONTMOST_SAMPLE_SECONDS,
            "targets": targets,
            "sampleCount": len(samples),
            "recordingWindowFrontmostSamples": frontmost_samples,
            "error": self._error,
            "samples": samples,
            "startedAt": self.started_at,
            "finishedAt": utc_now(),
        }
        write_json(self.report_path, payload)
        return payload


def _require_window_id(window_info: dict[str, Any], label: str) -> dict[str, Any]:
    """Hard gate: recording may only continue with a stable positive window ID."""
    if not isinstance(window_info, dict):
        raise SologsbError(f"无法定位{label}窗口ID，停止录制")
    try:
        window_id = int(window_info.get("windowId") or 0)
        owner_pid = int(window_info.get("ownerPid") or 0)
    except (TypeError, ValueError) as exc:
        raise SologsbError(f"无法定位{label}窗口ID，停止录制") from exc
    if window_id <= 0 or owner_pid <= 0:
        raise SologsbError(f"无法定位{label}窗口ID，停止录制")
    return window_info


def _window_info_by_id(
    window_id: int,
    *,
    label: str = "Terminal",
    timeout: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for window in _window_list():
            if int(window.get("kCGWindowNumber") or 0) != int(window_id):
                continue
            payload = _window_info_payload(window)
            if payload:
                return _require_window_id(payload, label)
        time.sleep(0.25)
    raise SologsbError(f"无法定位{label}窗口ID，停止录制: windowId={window_id}")


_ACTIVE_CURSOR_GUARD: "_MouseCursorGuard | None" = None
_ACTIVE_WINDOW_CAPTURE: subprocess.Popen[bytes] | None = None
_ACTIVE_WINDOW_CAPTURE_LOG: Any = None
_ACTIVE_WINDOW_CAPTURE_WATCHDOG: threading.Timer | None = None
_ACTIVE_WINDOW_CAPTURE_STARTED: float | None = None


def _bounds_rect(bounds: str) -> tuple[float, float, float, float] | None:
    parts = [item.strip() for item in str(bounds or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        x, y, width, height = (float(item) for item in parts)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _point_inside_rect(
    point: tuple[float, float] | None,
    rect: tuple[float, float, float, float] | None,
) -> bool:
    if point is None or rect is None:
        return False
    x, y = point
    left, top, width, height = rect
    return left <= x <= left + width and top <= y <= top + height


def _mouse_position(quartz: Any) -> tuple[float, float] | None:
    try:
        point = quartz.CGEventGetLocation(quartz.CGEventCreate(None))
        return float(point.x), float(point.y)
    except Exception:
        return None


def normalize_pointer_strategy(value: Any) -> str:
    """Force every recording onto the non-interfering host-input policy."""
    requested = str(value or "none").strip().lower()
    if requested in LEGACY_POINTER_STRATEGIES or requested == "none":
        return "none"
    return requested


class _MouseCursorGuard:
    """Record pointer observations without moving or otherwise taking over the mouse."""

    def __init__(
        self,
        bounds: str,
        report_path: Path | None = None,
        *,
        segment: str = "",
        window_id: int = 0,
        pointer_strategy: str = "none",
    ) -> None:
        self.bounds = bounds
        self.report_path = report_path
        self.segment = segment
        self.window_id = int(window_id)
        self.pointer_strategy = normalize_pointer_strategy(pointer_strategy)
        self.pointer_policy = POINTER_POLICIES.get(self.pointer_strategy, "invalid")
        self.poll_seconds = 0.0
        self.park: tuple[float, float] | None = None
        self.original: tuple[float, float] | None = None
        self.park_applied = False
        self.reapplied_count = 0
        self.skipped_while_dragging = 0
        self.skipped_while_user_active = 0
        self.final_pointer_inside_window: bool | None = None
        self.original_restored = False
        self.pointer_moved = False
        self.mouse_buttons_queried = False
        self.position_read_only = False
        self.started_at = utc_now()
        self._quartz: Any = None

    def start(self) -> None:
        if self.pointer_strategy not in POINTER_POLICIES:
            self._write_report("invalid-pointer-strategy")
            return
        try:
            import Quartz  # type: ignore
        except Exception:
            return
        self._quartz = Quartz
        self.original = _mouse_position(Quartz)
        self.position_read_only = self.original is not None

    def stop(self, status: str = "ok") -> None:
        final_point = _mouse_position(self._quartz) if self._quartz is not None else None
        if final_point is not None:
            self.position_read_only = True
            self.final_pointer_inside_window = _point_inside_rect(
                final_point,
                _bounds_rect(self.bounds),
            )
        effective_status = status
        if status == "ok" and self.pointer_strategy not in POINTER_POLICIES:
            effective_status = "invalid-pointer-strategy"
        self._write_report(effective_status)

    def _write_report(self, status: str) -> None:
        if self.report_path is None:
            return
        payload = {
            "status": status,
            "segment": self.segment,
            "windowId": self.window_id,
            "captureKind": "window-id",
            "pollIntervalSeconds": self.poll_seconds,
            "skippedWhileDragging": self.skipped_while_dragging,
            "pointerStrategy": self.pointer_strategy,
            "pointerPolicy": self.pointer_policy,
            "parkApplied": self.park_applied,
            "parkPoint": None,
            "reappliedCount": self.reapplied_count,
            "skippedWhileUserActive": self.skipped_while_user_active,
            "hostInputRespected": True,
            "pointerMoved": self.pointer_moved,
            "mouseButtonsQueried": self.mouse_buttons_queried,
            "positionReadOnly": self.position_read_only,
            "finalPointerInsideWindow": self.final_pointer_inside_window,
            "bounds": self.bounds,
            "originalPoint": list(self.original) if self.original else None,
            "originalRestored": self.original_restored,
            "startedAt": self.started_at,
            "finishedAt": utc_now(),
        }
        try:
            write_json(self.report_path, payload)
        except Exception:
            pass


def _start_cursor_guard(
    bounds: str,
    report_path: Path | None,
    *,
    segment: str,
    window_id: int,
    pointer_strategy: str,
) -> _MouseCursorGuard:
    global _ACTIVE_CURSOR_GUARD
    _stop_cursor_guard()
    guard = _MouseCursorGuard(
        bounds,
        report_path,
        segment=segment,
        window_id=window_id,
        pointer_strategy=pointer_strategy,
    )
    guard.start()
    _ACTIVE_CURSOR_GUARD = guard
    if guard.pointer_strategy not in POINTER_POLICIES:
        guard.stop("invalid-pointer-strategy")
        _ACTIVE_CURSOR_GUARD = None
        raise SologsbError(f"pointerStrategy 非法: {guard.pointer_strategy}")
    return guard


def _stop_cursor_guard(status: str = "ok") -> None:
    global _ACTIVE_CURSOR_GUARD
    guard = _ACTIVE_CURSOR_GUARD
    _ACTIVE_CURSOR_GUARD = None
    if guard is not None:
        guard.stop(status)


def _swift_compiler() -> str:
    explicit = os.environ.get("SOLOSB_SWIFTC", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise SologsbError(f"SOLOSB_SWIFTC 不可执行: {path}")
    compiler = shutil.which("swiftc")
    if compiler:
        return compiler
    xcrun = shutil.which("xcrun")
    if xcrun:
        proc = subprocess.run(
            [xcrun, "--find", "swiftc"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0:
            candidate = proc.stdout.decode("utf-8", errors="replace").strip()
            if candidate:
                return candidate
    raise SologsbError("缺少 Swift 编译器，无法构建 ScreenCaptureKit 窗口录制器")


def _ensure_sck_recorder() -> Path:
    """Return the cached macOS ScreenCaptureKit window recorder binary."""
    override = os.environ.get("SOLOSB_SCK_RECORDER", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise SologsbError(f"SOLOSB_SCK_RECORDER 不可执行: {path}")

    if platform.system() != "Darwin":
        raise SologsbError("ScreenCaptureKit 窗口录制仅支持 macOS")
    version = platform.mac_ver()[0].split(".")[0]
    if version.isdigit() and int(version) < 15:
        raise SologsbError(f"ScreenCaptureKit 录制要求 macOS 15 或更高，当前 {platform.mac_ver()[0]}")
    if not SCK_RECORDER_SOURCE.is_file():
        raise SologsbError(f"缺少 ScreenCaptureKit 录制器源码: {SCK_RECORDER_SOURCE}")

    digest = hashlib.sha256(SCK_RECORDER_SOURCE.read_bytes()).hexdigest()[:16]
    machine = platform.machine() or "arm64"
    binary = SCK_RECORDER_CACHE_DIR / f"screencapturekit-window-recorder-{machine}-{digest}"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary

    compiler = _swift_compiler()
    SCK_RECORDER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{binary.name}.",
        suffix=".tmp",
        dir=str(SCK_RECORDER_CACHE_DIR),
    )
    os.close(fd)
    temp_binary = Path(temp_name)
    temp_binary.unlink(missing_ok=True)
    command = [
        compiler,
        "-O",
        "-parse-as-library",
        "-swift-version",
        "5",
        "-target",
        f"{machine}-apple-macosx{SCK_RECORDER_MIN_MACOS}",
        "-framework",
        "ScreenCaptureKit",
        "-framework",
        "AVFoundation",
        "-framework",
        "AppKit",
        "-framework",
        "CoreGraphics",
        "-framework",
        "CoreMedia",
        str(SCK_RECORDER_SOURCE),
        "-o",
        str(temp_binary),
    ]
    try:
        proc = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=240,
            check=False,
        )
    except Exception:
        temp_binary.unlink(missing_ok=True)
        raise
    if proc.returncode != 0:
        error = proc.stderr.decode("utf-8", errors="replace").strip()
        temp_binary.unlink(missing_ok=True)
        raise SologsbError(f"ScreenCaptureKit 录制器编译失败: {error or proc.stdout.decode('utf-8', errors='replace').strip()}")
    os.chmod(temp_binary, 0o755)
    os.replace(temp_binary, binary)
    return binary


def _start_window_segment(
    *,
    output: Path,
    pid_file: Path,
    window_info: dict[str, Any],
    pointer_strategy: str = "none",
    frontmost_monitor: _FrontmostWindowMonitor | None = None,
) -> None:
    global _ACTIVE_WINDOW_CAPTURE, _ACTIVE_WINDOW_CAPTURE_LOG, _ACTIVE_WINDOW_CAPTURE_WATCHDOG, _ACTIVE_WINDOW_CAPTURE_STARTED
    if _ACTIVE_WINDOW_CAPTURE is not None and _ACTIVE_WINDOW_CAPTURE.poll() is None:
        raise SologsbError("已有窗口录屏进程在运行")

    recorder = _ensure_sck_recorder()
    output.parent.mkdir(parents=True, exist_ok=True)
    window_info = _validate_recording_window(window_info)
    bounds = str(window_info["bounds"])
    _start_cursor_guard(
        bounds,
        output.with_name(f"{output.stem}-cursor-guard.json"),
        segment=output.stem,
        window_id=int(window_info["windowId"]),
        pointer_strategy=pointer_strategy,
    )
    if frontmost_monitor is not None:
        frontmost_monitor.register_target(window_info)

    log_path = output.with_name(f"{output.stem}-window-recorder.log")
    ready_path = output.with_name(f"{output.stem}-window-recorder-ready.json")
    ready_path.unlink(missing_ok=True)
    output.unlink(missing_ok=True)
    handle = log_path.open("wb")
    command = [
        str(recorder),
        "--window-id",
        str(int(window_info["windowId"])),
        "--output",
        str(output),
        "--ready-file",
        str(ready_path),
        "--max-seconds",
        str(WINDOW_CAPTURE_MAX_SECONDS),
    ]
    proc: subprocess.Popen[bytes] | None = None
    watchdog: threading.Timer | None = None
    try:
        proc = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + SCK_READY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if ready_path.is_file():
                break
            exit_code = proc.poll()
            if exit_code is not None:
                raise SologsbError(f"ScreenCaptureKit 录制器提前退出({exit_code})，详见 {log_path}")
            time.sleep(0.1)
        else:
            raise SologsbError(f"等待 ScreenCaptureKit 录制器就绪超时，详见 {log_path}")

        ready = read_json(ready_path, {}) or {}
        if not isinstance(ready, dict):
            raise SologsbError("ScreenCaptureKit 录制器就绪文件格式无效")
        if ready.get("backend") != "screen-capture-kit":
            raise SologsbError(f"窗口录制后端不是 ScreenCaptureKit: {ready.get('backend')!r}")
        if ready.get("showsCursor") is not False or ready.get("cursorCaptured") is not False:
            raise SologsbError("ScreenCaptureKit 未确认关闭鼠标光标采集")
        if int(ready.get("windowId") or 0) != int(window_info["windowId"]):
            raise SologsbError("ScreenCaptureKit 录制器窗口 ID 不一致")

        _ACTIVE_WINDOW_CAPTURE = proc
        _ACTIVE_WINDOW_CAPTURE_LOG = handle
        owner_bundle_id = _app_bundle_id(int(window_info["ownerPid"]))
        _ACTIVE_WINDOW_CAPTURE_STARTED = time.monotonic()
        watchdog = threading.Timer(
            WINDOW_CAPTURE_MAX_SECONDS,
            lambda: proc.poll() is None and proc.send_signal(signal.SIGINT),
        )
        watchdog.daemon = True
        watchdog.start()
        _ACTIVE_WINDOW_CAPTURE_WATCHDOG = watchdog
        pid_file.write_text(f"{proc.pid}\t{output}\n", encoding="utf-8")
        write_json(
            output.with_name(f"{output.stem}-window-capture.json"),
            {
                "status": "recording",
                "captureKind": "window-id",
                "captureBackend": "screen-capture-kit",
                "showsCursor": False,
                "cursorCaptured": False,
                "minimumMacOS": SCK_RECORDER_MIN_MACOS,
                "windowId": int(window_info["windowId"]),
                "ownerPid": int(window_info["ownerPid"]),
                "ownerName": str(window_info["ownerName"]),
                "ownerBundleId": owner_bundle_id,
                "ownerApp": _canonical_owner(str(window_info["ownerName"]), owner_bundle_id),
                "windowName": str(window_info["windowName"]),
                "bounds": bounds,
                "captureWidth": int(ready.get("width") or 0),
                "captureHeight": int(ready.get("height") or 0),
                "command": command,
                "rawPath": str(output.resolve()),
                "readyPath": str(ready_path.resolve()),
                "logPath": str(log_path.resolve()),
                "ready": ready,
                "startedAt": utc_now(),
            },
        )
    except Exception:
        if watchdog is not None:
            watchdog.cancel()
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        _stop_cursor_guard("start-failed")
        handle.close()
        _ACTIVE_WINDOW_CAPTURE = None
        _ACTIVE_WINDOW_CAPTURE_LOG = None
        _ACTIVE_WINDOW_CAPTURE_WATCHDOG = None
        _ACTIVE_WINDOW_CAPTURE_STARTED = None
        raise


TERMINAL_VISUAL_FRAME_WIDTH = 80
TERMINAL_VISUAL_FRAME_HEIGHT = 52
TERMINAL_VISUAL_CROP = "700:500:300:100"
TERMINAL_VISUAL_MIN_MEAN_STD = 3.0


def _terminal_visual_content_metrics(video: Path) -> dict[str, Any]:
    """Reject blank/static terminal recordings before they can be delivered."""
    width = TERMINAL_VISUAL_FRAME_WIDTH
    height = TERMINAL_VISUAL_FRAME_HEIGHT
    frame_size = width * height
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(video),
        "-vf",
        f"fps=4,crop={TERMINAL_VISUAL_CROP},scale={width}:{height}:flags=area,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    proc = run(command, check=False)
    if proc.returncode != 0:
        return {
            "status": "failed",
            "ok": False,
            "error": proc.stderr.decode("utf-8", errors="replace").strip() or "ffmpeg 画面内容检测失败",
            "minimumMeanStd": TERMINAL_VISUAL_MIN_MEAN_STD,
        }
    raw = proc.stdout
    frame_count = len(raw) // frame_size
    frames = [raw[i * frame_size:(i + 1) * frame_size] for i in range(frame_count)]
    if len(frames) < 4:
        return {
            "status": "failed",
            "ok": False,
            "error": f"有效终端画面帧不足: {len(frames)}",
            "frameCount": len(frames),
            "minimumMeanStd": TERMINAL_VISUAL_MIN_MEAN_STD,
        }
    mean_stds: list[float] = []
    frame_diffs: list[float] = []
    for index, frame in enumerate(frames):
        values = list(frame)
        mean = sum(values) / len(values)
        mean_stds.append((sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5)
        if index:
            previous = frames[index - 1]
            frame_diffs.append(
                sum(abs(a - b) for a, b in zip(frame, previous)) / frame_size
            )
    mean_std = sum(mean_stds) / len(mean_stds)
    max_diff = max(frame_diffs) if frame_diffs else 0.0
    ok = mean_std >= TERMINAL_VISUAL_MIN_MEAN_STD
    return {
        "status": "ok" if ok else "failed",
        "ok": ok,
        "frameCount": len(frames),
        "meanStd": round(mean_std, 4),
        "maxFrameDiff": round(max_diff, 4),
        "minimumMeanStd": TERMINAL_VISUAL_MIN_MEAN_STD,
        "crop": TERMINAL_VISUAL_CROP,
        "scale": f"{width}x{height}",
        "error": "" if ok else "终端录屏没有可辨识内容，疑似空白或未渲染画面",
    }


def _stop_window_segment(
    *,
    raw: Path,
    cropped: Path,
    window_info: dict[str, Any],
    pid_file: Path,
    require_visible_content: bool = False,
) -> None:
    global _ACTIVE_WINDOW_CAPTURE, _ACTIVE_WINDOW_CAPTURE_LOG, _ACTIVE_WINDOW_CAPTURE_WATCHDOG, _ACTIVE_WINDOW_CAPTURE_STARTED
    proc = _ACTIVE_WINDOW_CAPTURE
    handle = _ACTIVE_WINDOW_CAPTURE_LOG
    watchdog = _ACTIVE_WINDOW_CAPTURE_WATCHDOG
    started_monotonic = _ACTIVE_WINDOW_CAPTURE_STARTED
    if proc is None:
        raise SologsbError("没有活动的窗口录屏进程")
    exit_code: int | None = None
    try:
        if proc is not None and proc.poll() is None:
            if started_monotonic is not None:
                remaining = WINDOW_CAPTURE_MINIMUM_SECONDS - (time.monotonic() - started_monotonic)
                if remaining > 0:
                    time.sleep(remaining)
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            try:
                exit_code = proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    exit_code = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    exit_code = proc.wait(timeout=5)
        elif proc is not None:
            exit_code = proc.poll()
    finally:
        if watchdog is not None:
            watchdog.cancel()
        _stop_cursor_guard()
        if handle is not None:
            handle.close()
        _ACTIVE_WINDOW_CAPTURE = None
        _ACTIVE_WINDOW_CAPTURE_LOG = None
        _ACTIVE_WINDOW_CAPTURE_WATCHDOG = None
        _ACTIVE_WINDOW_CAPTURE_STARTED = None

    if not raw.is_file() or raw.stat().st_size == 0:
        raise SologsbError(f"窗口录屏原始文件为空: {raw}")
    transcode = run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(raw),
            "-vf",
            f"{VIDEO_SCALE_FILTER},fps=30",
            "-an",
            *INTERMEDIATE_X264_ARGS,
            "-movflags",
            "+faststart",
            str(cropped),
        ],
        check=False,
    )
    if transcode.returncode != 0:
        raise SologsbError(
            transcode.stderr.decode("utf-8", errors="replace") or "窗口视频转码失败"
        )
    visual_report: dict[str, Any] = {"required": require_visible_content, "status": "skipped", "ok": True}
    if require_visible_content:
        visual_report = _terminal_visual_content_metrics(cropped)
        write_json(
            cropped.with_name(f"{cropped.stem}-visual-content.json"),
            visual_report,
        )
        if not visual_report.get("ok"):
            raise SologsbError(
                "终端录屏画面门禁未通过: "
                + str(visual_report.get("error") or "没有可辨识内容")
            )
    metadata_path = raw.with_name(f"{raw.stem}-window-capture.json")
    metadata = read_json(metadata_path, {}) or {}
    backend_ok = (
        metadata.get("captureBackend") == "screen-capture-kit"
        and metadata.get("showsCursor") is False
        and metadata.get("cursorCaptured") is False
    )
    metadata.update(
        {
            "status": "ok" if exit_code in (0, None) and backend_ok else "failed",
            "exitCode": exit_code,
            "finishedAt": utc_now(),
            "outputPath": str(cropped.resolve()),
            "pidFile": str(pid_file.resolve()),
            "visualContent": visual_report,
        }
    )
    write_json(metadata_path, metadata)


def _wait_url(url: str, timeout: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def _window_info_for_pids(
    pids: list[int] | set[int],
    *,
    timeout: float = 10.0,
    label: str = "Chrome",
) -> dict[str, Any]:
    expected = {int(pid) for pid in pids if int(pid) > 0}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates: list[dict[str, Any]] = []
        for window in _window_list():
            if int(window.get("kCGWindowOwnerPID") or 0) not in expected:
                continue
            if int(window.get("kCGWindowLayer") or 0) != 0:
                continue
            payload = _window_info_payload(window)
            if payload:
                candidates.append(payload)
        if candidates:
            selected = max(
                candidates,
                key=lambda item: (
                    _bounds_rect(str(item["bounds"])) or (0.0, 0.0, 0.0, 0.0)
                )[2]
                * (_bounds_rect(str(item["bounds"])) or (0.0, 0.0, 0.0, 0.0))[3],
            )
            return _require_window_id(selected, label)
        time.sleep(0.25)
    raise SologsbError(f"无法定位{label}窗口ID，停止录制: pids={sorted(expected)}")


def _concat_segments(parts: list[Path], output: Path) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for part in parts:
        cmd += ["-i", str(part)]
    filters = []
    for index in range(len(parts)):
        filters.append(f"[{index}:v]{VIDEO_SCALE_FILTER},fps=30[v{index}]")
    concat = "".join(f"[v{index}]" for index in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[out]"
    proc = run([*cmd, "-filter_complex", ";".join([*filters, concat]), "-map", "[out]", *INTERMEDIATE_X264_ARGS, "-movflags", "+faststart", str(output)], check=False)
    if proc.returncode != 0:
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "视频片段拼接失败")


def _run_recording_preflight(
    task_root: Path,
    side: str,
    plan: dict[str, Any],
    *,
    key: str = "preflightCommands",
    stage: str = "preflight",
) -> list[dict[str, Any]]:
    """Run non-recorded preparation commands so the recorded segment starts fast.

    ``buildCommands`` (dependency install, compile, image build) touch only the
    task's own directory and run before the host-wide recording lock, so other
    tasks are not queued behind them.  ``preflightCommands`` may start services,
    bind ports or reset shared data, and therefore run inside the lock.
    """
    commands = plan.get(key) or []
    if isinstance(commands, str):
        commands = [commands]
    commands = [str(command).strip() for command in commands if str(command).strip()]
    if not commands:
        return []
    project_dir = Path(str(plan.get("projectDir") or "")).expanduser().resolve()
    if not project_dir.is_dir():
        raise SologsbError(f"录制项目目录不存在: {project_dir}")
    output_dir = task_root / "monitor" / "recording" / side.lower() / stage
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, command in enumerate(commands, 1):
        started = time.monotonic()
        timeout = float(plan.get("preflightTimeoutSeconds") or 1200)
        proc = run(["/bin/bash", "-lc", command], cwd=project_dir, check=False, timeout=timeout)
        output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
        log_path = output_dir / f"{index:02d}.log"
        log_path.write_text(output, encoding="utf-8")
        result = {
            "name": f"{stage}-{index}",
            "command": command,
            "exitCode": proc.returncode,
            "durationSeconds": round(time.monotonic() - started, 3),
            "logPath": str(log_path.resolve()),
            "ok": proc.returncode == 0,
        }
        results.append(result)
        if proc.returncode != 0:
            raise SologsbError(
                f"录制{'构建' if stage == 'build' else '预检'}失败，退出码 {proc.returncode}: {command}\n{output[-3000:]}"
            )
    write_json(output_dir / "result.json", {"side": side, "ok": True, "checks": results})
    return results


def recording_output_name(task_root: Path, side: str) -> str:
    """Return the required project-based recording filename."""
    side = side.upper()
    if side not in {"A", "B"}:
        raise SologsbError(f"未知 side: {side}")
    state = read_json(task_root / "monitor" / "state.json", {})
    source = state.get("source") or {}
    selection_doc = read_json(task_root / "monitor" / "platform-selection.json", {})
    selection = selection_doc.get("selection") if isinstance(selection_doc, dict) else {}
    selection = selection if isinstance(selection, dict) else {}
    project_code = str(source.get("projectCode") or selection.get("projectCode") or "").strip()
    project_name = str(source.get("projectName") or selection.get("projectName") or "").strip()
    if not project_name:
        project_name = str(state.get("taskName") or task_root.name)
    prefix = "-".join(part for part in (project_code, project_name) if part)
    return f"{safe_local_name(prefix)}-验证{side}产物.mp4"


def _dev_port(script: str, default: int) -> int:
    for pattern in (r"--port(?:=|\s+)(\d+)", r"(?:^|\s)-p\s*(\d+)"):
        match = re.search(pattern, script)
        if match:
            return int(match.group(1))
    return default


def _package_preflight(repo: Path, package_manager: str, scripts: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    if package_manager == "pnpm":
        # pnpm 11 can abort a no-TTY module purge and may require explicit dependency
        # build approval. The fallback resolves both cases without hiding the first error log.
        commands.append(
            "CI=true pnpm install --frozen-lockfile || "
            "(pnpm approve-builds --all && CI=true pnpm install --frozen-lockfile)"
        )
    elif package_manager == "npm" and (repo / "package-lock.json").is_file():
        commands.append("CI=true npm ci")
    if "build" in scripts:
        commands.append("pnpm build" if package_manager == "pnpm" else "npm run build")
    return commands


FRONTEND_PACKAGE_MARKERS = {
    "react", "react-dom", "vue", "@vue/runtime-core", "svelte", "next", "nuxt",
    "@angular/core", "solid-js", "vite", "webpack", "parcel", "@remix-run/react", "astro",
}
BACKEND_PACKAGE_MARKERS = {
    "express", "fastify", "koa", "@nestjs/core", "@nestjs/common", "hapi", "@hapi/hapi",
    "restify", "hono",
}
BACKEND_FILE_MARKERS = (
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "Cargo.toml", "requirements.txt", "pyproject.toml", "manage.py",
    "composer.json", "Gemfile", "mix.exs",
)
FRONTEND_FILE_MARKERS = (
    "index.html", "vite.config.js", "vite.config.ts", "next.config.js", "next.config.mjs",
    "nuxt.config.ts", "nuxt.config.js", "vue.config.js", "svelte.config.js", "angular.json",
)
BACKEND_ENTRY_MARKERS = (
    "server.js", "server.ts", "app.js", "app.ts",
    "src/server.js", "src/server.ts", "src/app.js", "src/app.ts",
)


def _is_backend_project(repo: Path, package: dict[str, Any]) -> bool:
    """Best-effort detection so pure backend services use terminal API recording."""
    dependencies: set[str] = set()
    for group in ("dependencies", "devDependencies", "peerDependencies"):
        values = package.get(group)
        if isinstance(values, dict):
            dependencies.update(str(name) for name in values)
    has_frontend = bool(dependencies & FRONTEND_PACKAGE_MARKERS)
    has_backend = bool(dependencies & BACKEND_PACKAGE_MARKERS)
    if has_backend and not has_frontend:
        return True
    if has_backend or has_frontend:
        return False
    if any((repo / marker).is_file() for marker in BACKEND_FILE_MARKERS):
        return True
    if any((repo / marker).is_file() for marker in FRONTEND_FILE_MARKERS):
        return False
    return any((repo / marker).is_file() for marker in BACKEND_ENTRY_MARKERS)


def _recording_service_ports(plan: dict[str, Any]) -> list[int]:
    urls: list[str] = []
    for key in ("appUrl", "apiBaseUrl"):
        value = str(plan.get(key) or "").strip()
        if value:
            urls.append(value)
    for request in plan.get("apiRequests") or []:
        if isinstance(request, dict):
            value = str(request.get("url") or "").strip()
            if value:
                urls.append(value)
    ports: set[int] = set()
    for value in urls:
        try:
            parsed = urlparse(value)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
                ports.add(int(parsed.port))
        except (TypeError, ValueError):
            continue
    for command in [str(plan.get("startCommand") or ""), *[str(item) for item in plan.get("commands") or []]]:
        ports.update(int(value) for value in re.findall(r"--port(?:=|\s+)(\d+)", command))
        ports.update(int(value) for value in re.findall(r"(?:^|\s)-p\s*(\d+)", command))
    return sorted(port for port in ports if 0 < port < 65536)


def _process_command(pid: int) -> str:
    proc = run(["ps", "-p", str(pid), "-o", "command="], check=False)
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _all_process_ids() -> set[int]:
    proc = run(["ps", "-axo", "pid="], check=False)
    result: set[int] = set()
    for value in proc.stdout.decode("utf-8", errors="replace").split():
        if value.isdigit():
            result.add(int(value))
    return result


def _listener_rows(ports: list[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for port in ports:
        proc = run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
        )
        for value in proc.stdout.decode("utf-8", errors="replace").splitlines():
            if not value.strip().isdigit():
                continue
            pid = int(value.strip())
            key = (port, pid)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"port": port, "pid": pid, "command": _process_command(pid)})
    return rows


def _cleanup_recording_services(
    *,
    plan: dict[str, Any],
    baseline_process_ids: set[int],
    baseline_listeners: list[dict[str, Any]],
    report_path: Path,
) -> dict[str, Any]:
    """Run planned cleanup and terminate only app-port listeners started by this recording."""
    project_dir = Path(str(plan.get("projectDir") or ".")).resolve()
    commands = [str(value) for value in plan.get("cleanupCommands") or [] if str(value).strip()]
    command_results: list[dict[str, Any]] = []
    for command in commands:
        proc = run(["/bin/zsh", "-lc", command], cwd=project_dir, check=False, timeout=60)
        command_results.append(
            {
                "command": command,
                "exitCode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", errors="replace")[-4000:],
                "stderr": proc.stderr.decode("utf-8", errors="replace")[-4000:],
            }
        )
    ports = _recording_service_ports(plan)
    time.sleep(0.4)
    treated: set[tuple[int, int]] = set()
    terminated: list[dict[str, Any]] = []
    for _ in range(3):
        new_rows = [
            row
            for row in _listener_rows(ports)
            if int(row["pid"]) not in baseline_process_ids
            and (int(row["port"]), int(row["pid"])) not in treated
        ]
        if not new_rows:
            break
        for row in new_rows:
            pid = int(row["pid"])
            treated.add((int(row["port"]), pid))
            try:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.15)
                if pid in _all_process_ids():
                    os.kill(pid, signal.SIGKILL)
                row["status"] = "terminated"
            except ProcessLookupError:
                row["status"] = "already-exited"
            except PermissionError as exc:
                row["status"] = "permission-denied"
                row["error"] = str(exc)
            terminated.append(row)
        time.sleep(0.35)
    residual = [
        row
        for row in _listener_rows(ports)
        if int(row["pid"]) not in baseline_process_ids
    ]
    pre_existing = [
        row
        for row in baseline_listeners
        if int(row["pid"]) in baseline_process_ids
    ]
    payload = {
        "status": "ok" if not residual else "failed",
        "path": str(report_path.resolve()),
        "ports": ports,
        "cleanupCommands": command_results,
        "baselineListeners": pre_existing,
        "terminatedAppPortListeners": terminated,
        "residualAppPortListeners": residual,
        "finishedAt": utc_now(),
    }
    write_json(report_path, payload)
    return payload


def _remove_chrome_profile(profile: Path, report_path: Path) -> dict[str, Any]:
    keep = os.environ.get("SOLOSGB_0917_KEEP_CHROME_PROFILE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    payload: dict[str, Any] = {
        "status": "skipped" if keep else "removed",
        "path": str(profile),
        "keptByEnvironment": keep,
        "removed": False,
    }
    if not keep and profile.exists():
        for attempt in range(3):
            try:
                shutil.rmtree(profile)
                payload["removed"] = True
                payload["status"] = "removed"
                break
            except Exception as exc:
                payload["error"] = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(0.4)
        if not payload["removed"]:
            payload["status"] = "failed"
    write_json(report_path, payload)
    return payload


def default_plan(task_root: Path, side: str) -> dict[str, Any]:
    state = read_json(task_root / "monitor" / "state.json", {})
    side_state = (state.get("sides") or {}).get(side.upper()) or {}
    configured = str(side_state.get("workspacePath") or "")
    candidate = str(side_state.get("candidateId") or "")
    if configured:
        repo = Path(configured)
    elif candidate:
        repo = task_root / "source" / "candidates" / candidate
    else:
        repo = task_root / "source" / side.lower()
    package = read_json(repo / "package.json", {})
    scripts = package.get("scripts") or {}
    package_manager = "pnpm" if (repo / "pnpm-lock.yaml").is_file() else "npm"
    preflight = _package_preflight(repo, package_manager, scripts)
    if (repo / "docker-compose.yml").is_file():
        preflight = ["docker compose config --quiet", "docker compose build", *preflight]
    scenario = str((task_root / "workspace" / "视频信息" / side.lower() / "脚本" / "scenario.cjs").resolve())
    backend_project = _is_backend_project(repo, package)
    if "dev" in scripts and not backend_project:
        return {
            "mode": "web",
            "projectDir": str(repo.resolve()),
            "startCommand": "pnpm dev" if package_manager == "pnpm" else "npm run dev",
            "appUrl": f"http://127.0.0.1:{_dev_port(str(scripts.get('dev') or ''), 5173)}",
            "scenarioScript": scenario,
            "terminalApp": TERMINAL_APP,
            "pace": 1.8,
            "targetApps": ["Terminal", "Chrome"],
            "captureKind": "window-id",
            "pointerStrategy": "none",
            "buildCommands": preflight,
            "preflightCommands": [],
            "outputName": recording_output_name(task_root, side),
        }
    if "start" in scripts and not backend_project:
        return {
            "mode": "web",
            "projectDir": str(repo.resolve()),
            "startCommand": "pnpm start" if package_manager == "pnpm" else "npm start",
            "appUrl": "http://127.0.0.1:3000",
            "scenarioScript": scenario,
            "terminalApp": TERMINAL_APP,
            "pace": 1.8,
            "targetApps": ["Terminal", "Chrome"],
            "captureKind": "window-id",
            "pointerStrategy": "none",
            "buildCommands": preflight,
            "preflightCommands": [],
            "outputName": recording_output_name(task_root, side),
        }
    terminal_start = "<替换为真实启动或验证命令>"
    terminal_api_base = ""
    if backend_project and "dev" in scripts:
        terminal_start = "pnpm dev" if package_manager == "pnpm" else "npm run dev"
        terminal_api_base = f"http://127.0.0.1:{_dev_port(str(scripts.get('dev') or ''), 3000)}"
    elif backend_project and "start" in scripts:
        terminal_start = "pnpm start" if package_manager == "pnpm" else "npm start"
        terminal_api_base = f"http://127.0.0.1:{_dev_port(str(scripts.get('start') or ''), 3000)}"
    return {
        "mode": "terminal",
        "projectDir": str(repo.resolve()),
        "startCommand": terminal_start,
        "apiBaseUrl": terminal_api_base,
        "commands": [],
        "cleanupCommands": [],
        "apiRequests": [],
        "requiresApiRequests": backend_project,
        "holdSeconds": 12,
        "terminalApp": TERMINAL_APP,
        "expectedFailure": False,
        "targetApps": ["Terminal"],
        "captureKind": "window-id",
        "pointerStrategy": "none",
        "outputName": recording_output_name(task_root, side),
    }


def prepare_recording(task_root: Path, side: str) -> Path:
    folder = task_root / "workspace" / "视频信息" / side.lower() / "脚本"
    folder.mkdir(parents=True, exist_ok=True)
    plan_path = folder / "record-plan.json"
    if not plan_path.is_file():
        write_json(plan_path, default_plan(task_root, side))
    scenario = folder / "scenario.cjs"
    if not scenario.is_file():
        scenario.write_text(
            "module.exports = async ({ goto, wait, humanClick, page }) => {\n"
            "  await goto('/');\n"
            "  await wait(1500);\n"
            "  // 先确认当前用户、默认 Tab 和种子数据是否包含目标记录；需要时先切换 Tab。\n"
            "  // 多卡片页面不要默认取 first；优先按卡片范围定位，或用 last() 命中当前可见记录。\n"
            "  // 优先等待稳定的业务文案，不只等颜色或状态徽标。\n"
            "  // 根据本题提示词和真实产物补充关键验收路径。\n"
            "};\n",
            encoding="utf-8",
        )
    return plan_path


def video_dimensions(path: Path) -> tuple[int, int]:
    proc = run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path),
        ],
        check=False,
    )
    if proc.returncode != 0:
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "ffprobe 获取分辨率失败")
    value = proc.stdout.decode().strip()
    try:
        width, height = (int(part) for part in value.split("x", 1))
    except Exception as exc:
        raise SologsbError(f"无法解析视频分辨率: {value}") from exc
    return width, height


def _duration_seconds(path: Path) -> float:
    proc = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=False,
    )
    if proc.returncode != 0:
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "ffprobe 失败")
    try:
        return float(proc.stdout.decode().strip())
    except ValueError as exc:
        raise SologsbError("ffprobe 没有返回时长") from exc


def recording_command_ok(command_exit: int | None, expected_app_failure: bool) -> bool:
    """Return whether a finished recording matches the declared failure policy.

    A generated video is not by itself a successful recording.  An unexpected
    non-zero browser/API exit must keep ``ok=false`` so the task cannot advance
    to ``recorded`` until the scenario or application is fixed.
    """
    observed_failure = command_exit not in (None, 0)
    if expected_app_failure and not observed_failure:
        raise SologsbError("计划要求录制真实失败，但应用命令以 0 退出")
    return observed_failure == expected_app_failure


def recording_isolation_ok(
    *,
    mode: str,
    window_capture_reports: list[dict[str, Any]],
    guard_reports: list[dict[str, Any]],
    frontmost_report: dict[str, Any] | None = None,
    service_cleanup: dict[str, Any] | None = None,
) -> bool:
    """Require every window-capture isolation report to pass the hard gate."""
    def valid_capture(item: dict[str, Any]) -> bool:
        try:
            window_id = int(item.get("windowId") or 0)
            owner_pid = int(item.get("ownerPid") or 0)
        except (TypeError, ValueError):
            return False
        return (
            item.get("status") == "ok"
            and item.get("captureKind") == "window-id"
            and item.get("captureBackend") == "screen-capture-kit"
            and item.get("showsCursor") is False
            and item.get("cursorCaptured") is False
            and window_id > 0
            and owner_pid > 0
        )

    window_capture_ok = bool(window_capture_reports) and all(
        valid_capture(item) for item in window_capture_reports
    )
    terminal_visual_ok = True
    if mode in {"terminal", "failed-start"}:
        for item in window_capture_reports:
            report: dict[str, Any] = {}
            report_path = str(item.get("path") or "").strip()
            if report_path:
                report = read_json(Path(report_path), {}) or {}
            visual = item.get("visualContent") or report.get("visualContent")
            output_path = str(report.get("outputPath") or item.get("outputPath") or "").strip()
            if not isinstance(visual, dict) and output_path and Path(output_path).is_file():
                visual = _terminal_visual_content_metrics(Path(output_path))
            if not isinstance(visual, dict) or visual.get("ok") is not True:
                terminal_visual_ok = False
                break
    owners = {
        str(item.get("ownerApp") or "").strip()
        or _canonical_owner(str(item.get("ownerName") or ""), str(item.get("ownerBundleId") or ""))
        for item in window_capture_reports
    }
    expected_owners = {"Terminal", "Chrome"} if mode == "web" else {"Terminal"}
    targets_ok = owners == expected_owners

    def valid_guard(item: dict[str, Any]) -> bool:
        return (
            item.get("status") == "ok"
            and item.get("pointerStrategy") == "none"
            and item.get("pointerPolicy") == "host-input-untouched"
            and item.get("hostInputRespected") is True
            and item.get("pointerMoved") is False
            and item.get("mouseButtonsQueried") is False
            and item.get("parkApplied") is False
        )

    cursor_ok = bool(guard_reports) and all(valid_guard(item) for item in guard_reports)
    frontmost_ok = True
    if frontmost_report is not None:
        frontmost_ok = (
            frontmost_report.get("status") == "ok"
            and int(frontmost_report.get("sampleCount") or 0) > 0
            and int(frontmost_report.get("recordingWindowFrontmostSamples") or 0) == 0
        )
    service_ok = True
    if service_cleanup is not None:
        service_ok = (
            service_cleanup.get("status") == "ok"
            and not (service_cleanup.get("residualAppPortListeners") or [])
        )
    return (
        targets_ok
        and window_capture_ok
        and terminal_visual_ok
        and cursor_ok
        and frontmost_ok
        and service_ok
    )


def _copy_final(source: Path, task_root: Path, side: str, output_name: str) -> Path:
    target = task_root / "workspace" / "视频信息" / side.lower() / "视频" / output_name
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.stem}.720p{target.suffix}")
    temp.unlink(missing_ok=True)
    proc = run(
        [
            "ffmpeg", "-y", "-v", "error", "-i", str(source),
            "-vf", VIDEO_SCALE_FILTER,
            "-r", "30", "-an", *FINAL_X264_ARGS, "-movflags", "+faststart", str(temp),
        ],
        check=False,
    )
    if proc.returncode != 0:
        temp.unlink(missing_ok=True)
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "视频转 720p 失败")
    os.replace(temp, target)
    width, height = video_dimensions(target)
    if (width, height) != (1280, 720):
        target.unlink(missing_ok=True)
        raise SologsbError(f"{side} 视频不是 1280x720，当前为 {width}x{height}")
    duration = _duration_seconds(target)
    if duration > 90.5:
        target.unlink(missing_ok=True)
        raise SologsbError(f"{side} 视频 {duration:.1f} 秒，超过 90 秒；必须重写脚本，不允许静默裁剪")
    if target.stat().st_size > 500 * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise SologsbError(f"{side} 视频超过 500MB")
    return target


def _playwright_node_path(output_dir: Path) -> str:
    existing = os.environ.get("NODE_PATH", "").strip()
    if existing:
        return existing
    candidates: list[Path] = []
    for package in Path.home().glob(".npm/_npx/**/node_modules/playwright/package.json"):
        candidates.append(package.parent.parent)
    if candidates:
        return str(sorted(candidates)[0])
    tools = output_dir / ".tools"
    proc = run(["npm", "install", "--prefix", str(tools), "playwright@1.63.0"], check=False, timeout=900)
    if proc.returncode != 0:
        raise SologsbError(proc.stderr.decode("utf-8", errors="replace") or "Playwright 安装失败")
    return str(tools / "node_modules")


CHROME_FLAGS = [
    "--no-first-run", "--no-default-browser-check", "--lang=en-US", "--accept-lang=en-US,en", "--disable-translate", "--disable-component-extensions-with-background-pages",
    "--disable-save-password-bubble", "--password-store=basic",
    "--disable-sync", "--disable-background-networking", "--disable-component-update",
    "--disable-default-apps", "--disable-extensions", "--disable-search-engine-choice-screen",
    "--hide-crash-restore-bubble", "--no-service-autorun", "--no-report-upload",
    "--disable-features=Translate,TranslateUI,PasswordManager,PasswordManagerOnboarding,PasswordManagerEnableAccountStore,PasswordManagerRedesign,PasswordGeneration,PasswordLeakDetection,PasswordStrengthIndicator,PasswordManagerEnableBiometricAuthentication,AutofillServerCommunication,AutofillEnableAccountWalletStorage,AutofillEnablePayments,AutofillEnableOfferToSaveCard,AutofillEnableSyncingAutofill,AutofillEnableVirtualCard,AutofillEnableCardBenefits,AutofillEnableCardArtImage,AutofillUpstream,AutofillEnablePaymentsMandatoryReauth,AutofillEnableSaveCardBubble,AutofillSaveCardBubble,AutofillEnableWalletMetadataPayment,AutofillDisableAddressSaving,EnablePasswordsAccountStorage,SafeBrowsingEnhancedProtection,SigninPromo,DiceWebSigninInterception,ChromeSignin,AccountConsistency,ProfileMenuRevamp,ProfileCustomization,ProfilePickerOnStartup",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]

# Creates the recording window through CDP with background:true so Chrome never
# becomes the frontmost app (a startup window or `open` would activate it).
CHROME_BACKGROUND_WINDOW_JS = """
const [wsUrl, left, top, width, height] = process.argv.slice(1);
const ws = new WebSocket(wsUrl);
const timer = setTimeout(() => { console.error('CDP 超时'); process.exit(2); }, 15000);
ws.onopen = () => ws.send(JSON.stringify({id: 1, method: 'Target.createTarget', params: {url: 'about:blank', newWindow: true, background: true, left: +left, top: +top, width: +width, height: +height}}));
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id !== 1) return;
  clearTimeout(timer);
  if (msg.error) { console.error(JSON.stringify(msg.error)); process.exit(1); }
  console.log(msg.result.targetId);
  ws.close();
  process.exit(0);
};
ws.onerror = (e) => { console.error(String(e.message || e)); process.exit(1); };
"""


def _chrome_command(profile: Path, port: int) -> list[str]:
    """Run the Chrome binary directly: no LaunchServices activation, no startup window."""
    return [
        str(CHROME_BINARY),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        *CHROME_FLAGS,
        "--no-startup-window",
    ]


def _chrome_open_background_window(ws_url: str) -> str:
    if not ws_url:
        raise SologsbError("Chrome CDP 没有返回 webSocketDebuggerUrl")
    left, top, width, height = CHROME_WINDOW_BOUNDS
    proc = run(
        ["node", "-e", CHROME_BACKGROUND_WINDOW_JS, ws_url, str(left), str(top), str(width), str(height)],
        check=False,
        timeout=30,
    )
    if proc.returncode != 0:
        raise SologsbError(
            "无法在后台创建 Chrome 窗口: "
            + (proc.stderr.decode("utf-8", errors="replace").strip() or str(proc.returncode))
        )
    return proc.stdout.decode("utf-8", errors="replace").strip()


def _record_chrome_segment(
    *,
    output_dir: Path,
    app_url: str,
    scenario: Path,
    pace: float,
    pointer_strategy: str,
    focus_guard: _FocusRestoreGuard,
    frontmost_monitor: _FrontmostWindowMonitor,
) -> tuple[Path, int]:
    if not CHROME_BINARY.is_file():
        raise SologsbError(f"缺少 Google Chrome: {CHROME_BINARY}")
    raw = output_dir / "browser-screen.mov"
    cropped = output_dir / "browser-cropped.mp4"
    pid_file = output_dir / "browser.pid"
    profile = output_dir / "chrome-profile"
    if profile.exists():
        shutil.rmtree(profile)
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Preferences").write_text(
        json.dumps(
            {
                "credentials_enable_service": False,
                "credentials_enable_autosignin": False,
                "signin": {
                    "allowed": False,
                    "allowed_on_next_startup": False,
                },
                "sync": {"requested": False},
                "browser": {
                    "show_home_button": False,
                    "show_bookmark_bar": False,
                },
                "translate": {"enabled": False},
                "intl": {"accept_languages": "en-US,en"},
                "profile": {
                    "password_manager_enabled": False,
                    "password_manager_leak_detection": False,
                    "exit_type": "Normal",
                    "default_content_setting_values": {
                        "notifications": 2,
                        "geolocation": 2,
                        "media_stream_camera": 2,
                        "media_stream_mic": 2,
                    },
                },
                "autofill": {
                    "profile_enabled": False,
                    "credit_card_enabled": False,
                    "address_enabled": False,
                    "credit_card_fido_authentication": False,
                },
                "payments": {
                    "can_make_payment_enabled": False,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    port = random.randrange(9300, 9900)
    cdp_url = f"http://127.0.0.1:{port}"
    chrome_pids: list[int] = []
    chrome_proc: subprocess.Popen[bytes] | None = None
    chrome_log = (output_dir / "chrome.log").open("wb")
    try:
        chrome_proc = subprocess.Popen(
            _chrome_command(profile, port),
            stdin=subprocess.DEVNULL,
            stdout=chrome_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        version: dict[str, Any] = {}
        while time.monotonic() < deadline:
            if chrome_proc.poll() is not None:
                raise SologsbError(f"Chrome 提前退出({chrome_proc.returncode})，详见 {output_dir / 'chrome.log'}")
            try:
                with urlopen(cdp_url + "/json/version", timeout=2) as response:
                    version = json.loads(response.read().decode("utf-8"))
                    break
            except Exception:
                time.sleep(0.25)
        else:
            raise SologsbError("Chrome CDP 未启动")
        chrome_pids = [chrome_proc.pid]
        _chrome_open_background_window(str(version.get("webSocketDebuggerUrl") or ""))
        window_info = _window_info_for_pids(chrome_pids, label="Chrome")
        focus_guard.restore_if_recording_frontmost(
            set(chrome_pids),
            "chrome-open",
            {int(window_info.get("windowId") or 0)},
        )
        node_path = _playwright_node_path(output_dir)
        _start_window_segment(
            output=raw,
            pid_file=pid_file,
            window_info=window_info,
            pointer_strategy=pointer_strategy,
            frontmost_monitor=frontmost_monitor,
        )
        try:
            env = os.environ.copy()
            env.update(
                {
                    "CDP_URL": cdp_url,
                    "BASE_URL": app_url,
                    "PACE": str(pace),
                    "NODE_PATH": node_path,
                    "HUMAN_BROWSER_KEEP_FRONT": "0",
                }
            )
            proc = run(
                ["node", str(BROWSER_DRIVER), "--scenario", str(scenario), "--output", str(output_dir)],
                env=env,
                check=False,
                timeout=180,
            )
            time.sleep(2)
        finally:
            _stop_window_segment(
                raw=raw,
                cropped=cropped,
                window_info=window_info,
                pid_file=pid_file,
            )
        return cropped, proc.returncode
    finally:
        if chrome_proc is not None and chrome_proc.poll() is None:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome_proc.kill()
        run(["pkill", "-f", str(profile)], check=False)
        chrome_log.close()
        time.sleep(0.5)
        _remove_chrome_profile(profile, output_dir / "chrome-profile-cleanup.json")


def _run_web_terminal(
    task_root: Path,
    side: str,
    plan: dict[str, Any],
    *,
    pointer_strategy: str,
    focus_guard: _FocusRestoreGuard,
    frontmost_monitor: _FrontmostWindowMonitor,
) -> tuple[Path, int | None]:
    scenario = Path(str(plan.get("scenarioScript") or ""))
    if not scenario.is_file():
        raise SologsbError(f"Web scenario 不存在: {scenario}")
    output_dir = task_root / "monitor" / "recording" / side.lower() / WEB_RUNTIME_DIR
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    terminal_raw = output_dir / "terminal-screen.mov"
    terminal_cropped = output_dir / "terminal-cropped.mp4"
    terminal_pid = output_dir / "terminal.pid"
    title = f"sologsb-{side.lower()}-{random.randrange(0x100000):05x}"
    window_id = 0
    tty = ""
    browser_exit = 0
    combined: Path | None = None
    body_error: BaseException | None = None
    capture_error: Exception | None = None
    try:
        window_id, tty = _terminal_open_window(title, Path(str(plan["projectDir"])).resolve())
        window_info = _window_info_by_id(window_id, label="Terminal")
        focus_guard.restore_if_recording_frontmost(
            {int(window_info.get("ownerPid") or 0)},
            "terminal-open",
            {window_id},
        )
        precommands = [str(command) for command in plan.get("preCommands") or [] if str(command).strip()]
        for command in precommands:
            _terminal_send(window_id, command)
            time.sleep(0.3)
        if precommands:
            # preCommands run off camera; wipe their output before capture starts.
            _terminal_prepare_clean(window_id)
        _start_window_segment(
            output=terminal_raw,
            pid_file=terminal_pid,
            window_info=window_info,
            pointer_strategy=pointer_strategy,
            frontmost_monitor=frontmost_monitor,
        )
        try:
            _terminal_send(window_id, str(plan["startCommand"]))
            if not _wait_url(str(plan["appUrl"]), timeout=float(plan.get("startTimeoutSeconds") or 45)):
                raise SologsbError(f"项目未在时限内可访问: {plan['appUrl']}")
            time.sleep(float(plan.get("terminalHoldSeconds") or 2))
        finally:
            _stop_window_segment(
                raw=terminal_raw,
                cropped=terminal_cropped,
                window_info=window_info,
                pid_file=terminal_pid,
            )
        browser_cropped, browser_exit = _record_chrome_segment(
            output_dir=output_dir,
            app_url=str(plan["appUrl"]),
            scenario=scenario,
            pace=float(plan.get("pace") or 1.4),
            pointer_strategy=pointer_strategy,
            focus_guard=focus_guard,
            frontmost_monitor=frontmost_monitor,
        )
        combined = output_dir / "combined.mp4"
        _concat_segments([terminal_cropped, browser_cropped], combined)
    except BaseException as exc:
        body_error = exc
    finally:
        if window_id:
            try:
                _capture_terminal_text(window_id, output_dir / "terminal.log")
            except Exception as exc:
                capture_error = exc
            _terminal_close_window(window_id, tty)
    if body_error is not None:
        raise body_error
    if capture_error is not None:
        raise capture_error
    if combined is None:
        raise SologsbError("Web 录屏没有生成合成视频")
    return _copy_final(combined, task_root, side, str(plan.get("outputName") or "demo.mp4")), browser_exit


API_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
API_VAR_PATTERN = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
API_CALL_FUNCTION = r"""
sologsb_api_check() {
  local name="$1" expected="$2" contains_json="$3" extract_name="$4" extract_path="$5"
  local method="$6" url="$7" body_text="$8"
  shift 8
  local headers_file body_file status value curl_rc
  headers_file="$(mktemp)"
  body_file="$(mktemp)"
  echo ""
  echo "===== API: ${name} ====="
  echo "请求: ${method} ${url}"
  if [ -n "$body_text" ]; then
    echo "请求体: ${body_text}"
  fi
  status="$(curl -sS -D "$headers_file" -o "$body_file" -w '%{http_code}' -X "$method" "$url" "$@")"
  curl_rc=$?
  if [ "$curl_rc" -ne 0 ]; then
    echo "API_CHECK_FAILED: curl 退出码 ${curl_rc}"
    rm -f "$headers_file" "$body_file"
    return 1
  fi
  echo "响应状态: ${status}"
  echo "响应正文:"
  cat "$body_file"
  echo ""
  if [ -n "$expected" ]; then
    case "|$expected|" in
      *"|$status|"*) : ;;
      *)
        echo "API_CHECK_FAILED: 期望状态 ${expected}，实际 ${status}"
        rm -f "$headers_file" "$body_file"
        return 1
        ;;
    esac
  fi
  if [ -n "$contains_json" ] && [ "$contains_json" != "[]" ]; then
    if ! python3 - "$body_file" "$contains_json" <<'PY'
import json
import sys
body = open(sys.argv[1], encoding="utf-8", errors="replace").read()
needles = json.loads(sys.argv[2])
missing = [str(item) for item in needles if str(item) not in body]
if missing:
    print("缺少关键内容: " + ", ".join(missing))
    raise SystemExit(1)
PY
    then
      echo "API_CHECK_FAILED: 响应缺少预期内容"
      rm -f "$headers_file" "$body_file"
      return 1
    fi
  fi
  if [ -n "$extract_name" ]; then
    value="$(python3 - "$body_file" "$extract_path" <<'PY'
import json
import re
import sys
body_path, path = sys.argv[1], sys.argv[2]
try:
    with open(body_path, encoding="utf-8") as handle:
        current = json.load(handle)
except Exception as exc:
    print(f"响应不是有效 JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)
for token in [part for part in re.split(r"[.\[\]]+", path) if part and part != "$"]:
    if isinstance(current, list):
        try:
            current = current[int(token)]
        except (IndexError, ValueError):
            raise SystemExit(1)
    elif isinstance(current, dict):
        if token not in current:
            raise SystemExit(1)
        current = current[token]
    else:
        raise SystemExit(1)
if isinstance(current, (dict, list)):
    print(json.dumps(current, ensure_ascii=False, separators=(",", ":")))
else:
    print("" if current is None else current)
PY
)" || {
      echo "API_CHECK_FAILED: 提取字段失败 ${extract_path}"
      rm -f "$headers_file" "$body_file"
      return 1
    }
    if [ -z "$value" ]; then
      echo "API_CHECK_FAILED: 提取字段为空 ${extract_path}"
      rm -f "$headers_file" "$body_file"
      return 1
    fi
    export "$extract_name=$value"
    echo "已提取变量: ${extract_name}"
  fi
  echo "结果: PASS"
  rm -f "$headers_file" "$body_file"
  return 0
}
""".strip()


def _shell_double_quote(value: Any) -> str:
    """Quote a bash double-quoted argument and expand {{var}} placeholders at runtime."""
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    escaped = API_VAR_PATTERN.sub(lambda match: "${" + match.group(1) + "}", escaped)
    return f'"{escaped}"'


def _normalize_expected_status(value: Any) -> str:
    if value is None or value == "" or value == []:
        return ""
    values = value if isinstance(value, list) else [value]
    statuses: list[str] = []
    for item in values:
        text = str(item).strip()
        if not text.isdigit():
            raise SologsbError(f"apiRequests.expectedStatus 必须是 HTTP 状态码数字，当前为: {item!r}")
        statuses.append(text)
    return "|".join(dict.fromkeys(statuses))


def _normalize_contains(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        raise SologsbError("apiRequests.expectContains 必须是字符串或字符串数组")
    return [str(item) for item in values if str(item)]


def validate_api_requests(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize terminal-mode API requests used for backend recording."""
    raw = plan.get("apiRequests")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise SologsbError("record-plan.json 的 apiRequests 必须是数组")
    if not raw and plan.get("requiresApiRequests"):
        raise SologsbError(
            "纯后端/API 录制必须在 record-plan.json.apiRequests 中至少配置一个真实请求，"
            "用于在终端中模拟 API 调用并展示响应"
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise SologsbError(f"apiRequests[{index}] 必须是对象")
        method = str(item.get("method") or "GET").upper()
        if method not in API_METHODS:
            raise SologsbError(f"apiRequests[{index}].method 不支持: {method}")
        url = str(item.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            raise SologsbError(f"apiRequests[{index}].url 必须是 http(s) 完整地址: {url!r}")
        headers = item.get("headers") or {}
        if not isinstance(headers, dict):
            raise SologsbError(f"apiRequests[{index}].headers 必须是对象")
        normalized_headers = {str(key): str(value) for key, value in headers.items()}
        body = item.get("body")
        if body is not None and not isinstance(body, (dict, list, str, int, float, bool)):
            raise SologsbError(f"apiRequests[{index}].body 只支持对象、数组、字符串或数字")
        extract = item.get("extract") or {}
        if not isinstance(extract, dict):
            raise SologsbError(f"apiRequests[{index}].extract 必须是对象")
        extract_name = str(extract.get("name") or "").strip()
        extract_path = str(extract.get("path") or "").strip()
        if extract_name and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", extract_name):
            raise SologsbError(f"apiRequests[{index}].extract.name 必须是合法变量名")
        if extract_name and not extract_path:
            raise SologsbError(f"apiRequests[{index}].extract.path 不能为空")
        normalized.append(
            {
                "name": str(item.get("name") or f"API {index}").strip() or f"API {index}",
                "method": method,
                "url": url,
                "headers": normalized_headers,
                "body": body,
                "expectedStatus": _normalize_expected_status(item.get("expectedStatus")),
                "expectContains": _normalize_contains(item.get("expectContains")),
                "extractName": extract_name,
                "extractPath": extract_path,
            }
        )
    return normalized


def _api_request_shell(item: dict[str, Any], timeout: float = 20.0) -> str:
    body_text = ""
    if item["body"] is not None:
        if isinstance(item["body"], str):
            body_text = item["body"]
        else:
            body_text = json.dumps(item["body"], ensure_ascii=False, separators=(",", ":"))
    parts = [
        "sologsb_api_check",
        _shell_double_quote(item["name"]),
        _shell_double_quote(item["expectedStatus"]),
        _shell_double_quote(json.dumps(item["expectContains"], ensure_ascii=False)),
        _shell_double_quote(item["extractName"]),
        _shell_double_quote(item["extractPath"]),
        _shell_double_quote(item["method"]),
        _shell_double_quote(item["url"]),
        _shell_double_quote(body_text),
    ]
    for key, value in item["headers"].items():
        parts.extend([_shell_double_quote("-H"), _shell_double_quote(f"{key}: {value}")])
    if item["body"] is not None:
        parts.extend([_shell_double_quote("--data-raw"), _shell_double_quote(body_text)])
    parts.extend([
        _shell_double_quote("--connect-timeout"),
        _shell_double_quote("5"),
        _shell_double_quote("--max-time"),
        _shell_double_quote(str(timeout)),
    ])
    return " ".join(parts)


def _terminal_command(plan: dict[str, Any], log_path: Path, script_path: Path, result_path: Path) -> str:
    start = str(plan.get("startCommand") or "").strip()
    if not start:
        raise SologsbError("terminal/failed-start 模式缺少 startCommand")
    commands = [str(value) for value in plan.get("commands") or [] if str(value).strip()]
    cleanup_commands = [str(value) for value in plan.get("cleanupCommands") or [] if str(value).strip()]
    precommands = [str(value) for value in plan.get("preCommands") or [] if str(value).strip()]
    api_requests = validate_api_requests(plan)
    api_timeout = float(plan.get("apiTimeoutSeconds") or 20)
    project_dir = Path(str(plan["projectDir"])).resolve()
    block = [
        f"  {start}",
        *[f"  {command}" for command in commands],
        "  scenario_rc=$?",
    ]
    if api_requests:
        block.append("  api_rc=0")
        for item in api_requests:
            block.append(f"  {_api_request_shell(item, timeout=api_timeout)} || api_rc=1")
        block.append('  if [ "$api_rc" -ne 0 ] || [ "$scenario_rc" -ne 0 ]; then scenario_rc=1; fi')
    block.extend(f"  {command} || true" for command in cleanup_commands)
    block.append('  exit "$scenario_rc"')
    lines = [
        "#!/usr/bin/env bash",
        "set -o pipefail",
        f"cd {shlex.quote(str(project_dir))}",
        *precommands,
    ]
    if api_requests:
        lines.append(API_CALL_FUNCTION)
    lines.extend([
        "{",
        *block,
        f"}} 2>&1 | tee {shlex.quote(str(log_path))}",
        "rc=${PIPESTATUS[0]}",
        f"printf '__EXIT_CODE__=%s\\n' \"$rc\" > {shlex.quote(str(result_path))}",
        'exit "$rc"',
        "",
    ])
    script_path.write_text("\n".join(lines), encoding="utf-8")
    script_path.chmod(0o755)
    return f"bash {shlex.quote(str(script_path))}"

def _finalize_terminal_video(
    *,
    task_root: Path,
    side: str,
    plan: dict[str, Any],
    cropped: Path,
    result_path: Path,
) -> tuple[Path, int | None]:
    exit_code: int | None = None
    if result_path.is_file():
        match = re.search(r"__EXIT_CODE__=(\d+)", result_path.read_text(encoding="utf-8", errors="replace"))
        if match:
            exit_code = int(match.group(1))
    expected_failure = bool(plan.get("expectedFailure")) or plan.get("mode") == "failed-start"
    if expected_failure and exit_code == 0:
        raise SologsbError("failed-start 录制要求真实启动失败，但命令退出码为 0")
    if plan.get("requireFailureExit") and exit_code is None:
        raise SologsbError("要求录制真实失败，但终端日志没有可验证退出码")
    default_name = "failed-start.mp4" if plan.get("mode") == "failed-start" or (exit_code not in (None, 0)) else "demo.mp4"
    output_name = str(plan.get("outputName") or default_name)
    return _copy_final(cropped, task_root, side, output_name), exit_code


def _run_terminal_app(
    task_root: Path,
    side: str,
    plan: dict[str, Any],
    *,
    pointer_strategy: str,
    focus_guard: _FocusRestoreGuard,
    frontmost_monitor: _FrontmostWindowMonitor,
) -> tuple[Path, int | None]:
    output_dir = task_root / "monitor" / "recording" / side.lower() / TERMINAL_RUNTIME_DIR
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = output_dir / "screen.mov"
    cropped = output_dir / "terminal-cropped.mp4"
    log_path = output_dir / "terminal.log"
    runner_script = output_dir / "run-terminal.sh"
    result_path = output_dir / "exit-code.txt"
    pid_file = output_dir / "screen.pid"
    title = f"sologsb-{side.lower()}-{random.randrange(0x100000):05x}"
    window_id = 0
    tty = ""
    try:
        window_id, tty = _terminal_open_window(title, Path(str(plan["projectDir"])).resolve())
        window_info = _window_info_by_id(window_id, label="Terminal")
        focus_guard.restore_if_recording_frontmost(
            {int(window_info.get("ownerPid") or 0)},
            "terminal-open",
            {window_id},
        )
        _start_window_segment(
            output=raw,
            pid_file=pid_file,
            window_info=window_info,
            pointer_strategy=pointer_strategy,
            frontmost_monitor=frontmost_monitor,
        )
        try:
            for command in plan.get("preCommands") or []:
                _terminal_send(window_id, str(command))
                time.sleep(0.4)
            command = _terminal_command(plan, log_path, runner_script, result_path)
            _terminal_send(window_id, command)
            time.sleep(float(plan.get("holdSeconds") or 12))
        finally:
            _stop_window_segment(
                raw=raw,
                cropped=cropped,
                window_info=window_info,
                pid_file=pid_file,
                require_visible_content=True,
            )
    finally:
        if window_id:
            _terminal_close_window(window_id, tty)
    return _finalize_terminal_video(
        task_root=task_root,
        side=side,
        plan=plan,
        cropped=cropped,
        result_path=result_path,
    )


def _run_terminal(
    task_root: Path,
    side: str,
    plan: dict[str, Any],
    terminal_app: str,
    *,
    pointer_strategy: str,
    focus_guard: _FocusRestoreGuard,
    frontmost_monitor: _FrontmostWindowMonitor,
) -> tuple[Path, int | None]:
    if normalize_terminal_app(terminal_app) != TERMINAL_APP:
        raise SologsbError("录屏硬门禁要求终端应用必须是 Terminal.app")
    return _run_terminal_app(
        task_root,
        side,
        plan,
        pointer_strategy=pointer_strategy,
        focus_guard=focus_guard,
        frontmost_monitor=frontmost_monitor,
    )


def normalize_terminal_app(value: Any) -> str:
    """Terminal.app is the only recording terminal; legacy "otty" plans map onto it."""
    requested = str(value or "").strip().lower()
    if requested in TERMINAL_APP_ALIASES:
        return TERMINAL_APP
    raise SologsbError(f"录屏硬门禁要求 terminalApp=terminal（Terminal.app），当前为: {value!r}")


def normalize_recording_targets(targets: Any) -> list[str]:
    normalized: list[str] = []
    for value in list(targets or []):
        name = str(value or "").strip()
        if name in TERMINAL_TARGET_ALIASES:
            name = "Terminal"
        elif name == "Google Chrome":
            name = "Chrome"
        normalized.append(name)
    return normalized


def validate_recording_targets(mode: str, targets: Any, terminal_app: str = TERMINAL_APP) -> list[str]:
    normalize_terminal_app(terminal_app)
    values = normalize_recording_targets(targets)
    terminal_name = "Terminal"
    if mode == "web":
        if set(values) != {terminal_name, "Chrome"}:
            raise SologsbError(f"Web 录屏只能包含 {terminal_name} 和 Chrome")
        return [terminal_name, "Chrome"]
    if mode in {"terminal", "failed-start"}:
        if set(values) != {terminal_name}:
            raise SologsbError(f"终端或失败录屏只能包含 {terminal_name}")
        return [terminal_name]
    if mode == "desktop":
        raise SologsbError("禁止录制桌面应用，必须使用 Terminal/Chrome 窗口 ID")
    raise SologsbError(f"未知录制模式: {mode}")


def _record_side_locked(
    task_root: Path,
    side: str,
    plan_path: Path,
    draft: dict[str, Any],
    state: dict[str, Any],
    lock_metadata: dict[str, Any],
) -> dict[str, Any]:
    _run_recording_preflight(task_root, side, draft)
    draft["outputName"] = recording_output_name(task_root, side)
    mode = str(draft.get("mode") or "")
    terminal_app = normalize_terminal_app(draft.get("terminalApp"))
    draft["terminalApp"] = terminal_app
    capture_kind = str(draft.get("captureKind") or "window-id")
    pointer_strategy = normalize_pointer_strategy(draft.get("pointerStrategy"))
    draft["pointerStrategy"] = pointer_strategy
    if capture_kind != "window-id":
        raise SologsbError("录屏硬门禁要求 captureKind=window-id")
    if pointer_strategy not in POINTER_POLICIES:
        raise SologsbError("pointerStrategy 只能是 none")
    recording_root = task_root / "monitor" / "recording" / side.lower()
    frontmost_report_path = recording_root / "frontmost-window-monitor.json"
    service_cleanup_path = recording_root / "service-cleanup.json"
    baseline_process_ids = _all_process_ids()
    service_ports = _recording_service_ports(draft)
    baseline_listeners = _listener_rows(service_ports)
    frontmost_monitor = _FrontmostWindowMonitor(frontmost_report_path)
    focus_guard: _FocusRestoreGuard | None = None
    frontmost_report: dict[str, Any] = {}
    service_cleanup: dict[str, Any] = {}
    video: Path
    command_exit: int | None
    terminal_launched = False
    try:
        with contextlib.nullcontext():
            # Capture the user's app before anything is launched, so a focus
            # restore always returns to what the user was actually using.
            focus_guard = _FocusRestoreGuard()
            frontmost_monitor.start()
            if mode in {"web", "terminal", "failed-start"}:
                _terminal_write_profile_prefs()
                terminal_launched = _terminal_ensure_ready()
                _terminal_ensure_profile()
            if mode == "web":
                default_targets = ["Terminal", "Chrome"]
                validate_recording_targets(mode, draft.get("targetApps") or default_targets, terminal_app)
                if CHECK_ENV.is_file():
                    check = run(["/bin/bash", str(CHECK_ENV)], check=False, timeout=120)
                    if check.returncode != 0:
                        raise SologsbError(check.stderr.decode("utf-8", errors="replace") or "录屏环境检查失败")
                video, command_exit = _run_web_terminal(
                    task_root,
                    side,
                    draft,
                    pointer_strategy=pointer_strategy,
                    focus_guard=focus_guard,
                    frontmost_monitor=frontmost_monitor,
                )
            elif mode in {"terminal", "failed-start"}:
                default_targets = ["Terminal"]
                validate_recording_targets(mode, draft.get("targetApps") or default_targets, terminal_app)
                validate_api_requests(draft)
                video, command_exit = _run_terminal(
                    task_root,
                    side,
                    draft,
                    terminal_app,
                    pointer_strategy=pointer_strategy,
                    focus_guard=focus_guard,
                    frontmost_monitor=frontmost_monitor,
                )
            else:
                validate_recording_targets(mode, draft.get("targetApps") or [], terminal_app)
                raise SologsbError(f"未知录制模式: {mode}")
    finally:
        _terminal_quit_if_launched(terminal_launched)
        frontmost_report = frontmost_monitor.stop()
        service_cleanup = _cleanup_recording_services(
            plan=draft,
            baseline_process_ids=baseline_process_ids,
            baseline_listeners=baseline_listeners,
            report_path=service_cleanup_path,
        )
        if focus_guard is not None:
            focus_guard.restore_final()
    expected_app_failure = mode == "failed-start" or bool(draft.get("expectedBrowserFailure"))
    observed_app_failure = command_exit not in (None, 0)
    command_ok = recording_command_ok(command_exit, expected_app_failure)
    runtime_dir = task_root / "monitor" / "recording" / side.lower() / (WEB_RUNTIME_DIR if mode == "web" else TERMINAL_RUNTIME_DIR)
    browser_result_path = runtime_dir / "browser-result.json"
    terminal_log_path = runtime_dir / "terminal.log"
    guard_reports = []
    window_capture_reports = []
    for guard_path in sorted(runtime_dir.rglob("*-cursor-guard.json")):
        report = read_json(guard_path, {}) or {}
        guard_reports.append(
            {
                "path": str(guard_path.resolve()),
                "status": report.get("status"),
                "parkPoint": report.get("parkPoint"),
                "parkApplied": report.get("parkApplied"),
                "pointerMoved": report.get("pointerMoved"),
                "mouseButtonsQueried": report.get("mouseButtonsQueried"),
                "positionReadOnly": report.get("positionReadOnly"),
                "segment": report.get("segment"),
                "windowId": report.get("windowId"),
                "captureKind": report.get("captureKind"),
                "pollIntervalSeconds": report.get("pollIntervalSeconds"),
                "pointerStrategy": report.get("pointerStrategy"),
                "pointerPolicy": report.get("pointerPolicy"),
                "reappliedCount": report.get("reappliedCount"),
                "skippedWhileDragging": report.get("skippedWhileDragging"),
                "skippedWhileUserActive": report.get("skippedWhileUserActive"),
                "hostInputRespected": report.get("hostInputRespected"),
                "finalPointerInsideWindow": report.get("finalPointerInsideWindow"),
            }
        )
    for capture_path in sorted(runtime_dir.rglob("*-window-capture.json")):
        report = read_json(capture_path, {}) or {}
        window_capture_reports.append(
            {
                "path": str(capture_path.resolve()),
                "status": report.get("status"),
                "captureKind": report.get("captureKind"),
                "captureBackend": report.get("captureBackend"),
                "showsCursor": report.get("showsCursor"),
                "cursorCaptured": report.get("cursorCaptured"),
                "windowId": report.get("windowId"),
                "ownerPid": report.get("ownerPid"),
                "ownerName": report.get("ownerName"),
                "ownerBundleId": report.get("ownerBundleId"),
                "ownerApp": report.get("ownerApp"),
                "windowName": report.get("windowName"),
                "bounds": report.get("bounds"),
                "exitCode": report.get("exitCode"),
                "readyPath": report.get("readyPath"),
                "logPath": report.get("logPath"),
                "outputPath": report.get("outputPath"),
                "visualContent": report.get("visualContent"),
            }
        )
    chrome_profile_cleanup = read_json(runtime_dir / "chrome-profile-cleanup.json", {}) or {}
    capture_backend_ok = bool(window_capture_reports) and all(
        item.get("captureBackend") == "screen-capture-kit"
        and item.get("showsCursor") is False
        and item.get("cursorCaptured") is False
        for item in window_capture_reports
    )
    recording_metadata = {
        "activationPerformed": False,
        "untouched": True,
        "captureBackend": "screen-capture-kit",
        "captureExcludesCursor": capture_backend_ok,
        "userFrontmostAppAtStart": (focus_guard.user_frontmost_app if focus_guard else {}),
        "focusRestores": (focus_guard.events if focus_guard else []),
        "focusRestoreOk": bool(focus_guard and focus_guard.events) and all(
            event.get("action") != "restore-user-app" or event.get("restored") is True
            for event in (focus_guard.events if focus_guard else [])
        ),
        "frontmostSampling": frontmost_report,
        "serviceCleanup": service_cleanup,
        "residualAppPortListeners": service_cleanup.get("residualAppPortListeners") or [],
        "chromeProfileCleanup": chrome_profile_cleanup,
    }
    recording_ok = command_ok and recording_metadata["focusRestoreOk"] and recording_isolation_ok(
        mode=mode,
        window_capture_reports=window_capture_reports,
        guard_reports=guard_reports,
        frontmost_report=frontmost_report,
        service_cleanup=service_cleanup,
    )
    result = {
        "ok": recording_ok,
        "mode": mode,
        "captureMethod": "window-id",
        "captureBackend": "screen-capture-kit",
        "cursorExcludedFromCapture": capture_backend_ok,
        "recordingMetadata": recording_metadata,
        "windowCaptures": window_capture_reports,
        "cursorSuppression": {
            "reports": guard_reports,
            "captureExcludesCursor": capture_backend_ok,
            "allHostInputUntouched": bool(guard_reports) and all(
                item.get("pointerPolicy") == "host-input-untouched"
                and item.get("hostInputRespected") is True
                and item.get("pointerMoved") is False
                and item.get("mouseButtonsQueried") is False
                for item in guard_reports
            ),
        },
        "videoPath": str(video.resolve()),
        "browserResultPath": str(browser_result_path.resolve()) if browser_result_path.is_file() else "",
        "terminalLogPath": str(terminal_log_path.resolve()) if terminal_log_path.is_file() else "",
        "durationSeconds": round(_duration_seconds(video), 3),
        "width": video_dimensions(video)[0],
        "height": video_dimensions(video)[1],
        "sizeBytes": video.stat().st_size,
        "recordedAt": utc_now(),
        "planPath": str(plan_path.resolve()),
        "commandExitCode": command_exit,
        "expectedAppFailure": expected_app_failure,
        "observedAppFailure": observed_app_failure,
        "appOutcome": "failure" if observed_app_failure else "success",
        "globalRecordingLock": {
            "path": str(lock_metadata["lockPath"]),
            "projectKey": str(lock_metadata["projectKey"]),
            "projectLabel": str(lock_metadata["projectLabel"]),
            "acquiredAt": str(lock_metadata["acquiredAt"]),
            "waitSeconds": float(lock_metadata["waitSeconds"]),
        },
    }
    recordings = state.setdefault("recordings", {})
    recordings[side] = result
    state["recordings"] = recordings
    if not recording_ok:
        state["status"] = "gsb_ready"
    elif (recordings.get("A") or {}).get("ok") and (recordings.get("B") or {}).get("ok"):
        state["status"] = "recorded"
    save_state(task_root, state)
    refresh_excel(task_root)
    return result


def record_side(
    task_root: Path,
    side: str,
    plan_path: Path | None = None,
    *,
    lock_timeout: float = DEFAULT_RECORDING_LOCK_TIMEOUT,
) -> dict[str, Any]:
    side = side.upper()
    state = read_json(task_root / "monitor" / "state.json", {})
    if state.get("status") not in {"gsb_ready", "recorded", "complete"}:
        raise SologsbError("必须先完成 GSB 文案门禁")
    if side == "B" and not ((state.get("recordings") or {}).get("A") or {}).get("ok"):
        raise SologsbError("必须先完成 A 录屏，再录 B")
    plan_path = plan_path or prepare_recording(task_root, side)
    draft = read_json(plan_path, {})
    if "<" in str(draft.get("startCommand") or "") or "TODO" in str(draft.get("startCommand") or ""):
        raise SologsbError(f"请先完善录制脚本: {plan_path}")
    _run_recording_preflight(task_root, side, draft, key="buildCommands", stage="build")
    with global_recording_lock(task_root, side=side, timeout=lock_timeout) as lock_metadata:
        # The lock wait can be long; saving the pre-wait snapshot later would
        # drop whatever other commands wrote to state.json meanwhile.
        state = read_json(task_root / "monitor" / "state.json", {})
        return _record_side_locked(task_root, side, plan_path, draft, state, lock_metadata)
