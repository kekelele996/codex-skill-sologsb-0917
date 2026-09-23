#!/usr/bin/env python3
"""Hold a sologsb-0917 project claim until the task no longer needs it.

Usage: claim_holder.py TTL_SECONDS LOCK_PATH TASK_ROOT

The claim lock fd is inherited from the parent.  The holder exits (which lets
the kernel drop the flock) when the TTL expires or when
``project_claims.claim_release_reason`` reports that the task is finished, so a
submitted task never keeps its project locked for the full TTL just because
nobody ran ``cleanup``.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

POLL_SECONDS = float(os.environ.get("SOLOGBS_CLAIM_HOLDER_POLL_SECONDS", "30") or 30)


def main() -> int:
    ttl = float(sys.argv[1])
    lock_path = Path(sys.argv[2])
    task_root = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    deadline = time.monotonic() + ttl
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        time.sleep(min(POLL_SECONDS, remaining))
        if task_root is None:
            continue
        try:
            from project_claims import claim_release_reason, mark_claim_self_released

            reason = claim_release_reason(task_root)
            if reason:
                mark_claim_self_released(task_root, lock_path, reason)
                return 0
        except Exception:
            # Never drop a claim because the checker itself broke; TTL still applies.
            continue


if __name__ == "__main__":
    raise SystemExit(main())
