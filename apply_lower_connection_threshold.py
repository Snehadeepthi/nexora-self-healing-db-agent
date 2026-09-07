#!/usr/bin/env python3
"""
TEMPORARY TEST CHANGE -- part of the live Tier 3 approval-timeout
demonstration, not a permanent fix. Lowers config.CONNECTION_PCT_THRESHOLD
from 0.8 (80%) to 0.08 (8%) so a genuine connection-storm anomaly can be
induced on AlloyDB (max_connections=1000) with ~60-100 real connections
instead of the impractical ~800 the real 80% threshold would require.

MUST be reverted after the test via apply_restore_connection_threshold.py
-- this intentionally weakens a production safety threshold for the
duration of the test. Do not leave this deployed.

Safety: single anchor, verified to occur exactly once before writing.
Backs up the file first; aborts cleanly with no changes if the anchor
doesn't match exactly once.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "config.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()

anchor = "CONNECTION_PCT_THRESHOLD = 0.8\n"
n = src.count(anchor)
if n != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for anchor, found {n}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"No files have been modified. Paste this error back for a corrected patch."
    )

replacement = "CONNECTION_PCT_THRESHOLD = 0.08  # TEMPORARY -- live Tier 3 TTL test, MUST revert to 0.8 after\n"
new_src = src.replace(anchor, replacement, 1)

backup_path = path + ".bak.tier3livetest"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("CONNECTION_PCT_THRESHOLD temporarily lowered to 0.08 for the live test.")
print("REMINDER: run apply_restore_connection_threshold.py after the test completes.")
