#!/usr/bin/env python3
"""
Restores TIER3_APPROVAL_TTL_SECONDS to its real value of 900 (15 min) after
apply_temp_lower_ttl.py's temporary 30s verification override. Run this,
then redeploy, before this deployment goes anywhere near a real demo or
judge -- a 30s Tier 3 approval window is a verification convenience, not a
real setting.

Anchor-verified, backs up to config.py.bak.prerestorettl first.
"""
PATH = "gcp_deploy/services/orchestrator/config.py"

with open(PATH) as f:
    content = f.read()
original = content

anchor = (
    "# cascade doesn't fester, long enough a human has a real shot at the Slack\n"
    "# ping.\n"
    "TIER3_APPROVAL_TTL_SECONDS = 30  # TEMP for verification -- restore to 900 via apply_restore_ttl.py\n"
)
n = content.count(anchor)
if n != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for anchor in {PATH}, found {n}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"This usually means apply_temp_lower_ttl.py was never run, or the\n"
        f"line was hand-edited since. Check the file directly:\n"
        f"  grep -n TIER3_APPROVAL_TTL_SECONDS {PATH}\n"
        f"and fix it manually if needed -- it MUST read 900 before any real demo.\n"
        f"No files have been modified by this script."
    )

replacement = (
    "# cascade doesn't fester, long enough a human has a real shot at the Slack\n"
    "# ping.\n"
    "TIER3_APPROVAL_TTL_SECONDS = 900\n"
)
content = content.replace(anchor, replacement, 1)

backup_path = PATH + ".bak.prerestorettl"
with open(backup_path, "w") as f:
    f.write(original)
with open(PATH, "w") as f:
    f.write(content)

print(f"OK: patched {PATH} (backup at {backup_path})")
print("TIER3_APPROVAL_TTL_SECONDS is back to 900 (real value).")
print("Next: redeploy (gcloud builds submit ...) so the running container")
print("actually picks this up -- the 30s value stays live until you do.")
