#!/usr/bin/env python3
"""
TEMPORARY, for verification only. Lowers TIER3_APPROVAL_TTL_SECONDS from
900 (15 min) to 30s so the suppression auto-clear flow can be exercised
end-to-end in a few minutes instead of ~18 -- same "temporarily lower a
threshold for the demo" pattern this project already uses elsewhere
(the connection-storm threshold), not a permanent config change.

Run this, redeploy, do the verification run (trigger Tier 3 -> let the 30s
TTL expire -> confirm /suppressions shows it -> wait for 3 healthy ticks
-> confirm auto-clear), then run apply_restore_ttl.py and redeploy AGAIN
to put the real 900s value back before this goes anywhere near a real demo
or judge -- a 30s Tier 3 approval window is not something to ship.

Anchor-verified, backs up to config.py.bak.pretemplowerttl first.
"""
PATH = "gcp_deploy/services/orchestrator/config.py"

with open(PATH) as f:
    content = f.read()
original = content

anchor = (
    "# cascade doesn't fester, long enough a human has a real shot at the Slack\n"
    "# ping.\n"
    "TIER3_APPROVAL_TTL_SECONDS = 900\n"
)
n = content.count(anchor)
if n != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for anchor in {PATH}, found {n}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"No files have been modified. Paste this error back for a corrected patch."
    )

replacement = (
    "# cascade doesn't fester, long enough a human has a real shot at the Slack\n"
    "# ping.\n"
    "TIER3_APPROVAL_TTL_SECONDS = 30  # TEMP for verification -- restore to 900 via apply_restore_ttl.py\n"
)
content = content.replace(anchor, replacement, 1)

backup_path = PATH + ".bak.pretemplowerttl"
with open(backup_path, "w") as f:
    f.write(original)
with open(PATH, "w") as f:
    f.write(content)

print(f"OK: patched {PATH} (backup at {backup_path})")
print("TIER3_APPROVAL_TTL_SECONDS is now 30s (TEMPORARY).")
print("Next: redeploy (gcloud builds submit ...), then run the verification.")
print("IMPORTANT: run apply_restore_ttl.py + redeploy again afterward -- do not")
print("leave this at 30s.")
