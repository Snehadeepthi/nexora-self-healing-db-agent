#!/usr/bin/env python3
"""
Applies the Tier 3 approval TTL fix across 4 files:
  - config.py               : add TIER3_APPROVAL_TTL_SECONDS constant
  - guardrail_callbacks.py  : timestamp pending approvals + expire_stale_approvals()
  - notifications.py        : add send_approval_timeout()
  - pipeline.py             : call expire_stale_approvals() at top of run_cycle()

Safety: verifies every anchor text exists EXACTLY ONCE in its target file
BEFORE writing anything. If any single check fails, the whole script aborts
with no files modified. Backs up every file to <name>.bak.pretier3ttl first.
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
# 1. config.py
# ---------------------------------------------------------------------
config_path, config_src = load("config.py")
config_anchor = (
    "CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3\n"
    "CIRCUIT_BREAKER_RESET_SECONDS = 120\n"
)
verify_once(config_src, config_anchor, "config.py")
config_addition = (
    "\n# Tier 3 approval TTL: a pending_approvals entry with no human response\n"
    "# within this window is auto-expired (see guardrail_callbacks.py's\n"
    "# expire_stale_approvals) rather than left to accumulate indefinitely.\n"
    "# During something like a lock cascade, an approval no one ever sees means\n"
    "# nothing acts while the underlying problem keeps getting worse -- this\n"
    "# bounds how long that silence can last. 900s = 15 minutes: short enough a\n"
    "# cascade doesn't fester, long enough a human has a real shot at the Slack\n"
    "# ping.\n"
    "TIER3_APPROVAL_TTL_SECONDS = 900\n"
)
config_new = config_src.replace(config_anchor, config_anchor + config_addition, 1)

# ---------------------------------------------------------------------
# 2. guardrail_callbacks.py -- two edits
# ---------------------------------------------------------------------
gc_path, gc_src = load("guardrail_callbacks.py")

gc_anchor1 = (
    '        if action.tier == config.Tier.TIER_3:\n'
    '            self.pending_approvals[incident_id] = {\n'
    '                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,\n'
    '                "query_text": tool_context.state.get("query_text", ""),\n'
    '            }\n'
)
verify_once(gc_src, gc_anchor1, "guardrail_callbacks.py (pending_approvals block)")
gc_replacement1 = (
    '        if action.tier == config.Tier.TIER_3:\n'
    '            self.pending_approvals[incident_id] = {\n'
    '                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,\n'
    '                "query_text": tool_context.state.get("query_text", ""),\n'
    '                "created_at": time.time(),\n'
    '            }\n'
)

gc_anchor2 = (
    '        if engine_id == "alloydb":\n'
    '            return self.alloydb_breaker\n'
    '        if engine_id == "mysql":\n'
    '            return self.mysql_breaker\n'
    '        return self.db_breaker\n'
)
verify_once(gc_src, gc_anchor2, "guardrail_callbacks.py (_breaker_for body)")
gc_new_method = '''
    def expire_stale_approvals(self, engine_id: str) -> list:
        """Tier 3 TTL safeguard: a pending_approvals entry with no human
        response within config.TIER3_APPROVAL_TTL_SECONDS is auto-expired
        rather than left to accumulate indefinitely -- during something
        like a lock cascade, an unresolved Tier 3 action sitting forever in
        PENDING_APPROVAL means nothing ever acts while the underlying
        problem can keep getting worse. Called once per engine at the start
        of that engine's run_cycle() tick (pipeline.py), scoped to THIS
        engine's own incident_ids (namespaced "{engine_id}-...", see
        pipeline.py's run_cycle()) so one engine's tick never touches
        another engine's still-live pending approval. Does not attempt to
        auto-escalate to a lower-tier fallback action -- it aborts safely
        and notifies loudly instead, so a human still finds out even though
        nothing executed.
        Known limitation: this clears the STALE entry, but if the
        underlying anomaly is still active, Predict may propose a fresh
        Tier 3 action on a later tick with a new incident_id -- this does
        not deduplicate repeated proposals for the same ongoing condition,
        it only bounds how long any single one can sit unresolved.
        """
        now = time.time()
        expired_ids = [
            incident_id for incident_id, pending in self.pending_approvals.items()
            if incident_id.startswith(f"{engine_id}-")
            and now - pending.get("created_at", now) > config.TIER3_APPROVAL_TTL_SECONDS
        ]
        for incident_id in expired_ids:
            pending = self.pending_approvals.pop(incident_id)
            detail = (
                f"Tier 3 action '{pending['action_key']}' was never approved within "
                f"{config.TIER3_APPROVAL_TTL_SECONDS}s -- auto-expired without executing."
            )
            self.audit_log.log("approval_timeout", incident_id=incident_id,
                                action_key=pending["action_key"], detail=detail)
            self.notifier.send_approval_timeout(
                {"action_key": pending["action_key"], "params": pending["params"],
                 "incident_id": incident_id}, detail,
            )
        return expired_ids
'''
gc_replacement2 = gc_anchor2 + gc_new_method

gc_new = gc_src.replace(gc_anchor1, gc_replacement1, 1)
gc_new = gc_new.replace(gc_anchor2, gc_replacement2, 1)

# ---------------------------------------------------------------------
# 3. notifications.py
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
# 4. pipeline.py
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
# All anchors verified -- now back up and write every file.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (config_path, config_src, config_new),
    (gc_path, gc_src, gc_new),
    (notif_path, notif_src, notif_new),
    (pipe_path, pipe_src, pipe_new),
]:
    backup_path = path + ".bak.pretier3ttl"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nAll 4 files patched successfully.")
