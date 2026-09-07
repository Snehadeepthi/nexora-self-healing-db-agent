#!/usr/bin/env python3
"""
Reverts the TEMPORARY test change made by apply_lower_connection_threshold.py
-- restores config.CONNECTION_PCT_THRESHOLD from 0.08 back to its real
production value of 0.8 (80%). Run this AFTER the live Tier 3
approval-timeout test has completed (the connection-storm anomaly has been
observed, the TTL has expired, and approval_timeout has been verified in
BigQuery/Slack) -- do not leave the lowered threshold deployed.

Safety: single anchor, verified to occur exactly once before writing.
Backs up the file first; aborts cleanly with no changes if the anchor
doesn't match exactly once (e.g. if this is run before the lowering patch,
or run twice).
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "config.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()

anchor = "CONNECTION_PCT_THRESHOLD = 0.08  # TEMPORARY -- live Tier 3 TTL test, MUST revert to 0.8 after\n"
n = src.count(anchor)
if n != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for anchor, found {n}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"If this is 0, the threshold may already be back at 0.8 (nothing to do), "
        f"or the lowering patch was never applied. Check config.py directly.\n"
        f"No files have been modified."
    )

replacement = "CONNECTION_PCT_THRESHOLD = 0.8\n"
new_src = src.replace(anchor, replacement, 1)

backup_path = path + ".bak.tier3livetest.restore"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("CONNECTION_PCT_THRESHOLD restored to 0.8 (production value). Redeploy now.")
