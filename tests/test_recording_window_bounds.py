from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from common import SologsbError  # noqa: E402
from recorder import (  # noqa: E402
    CHROME_WINDOW_BOUNDS,
    _assert_browser_window_aspect,
    _assert_window_bounds_unchanged,
    browser_window_bounds,
)


class BrowserWindowBoundsTests(unittest.TestCase):
    def test_default_bounds_when_plan_has_no_override(self) -> None:
        self.assertEqual(browser_window_bounds({}), CHROME_WINDOW_BOUNDS)
        self.assertEqual(browser_window_bounds({"browserWindow": {}}), CHROME_WINDOW_BOUNDS)

    def test_plan_override_keeps_16x9(self) -> None:
        bounds = browser_window_bounds({"browserWindow": {"width": 820, "height": 461}})
        self.assertEqual(bounds, (40, 40, 820, 461))
        bounds = browser_window_bounds(
            {"browserWindow": {"left": 10, "top": 20, "width": 1280, "height": 720}}
        )
        self.assertEqual(bounds, (10, 20, 1280, 720))

    def test_non_16x9_is_rejected(self) -> None:
        # 采集期间或计划里出现非 16:9 窗口，成片会留黑边，必须在开录前挡住。
        with self.assertRaises(SologsbError) as ctx:
            browser_window_bounds({"browserWindow": {"width": 820, "height": 560}})
        self.assertIn("16:9", str(ctx.exception))

    def test_too_small_and_bad_types_are_rejected(self) -> None:
        with self.assertRaises(SologsbError):
            browser_window_bounds({"browserWindow": {"width": 320, "height": 180}})
        with self.assertRaises(SologsbError):
            browser_window_bounds({"browserWindow": {"width": "宽", "height": 810}})

    def test_actual_window_aspect_must_be_16x9(self) -> None:
        # 系统把窗口尺寸改掉时，成片会出现黑边，必须在开录前就停下。
        _assert_browser_window_aspect({"bounds": "40,40,820,461"})
        with self.assertRaises(SologsbError) as ctx:
            _assert_browser_window_aspect({"bounds": "40,40,1440,900"})
        self.assertIn("16:9", str(ctx.exception))
        with self.assertRaises(SologsbError):
            _assert_browser_window_aspect({"bounds": ""})


class WindowBoundsGuardTests(unittest.TestCase):
    def _raw(self, root: Path) -> Path:
        raw = root / "browser-screen.mov"
        raw.write_bytes(b"stub")
        report = root / "browser-screen-window-capture.json"
        report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "captureKind": "window-id",
                    "captureBackend": "screen-capture-kit",
                    "showsCursor": False,
                    "cursorCaptured": False,
                    "windowId": 4242,
                    "bounds": "40,40,1440,810",
                }
            ),
            encoding="utf-8",
        )
        return raw

    def _read_report(self, raw: Path) -> dict:
        return json.loads(
            raw.with_name(f"{raw.stem}-window-capture.json").read_text(encoding="utf-8")
        )

    def test_unchanged_bounds_keep_report_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = self._raw(root)
            with mock.patch(
                "recorder._window_info_by_id",
                return_value={"bounds": "40,40,1440,810", "windowId": 4242},
            ):
                result = _assert_window_bounds_unchanged(
                    {"bounds": "40,40,1440,810", "windowId": 4242}, raw
                )
            self.assertFalse(result["changed"])
            report = self._read_report(raw)
            self.assertEqual(report["status"], "ok")
            self.assertIs(report["windowBoundsChangedDuringCapture"], False)
            self.assertEqual(report["windowBoundsAtCaptureStop"], "40,40,1440,810")

    def test_resized_window_fails_the_segment(self) -> None:
        # 采集期间把 1440x810 改成 820x560，成片页面会被放大并留黑边，必须判失败。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = self._raw(root)
            with mock.patch(
                "recorder._window_info_by_id",
                return_value={"bounds": "40,40,820,560", "windowId": 4242},
            ):
                result = _assert_window_bounds_unchanged(
                    {"bounds": "40,40,1440,810", "windowId": 4242}, raw
                )
            self.assertTrue(result["changed"])
            self.assertIn("缩放", result["error"])
            report = self._read_report(raw)
            self.assertEqual(report["status"], "failed")
            self.assertIs(report["windowBoundsChangedDuringCapture"], True)
            self.assertEqual(report["windowBoundsAtCaptureStart"], "40,40,1440,810")
            self.assertEqual(report["windowBoundsAtCaptureStop"], "40,40,820,560")


if __name__ == "__main__":
    unittest.main()
