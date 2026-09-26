from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import device_config  # noqa: E402
import status_push  # noqa: E402


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _FakeLimiter:
    def status(self):
        return {
            "ok": True, "limit": 5, "used": 3, "runningContainers": 2, "reservedSlots": 1,
            "queuedCandidates": 2, "runningNames": ["sologsb-x-1", "sologsb-x-2"],
        }

    def _count_all_containers(self):
        return False

    def _running_containers(self, count_all=False):
        return [("sologsb-x-1", "LD-1"), ("sologsb-x-2", "ld-1"), ("sologsb-db", "ld-1")]


class StatusPushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "config.json"
        patcher = mock.patch.dict(os.environ, {"SOLOSB_CONFIG": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        # 其他用例可能把本机真实设备配置注入过环境变量
        for name in ("SOLOSB_BARK_URL", "SOLOSB_NOTIFY_INTERVAL"):
            os.environ.pop(name, None)
        self.addCleanup(self.tmp.cleanup)

    def test_summary_counts_today_in_local_time(self):
        items = [
            {"status": "质检通过", "submittedAt": _now_utc()},
            {"status": "质检通过", "submittedAt": "2020-01-01T00:00:00Z"},
            {"status": "已提交", "submittedAt": _now_utc()},
        ]
        lines = status_push._summarize_submissions(items, 40)
        self.assertEqual(lines[0], "今日提交 2 · 总提交 40")
        self.assertIn("质检通过 2", lines[1])

    def test_container_section_counts_only_limited_containers(self):
        fake_module = mock.Mock(_ContainerLimiter=_FakeLimiter)
        with mock.patch.dict(sys.modules, {"side_runner": fake_module}):
            lines, per_project = status_push.container_section()
        self.assertEqual(lines, ["容器 3/5（运行 2 · 预占 1）· 候选排队 2"])
        self.assertEqual(per_project, {"ld-1": 2})

    def test_task_section_puts_container_holders_first(self):
        fake_claims = mock.Mock(claimed_project_codes=lambda base: {"aa-1", "zz-9"})
        with mock.patch.dict(sys.modules, {"project_claims": fake_claims}), \
                mock.patch.dict(os.environ, {"SOLO_MANAGER_BASE_URL": "http://m"}):
            lines = status_push.task_section({"zz-9": 2})
        self.assertEqual(lines, ["运行中任务 2（占容器 1）", "▶ zz-9 · 2 容器", "▶ aa-1"])

    def test_platform_falls_back_to_local_cache(self):
        cache = Path(self.tmp.name) / "cache.json"
        cache.write_text(json.dumps({
            "fetchedAt": _now_utc(), "serverTotal": 7,
            "items": [{"status": "已提交", "submittedAt": _now_utc()}],
        }), encoding="utf-8")
        preflight = mock.Mock()
        preflight._readonly_json.side_effect = OSError("handshake timed out")
        preflight.gsb_history_cache_path.return_value = cache
        fake_gsb = mock.Mock(_load_submission_preflight=lambda: preflight)
        with mock.patch.dict(sys.modules, {"gsb_tools": fake_gsb}):
            lines = status_push.platform_section()
        self.assertTrue(lines[0].startswith("今日提交 1 · 总提交 7（平台不可达，缓存 "))

    def test_bark_url_required_and_masked(self):
        with self.assertRaises(device_config.ConfigError):
            status_push.bark_send("t", "b")
        self.assertIn("notify.barkUrl", device_config.SECRET_FIELDS)

    def test_bark_send_retries_then_succeeds(self):
        device_config.save({"notify": {"barkUrl": "https://bark.example/KEY"}})
        replies = [OSError("offline"), mock.MagicMock()]
        replies[1].__enter__.return_value.read.return_value = b'{"code":200}'
        with mock.patch.object(status_push.urllib.request, "urlopen", side_effect=replies) as urlopen, \
                mock.patch.object(status_push.time, "sleep"):
            status_push.bark_send("标题", "正文")
        self.assertEqual(urlopen.call_count, 2)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://bark.example/KEY")
        self.assertIn(b"group=sologsb", request.data)

    def test_interval_has_floor_and_default(self):
        self.assertEqual(status_push._interval_minutes(1), 5)
        self.assertEqual(status_push._interval_minutes(None), 30)


if __name__ == "__main__":
    unittest.main()
