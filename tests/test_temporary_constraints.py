from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from temporary_constraints import (  # noqa: E402
    evaluate_double_perfect_delivery,
    prune_expired_policies,
)


class TemporaryConstraintTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        skill_md = root / "SKILL.md"
        start = "<!-- temporary-constraint:test:start -->"
        end = "<!-- temporary-constraint:test:end -->"
        skill_md.write_text(f"before\n\n{start}\n- temporary rule\n{end}\n\nafter\n", encoding="utf-8")
        policy_path = root / "references" / "temporary-constraints.json"
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "test-double-perfect",
                    "effectiveFrom": "2026-09-29T00:00:00+08:00",
                    "expiresAt": "2026-09-30T00:00:00+08:00",
                    "selfDeleteOnExpiry": True,
                    "documentation": {
                        "path": "SKILL.md",
                        "startMarker": start,
                        "endMarker": end,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return policy_path, skill_md

    def test_active_rule_blocks_double_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy_path, _ = self._fixture(root)
            result = evaluate_double_perfect_delivery(
                {"delivery": {"A": {"score": 5}, "B": {"score": 5}}},
                datetime.fromisoformat("2026-09-29T12:00:00+08:00"),
                policy_path=policy_path,
                skill_root=root,
            )
        self.assertTrue(result["active"])
        self.assertTrue(result["violation"])
        self.assertTrue(result["discardRequired"])
        self.assertEqual(result["action"], "discard-no-submit")

    def test_active_rule_allows_non_double_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy_path, _ = self._fixture(root)
            result = evaluate_double_perfect_delivery(
                {"delivery": {"A": {"score": 5}, "B": {"score": 4}}},
                datetime.fromisoformat("2026-09-29T23:59:59+08:00"),
                policy_path=policy_path,
                skill_root=root,
            )
        self.assertTrue(result["active"])
        self.assertFalse(result["violation"])
        self.assertEqual(result["action"], "none")

    def test_rule_self_prunes_at_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy_path, skill_md = self._fixture(root)
            result = prune_expired_policies(
                datetime.fromisoformat("2026-09-30T00:00:00+08:00"),
                policy_path=policy_path,
                skill_root=root,
            )
            self.assertTrue(result["removed"])
            self.assertFalse(policy_path.exists())
            text = skill_md.read_text(encoding="utf-8")
            self.assertNotIn("temporary-constraint:test:start", text)
            self.assertNotIn("temporary rule", text)
            self.assertIn("before", text)
            self.assertIn("after", text)

    def test_rule_does_not_prune_before_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy_path, skill_md = self._fixture(root)
            result = prune_expired_policies(
                datetime.fromisoformat("2026-09-29T23:59:59+08:00"),
                policy_path=policy_path,
                skill_root=root,
            )
            self.assertFalse(result["removed"])
            self.assertEqual(result["reason"], "not_expired")
            self.assertTrue(policy_path.exists())
            self.assertIn("temporary rule", skill_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
