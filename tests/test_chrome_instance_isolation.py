from __future__ import annotations

# ruff: noqa: E402

import tempfile
import unittest
import sys
import json
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recorder
from sologsb import _recording_chrome_instance_errors


class ChromeInstanceIsolationTests(unittest.TestCase):
    def test_existing_chrome_process_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "chrome-profile"
            profile.mkdir()
            command = f"{recorder.CHROME_BINARY} --user-data-dir={profile}"
            self.assertFalse(
                recorder._process_belongs_to_instance(
                    4242,
                    4242,
                    profile,
                    {4242},
                    command=command,
                )
            )

    def test_process_outside_new_instance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "chrome-profile"
            profile.mkdir()
            command = f"{recorder.CHROME_BINARY} --user-data-dir={profile}"
            with mock.patch.object(recorder, "_process_parent_pid", return_value=1):
                self.assertFalse(
                    recorder._process_belongs_to_instance(
                        333,
                        222,
                        profile,
                        set(),
                        command=command,
                    )
                )

    def test_occupied_debug_port_is_rejected(self) -> None:
        with mock.patch.object(recorder, "_listener_rows", return_value=[{"pid": 9999}]):
            self.assertFalse(recorder._debug_port_is_free(9333))
            with self.assertRaisesRegex(recorder.SologsbError, "已被占用"):
                recorder._ensure_debug_port_free(9333)

    def test_mismatched_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "chrome-profile"
            profile.mkdir()
            other = Path(temp) / "other-profile"
            command = f"{recorder.CHROME_BINARY} --user-data-dir={other}"
            self.assertFalse(recorder._command_uses_chrome_profile(command, profile))
            self.assertFalse(
                recorder._process_belongs_to_instance(
                    222,
                    222,
                    profile,
                    set(),
                    command=command,
                )
            )

    def test_dedicated_instance_passes_same_status_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "chrome-profile"
            profile.mkdir()
            report_path = root / "chrome-instance.json"
            cleanup_path = root / "chrome-profile-cleanup.json"
            command = f"{recorder.CHROME_BINARY} --user-data-dir={profile}"
            with mock.patch.object(
                recorder,
                "_process_parent_pid",
                side_effect=lambda pid: {333: 222, 222: 1}.get(pid, 0),
            ):
                self.assertTrue(
                    recorder._process_belongs_to_instance(
                        333,
                        222,
                        profile,
                        {111},
                        command=command,
                    )
                )
            report = {
                "status": "ok",
                "dedicatedInstance": True,
                "reusedRunningChrome": False,
                "userChromeTouched": False,
                "recordingChromePid": 222,
                "preExistingChromePids": [111],
                "windowOwnerPid": 222,
                "windowId": 333,
                "portListenerPids": [222],
                "userDataDir": str(profile),
                "debugPort": 9333,
                "path": str(report_path),
                "validations": {
                    "preExistingChromeSnapshotted": True,
                    "debugPortWasFreeBeforeLaunch": True,
                    "portOwnerValidatedBeforeCdp": True,
                    "windowOwnerValidated": True,
                    "windowOwnerUsesDedicatedProfile": True,
                },
                "profileRemoved": True,
                "profileExistsAfterCleanup": False,
                "cleanupStatus": "removed",
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")
            cleanup_path.write_text("{}", encoding="utf-8")
            captures = [
                {
                    "ownerName": "Google Chrome",
                    "ownerBundleId": "com.google.Chrome",
                    "ownerPid": 222,
                    "windowId": 333,
                }
            ]
            self.assertTrue(
                recorder.chrome_instance_gate_ok(report, window_capture_reports=captures)
            )
            video = {
                "mode": "web",
                "recordingMetadata": {
                    "chromeInstance": {"path": str(report_path)},
                    "chromeProfileCleanup": {"path": str(cleanup_path)},
                },
            }
            cleanup_path.write_text(
                '{"status":"removed","removed":true,"profileExistsAfterCleanup":false}',
                encoding="utf-8",
            )
            self.assertEqual(_recording_chrome_instance_errors("A", video, captures), [])


if __name__ == "__main__":
    unittest.main()
