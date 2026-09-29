from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import recorder  # noqa: E402
from common import SologsbError  # noqa: E402


class ChromeInstanceGateTests(unittest.TestCase):
    profile = Path("/tmp/sologsb-0917-unit-profile")
    root_pid = 100
    child_pid = 101

    def _call(
        self,
        *,
        pre_existing: list[int],
        profile: Path | None = None,
        command_profile: Path | None = None,
        listener_pid: int = 100,
        owner_pid: int = 101,
    ):
        selected_profile = profile or self.profile
        root_command = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={command_profile or selected_profile} --remote-debugging-port=9333"
        )
        child_command = (
            "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
            "Helpers/Google Chrome Helper --type=renderer"
        )

        def process_command(pid: int) -> str:
            return root_command if int(pid) == self.root_pid else child_command

        with mock.patch.object(recorder, "_process_tree_pids", return_value={self.root_pid, self.child_pid}), \
            mock.patch.object(recorder, "_process_command", side_effect=process_command), \
            mock.patch.object(recorder, "_listener_rows", return_value=[{"pid": listener_pid, "port": 9333}]):
            return recorder.validate_chrome_instance(
                recording_chrome_pid=self.root_pid,
                pre_existing_chrome_pids=pre_existing,
                profile=selected_profile,
                debug_port=9333,
                window_info={"windowId": 555, "ownerPid": owner_pid},
            )

    def test_existing_chrome_window_is_rejected(self) -> None:
        with self.assertRaisesRegex(SologsbError, "命中已存在 Chrome"):
            self._call(pre_existing=[900], owner_pid=900)

    def test_window_from_non_current_instance_is_rejected(self) -> None:
        with self.assertRaisesRegex(SologsbError, "不属于本次新起"):
            self._call(pre_existing=[900], owner_pid=202)

    def test_occupied_debug_port_is_rejected(self) -> None:
        with self.assertRaisesRegex(SologsbError, "非本次 Chrome"):
            self._call(pre_existing=[900], listener_pid=300)

    def test_profile_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(SologsbError, "profile 不符"):
            self._call(
                pre_existing=[900],
                command_profile=Path("/tmp/another-profile"),
            )

    def test_current_instance_passes(self) -> None:
        result = self._call(pre_existing=[900])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["recordingChromePid"], self.root_pid)
        self.assertEqual(result["windowOwnerPid"], self.child_pid)
        self.assertEqual(result["debugPortOwnerPids"], [self.root_pid])

    def test_persisted_evidence_requires_cleanup(self) -> None:
        report = {
            "status": "ok",
            "dedicatedInstance": True,
            "reusedRunningChrome": False,
            "userChromeTouched": False,
            "recordingChromePid": 100,
            "preExistingChromePids": [900],
            "recordingChromeProcessTree": [100, 101],
            "windowId": 555,
            "windowOwnerPid": 101,
            "debugPort": 9333,
            "debugPortOwnerPids": [100],
            "userDataDir": "/tmp/sologsb-0917-unit-profile",
            "profileCommandLineVerified": True,
            "debugPortOwnerVerified": True,
            "path": "/tmp/sologsb-0917-unit-instance.json",
        }
        cleanup = {
            "status": "removed",
            "removed": True,
            "existsAfter": False,
            "path": "/tmp/sologsb-0917-unit-profile",
        }
        self.assertTrue(recorder.chrome_instance_evidence_ok(report, cleanup))
        cleanup["status"] = "failed"
        self.assertFalse(recorder.chrome_instance_evidence_ok(report, cleanup))


if __name__ == "__main__":
    unittest.main()
