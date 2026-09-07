#!/usr/bin/env python3
"""
Adds a design-rule comment to config.py as a guard for FUTURE Oracle
actions -- not a bug fix, since none of the 5 current Oracle allowlisted
actions (kill_blocking_session, kill_runaway_query, flush_shared_pool,
increase_pga_target, restart_listener) perform schema DDL; they're all
ALTER SYSTEM / session-level tuning, which is why ORA-65066 (a
common-user-schema-DDL error) was ruled out as not applicable today.

This just leaves a note so that whoever adds the next Oracle action
remembers to check for that case.

Safety: anchors on the exact TIER3_APPROVAL_TTL_SECONDS = 900 line (already
confirmed present from the earlier TTL patch), verifies it occurs exactly
once, and appends the new comment block immediately after it. Backs up the
file first; aborts cleanly with no changes if the anchor isn't found
exactly once.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "config.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()

anchor = "TIER3_APPROVAL_TTL_SECONDS = 900\n"
count = src.count(anchor)
if count != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for anchor in {FNAME}, found {count}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"No files have been modified. Paste this error back for a corrected patch."
    )

addition = (
    "\n# Design rule for future Oracle actions: every Oracle action currently\n"
    "# in ALLOWLIST is ALTER SYSTEM / session-level tuning (kill a session,\n"
    "# flush the shared pool, adjust PGA target, restart the listener) -- none\n"
    "# perform schema-level DDL, which is why ORA-65066 (a common-user-schema\n"
    "# DDL error under a multitenant CDB) doesn't apply today. If a future\n"
    "# action ever needs to run DDL against a CDB, its statement_template must\n"
    "# explicitly include CONTAINER=ALL, or it risks ORA-65066 and a doomed\n"
    "# retry loop instead of a clean failure.\n"
)

new_src = src.replace(anchor, anchor + addition, 1)

backup_path = path + ".bak.pretier3ttl2"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("Design-rule comment added after TIER3_APPROVAL_TTL_SECONDS.")
