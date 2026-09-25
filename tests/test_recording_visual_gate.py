from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from recorder import _terminal_visual_content_metrics  # noqa: E402


def _encode(path: Path, source: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            "2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


class RecordingVisualGateTests(unittest.TestCase):
    def test_blank_terminal_surface_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "blank.mp4"
            _encode(video, "color=c=0x1e1e1e:s=1280x720:r=30")
            result = _terminal_visual_content_metrics(video)
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "failed")

    def test_visible_terminal_content_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "content.mp4"
            _encode(video, "testsrc2=s=1280x720:r=30")
            result = _terminal_visual_content_metrics(video)
            self.assertTrue(result["ok"], result)
            self.assertGreaterEqual(result["meanStd"], result["minimumMeanStd"])


if __name__ == "__main__":
    unittest.main()
