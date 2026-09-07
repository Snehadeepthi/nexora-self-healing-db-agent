#!/usr/bin/env python3
"""
Applies the Tier 3 re-trigger flooding fix across 2 files:
  - guardrail_callbacks.py : suppress repeat Tier 3 proposals of the same
                              action on the same engine after an
                              approval_timeout, until manually cleared
  - main.py                : two new routes -- GET /suppressions and
                              POST /suppressions/clear/<action_key> -- so
                              an operator can see and reset the flag

Context: external review flagged that if an unresolved anomaly outlives the
900s Tier 3 approval TTL (guardrail_callbacks.py's expire_stale_approvals,
added by apply_tier3_ttl_fix.py), Predict proposes a FRESH Tier 3 action
under a new incident_id on the next tick -- and keeps doing so roughly
every ~2 minutes for as long as the anomaly persists, spamming on-call
engineers in Slack. This patch keys a "suppressed" flag on
(engine_id, action_key) the moment a proposal times out; any further
proposal of that exact action on that engine is logged
(action_suppressed_after_timeout) but does NOT create a new
pending_approvals entry and does NOT send another Slack ping, until an
operator explicitly clears it via the new endpoint.

Safety: verifies every anchor text exists EXACTLY ONCE in its target file
BEFORE writing anything. If any single check fails, the whole script aborts
with no files modified. Backs up every file to <name>.bak.pretier3flood first.
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
# 1. guardrail_callbacks.py -- four edits
# ---------------------------------------------------------------------
gc_path, gc_src = load("guardrail_callbacks.py")

# 1a. __init__: add the suppressed-flag dict alongside pending_approvals.
gc_anchor_init = (
    '        self.pending_approvals = {}   # incident_id -> {action_key, params, snapshot}\n'
    '        self._in_flight_snapshots = {}  # incident_id -> snapshot, bridges before_tool -> after_tool\n'
)
verify_once(gc_src, gc_anchor_init, "guardrail_callbacks.py (__init__ dicts)")
gc_replacement_init = (
    '        self.pending_approvals = {}   # incident_id -> {action_key, params, snapshot}\n'
    '        self._in_flight_snapshots = {}  # incident_id -> snapshot, bridges before_tool -> after_tool\n'
    '        # (engine_id, action_key) -> {since, since_incident_id, repeat_count, last_incident_id}\n'
    '        # Set by expire_stale_approvals() the moment a Tier 3 proposal times out;\n'
    '        # cleared only by clear_suppression() (an operator action, see main.py\'s\n'
    '        # POST /suppressions/clear/<action_key>) -- see before_tool()\'s Tier 3\n'
    '        # branch and expire_stale_approvals()\'s docstring for the full mechanism.\n'
    '        self.suppressed = {}\n'
)

# 1b. before_tool(): check suppression before the snapshot read, short-circuit
#     without creating a new pending_approvals entry or paging Slack again.
gc_anchor_tier3 = (
    '        snapshot = self._snapshot(action, args)\n'
    '\n'
    '        if action.tier == config.Tier.TIER_3:\n'
    '            self.pending_approvals[incident_id] = {\n'
    '                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,\n'
    '                "query_text": tool_context.state.get("query_text", ""),\n'
    '                "created_at": time.time(),\n'
    '            }\n'
    '            self.audit_log.log("action_pending_approval", incident_id=incident_id,\n'
    '                                action_key=tool.name, tier=int(action.tier))\n'
    '            email = self.notifier.send_approval_request(\n'
    '                {"action_key": tool.name, "params": args, "incident_id": incident_id}, action\n'
    '            )\n'
    '            self.audit_log.log("approval_notification_sent", incident_id=incident_id, to=email.to)\n'
    '            return {\n'
    '                "status": "PENDING_APPROVAL",\n'
    '                "detail": f"\'{tool.name}\' requires Tier 3 approval",\n'
    '                "incident_id": incident_id,\n'
    '            }\n'
    '\n'
    '        breaker = self._breaker_for(action.engine)\n'
)
verify_once(gc_src, gc_anchor_tier3, "guardrail_callbacks.py (Tier 3 branch)")
gc_replacement_tier3 = '''        if action.tier == config.Tier.TIER_3:
            suppression_key = (action.engine, tool.name)
            suppressed = self.suppressed.get(suppression_key)
            if suppressed is not None:
                # Flooding guard: a prior proposal of this EXACT action on
                # this engine already timed out unapproved (see
                # expire_stale_approvals() below) and set this flag.
                # Skipping the DB snapshot read here too, not just the
                # Slack ping -- the underlying anomaly is very often DB
                # load itself, so a repeat proposal every ~2 minutes
                # shouldn't also cost a fresh Toolbox round trip on top of
                # the audit entry.
                suppressed["repeat_count"] = suppressed.get("repeat_count", 0) + 1
                suppressed["last_incident_id"] = incident_id
                detail = (
                    f"'{tool.name}' on '{action.engine}' is suppressed after a prior Tier 3 "
                    f"approval_timeout (since incident {suppressed['since_incident_id']}) -- "
                    f"{suppressed['repeat_count']} repeat proposal(s) dropped without a fresh "
                    f"Slack ping. POST /suppressions/clear/{tool.name}?db={action.engine} (or the "
                    f"dashboard's reset control) once the underlying condition is confirmed handled."
                )
                self.audit_log.log("action_suppressed_after_timeout", incident_id=incident_id,
                                    action_key=tool.name, detail=detail)
                return {"status": "SUPPRESSED", "detail": detail}

            snapshot = self._snapshot(action, args)
            self.pending_approvals[incident_id] = {
                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,
                "query_text": tool_context.state.get("query_text", ""),
                "created_at": time.time(),
            }
            self.audit_log.log("action_pending_approval", incident_id=incident_id,
                                action_key=tool.name, tier=int(action.tier))
            email = self.notifier.send_approval_request(
                {"action_key": tool.name, "params": args, "incident_id": incident_id}, action
            )
            self.audit_log.log("approval_notification_sent", incident_id=incident_id, to=email.to)
            return {
                "status": "PENDING_APPROVAL",
                "detail": f"'{tool.name}' requires Tier 3 approval",
                "incident_id": incident_id,
            }

        snapshot = self._snapshot(action, args)
        breaker = self._breaker_for(action.engine)
'''

# 1c. expire_stale_approvals(): set the suppression flag on timeout, and
#     bring the docstring's "Known limitation" note up to date.
gc_anchor_expire = '''        Known limitation: this clears the STALE entry, but if the
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
verify_once(gc_src, gc_anchor_expire, "guardrail_callbacks.py (expire_stale_approvals tail)")
gc_replacement_expire = '''        Flooding guard (external review, closed 2026-09-02): on its own,
        clearing the stale entry above wasn't enough -- if the underlying
        anomaly is still active, Predict proposes a FRESH Tier 3 action
        with a new incident_id on a later tick, roughly every ~2 minutes
        for as long as the anomaly persists, and before_tool() used to
        page Slack again every single time. Every timeout below now also
        arms self.suppressed[(engine_id, action_key)]; before_tool()'s
        Tier 3 branch checks that flag BEFORE creating a new
        pending_approvals entry or sending another approval request, so a
        prolonged outage produces exactly one PENDING_APPROVAL ping and
        one approval_timeout ping, then silence (still audit-logged) until
        an operator calls clear_suppression() -- see main.py's
        POST /suppressions/clear/<action_key>.
        Residual, disclosed limitation: suppression is keyed on the exact
        (engine_id, action_key) pair, so a different allowlisted action
        proposed for the same underlying condition still notifies
        separately; and nothing auto-clears the flag when the condition
        actually resolves -- an operator has to confirm that and reset it,
        by design, so a fix that silently stopped applying itself can't go
        unnoticed.
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
            suppression_key = (engine_id, pending["action_key"])
            self.suppressed[suppression_key] = {
                "since": now, "since_incident_id": incident_id,
                "repeat_count": 0, "last_incident_id": incident_id,
            }
        return expired_ids

    def clear_suppression(self, engine_id: str, action_key: str = None) -> list:
        """Manual reset for the Tier 3 flooding guard above -- an operator
        calls this (main.py's POST /suppressions/clear/<action_key>) once
        they've confirmed the underlying condition is actually handled, or
        was a false alarm. Nothing in this process clears a suppression on
        its own -- see expire_stale_approvals()'s docstring for why that's
        deliberate. Clears one (engine_id, action_key) pair, or every
        suppression on engine_id if action_key is omitted. Returns the
        (engine_id, action_key) pairs actually cleared."""
        if action_key is not None:
            keys = [(engine_id, action_key)] if (engine_id, action_key) in self.suppressed else []
        else:
            keys = [k for k in self.suppressed if k[0] == engine_id]
        for key in keys:
            cleared = self.suppressed.pop(key)
            self.audit_log.log(
                "suppression_cleared", incident_id=cleared.get("last_incident_id", "unknown"),
                action_key=key[1],
                detail=f"Manually reset by operator after {cleared.get('repeat_count', 0)} suppressed repeat(s).",
            )
        return keys

    def list_suppressed(self, engine_id: str) -> list:
        """Backs main.py's GET /suppressions -- same read-only-mirror
        pattern as pending_approvals/GET /approvals/pending."""
        return [
            {
                "action_key": action_key, "since": v["since"],
                "since_incident_id": v["since_incident_id"],
                "repeat_count": v.get("repeat_count", 0),
                "last_incident_id": v.get("last_incident_id"),
            }
            for (eng, action_key), v in self.suppressed.items() if eng == engine_id
        ]
'''

gc_new = gc_src.replace(gc_anchor_init, gc_replacement_init, 1)
gc_new = gc_new.replace(gc_anchor_tier3, gc_replacement_tier3, 1)
gc_new = gc_new.replace(gc_anchor_expire, gc_replacement_expire, 1)

# ---------------------------------------------------------------------
# 2. main.py -- add the two operator-facing routes right after
#    /approvals/pending, same read/act pairing /approve already has.
# ---------------------------------------------------------------------
main_path, main_src = load("main.py")
main_anchor = '''        for incident_id, p in orch.guardrails.pending_approvals.items()
    ]
    return jsonify(pending=pending)
'''
verify_once(main_src, main_anchor, "main.py (approvals_pending tail)")
main_addition = '''

@app.route("/suppressions")
def suppressions_list():
    """Tier 3 flooding guard: after an unresolved Tier 3 proposal times
    out (guardrail_callbacks.py's expire_stale_approvals), any further
    proposal of the SAME action on the SAME engine is silently suppressed
    -- audit-logged, not re-pinged to Slack -- until an operator clears it
    here. Backs a dashboard panel the same read-only-mirror way
    /approvals/pending does."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    return jsonify(suppressed=state(db_id).guardrails.list_suppressed(db_id))


@app.route("/suppressions/clear/<action_key>", methods=["POST"])
def suppressions_clear(action_key):
    """Manual reset for the Tier 3 flooding guard above -- an operator
    calls this once they've confirmed the underlying condition is
    actually handled (or was a false alarm); nothing in this process
    clears a suppression on its own, by design (see
    guardrail_callbacks.py's clear_suppression docstring)."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    cleared = state(db_id).guardrails.clear_suppression(db_id, action_key)
    return jsonify(cleared=[{"engine": e, "action_key": a} for e, a in cleared])
'''
main_new = main_src.replace(main_anchor, main_anchor + main_addition, 1)

# ---------------------------------------------------------------------
# All anchors verified -- now back up and write every file.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (gc_path, gc_src, gc_new),
    (main_path, main_src, main_new),
]:
    backup_path = path + ".bak.pretier3flood"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nAll 2 files patched successfully.")
