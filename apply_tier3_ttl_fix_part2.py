#!/usr/bin/env python3
"""
Part 2 of the Tier 3 TTL fix -- finishes notifications.py and pipeline.py
only. config.py and guardrail_callbacks.py were already patched
successfully by apply_tier3_ttl_fix.py; do not re-run that script.
Same safety pattern: verifies both anchors exist exactly once before
writing anything, backs up both files first.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"

def load(fname):
    path = os.path.join(ROOT, fname)
    with open(path, "r") as f:
        return path, f.read()

def verify_once(content, anchor, fname):
    n = content.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT: expected exactly 1 match for anchor in {fname}, found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )

# ---------------------------------------------------------------------
# notifications.py
# ---------------------------------------------------------------------
notif_path, notif_src = load("notifications.py")
notif_anchor = "return self._send(self.approvals_to, subject, body)\n"
verify_once(notif_src, notif_anchor, "notifications.py")
notif_addition = '''
    # ---- Tier 3 approval TTL expiry notification ----
    def send_approval_timeout(self, action_payload: dict, detail: str) -> SentEmail:
        subject = f"[SRE Agent] Tier 3 approval EXPIRED: {action_payload.get('action_key')}"
        body = (
            f"A Tier 3 action was proposed and awaiting approval, but no one approved it "
            f"in time -- it has been automatically aborted WITHOUT executing.\\n\\n"
            f"Incident:     {action_payload.get('incident_id')}\\n"
            f"Action:       {action_payload.get('action_key')}\\n"
            f"Parameters:   {action_payload.get('params')}\\n"
            f"Detail:       {detail}\\n\\n"
            f"The underlying condition that triggered this proposal may still be "
            f"unresolved -- check the dashboard for this incident."
        )
        return self._send(self.approvals_to, subject, body)
'''
notif_new = notif_src.replace(notif_anchor, notif_anchor + notif_addition, 1)

# ---------------------------------------------------------------------
# pipeline.py
# ---------------------------------------------------------------------
pipe_path, pipe_src = load("pipeline.py")
pipe_anchor = (
    "        with self._pipeline_lock:\n"
    "            db_breaker = self.guardrails._breaker_for(self.engine.id)\n"
)
verify_once(pipe_src, pipe_anchor, "pipeline.py")
pipe_replacement = (
    "        with self._pipeline_lock:\n"
    "            self.guardrails.expire_stale_approvals(self.engine.id)\n"
    "            db_breaker = self.guardrails._breaker_for(self.engine.id)\n"
)
pipe_new = pipe_src.replace(pipe_anchor, pipe_replacement, 1)

# ---------------------------------------------------------------------
# Both anchors verified -- back up and write both files.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (notif_path, notif_src, notif_new),
    (pipe_path, pipe_src, pipe_new),
]:
    backup_path = path + ".bak.pretier3ttl"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nBoth remaining files patched successfully. All 4 files now done.")
